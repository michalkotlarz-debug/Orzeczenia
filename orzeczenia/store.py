"""Własna baza orzeczeń zbieranych przez obserwatora.

Obserwator dociąga pełną treść (`upsert_documents`) każdego nowego orzeczenia,
które zobaczył - dzięki temu wyszukiwarka może czytać z tej bazy zamiast za
każdym razem pytać portal źródłowy na żywo (`search_fulltext`, `get_document`).
Warstwa web i tak trzyma żywy fallback (`registry.search()`/`registry.document()`)
na wypadek, gdy czegoś jeszcze nie zdążyliśmy zaimportować.

Dwa warianty składowania, jeden interfejs:
  * SQLite  - domyślnie, plik na dysku (lokalnie, Railway z wolumenem, VPS),
  * Postgres - gdy `store.url` / DATABASE_URL wskazuje postgres (Neon, Supabase,
    Railway); wymaga pakietu `psycopg`.

Celowo bez ORM-a: schemat ma pięć zapytań, a każda dodatkowa zależność to
kolejne megabajty w paczce wgrywanej na Vercela.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

from .parse.common import squash
from .sources.base import Hit

log = logging.getLogger("orzecznik.store")

# ----------------------------------------------------------------------
# Schemat, według którego orzeczenia są przenoszone z portali do aplikacji.
# `?` jest tłumaczone na `%s` dla Postgresa - patrz Store._sql().
# ----------------------------------------------------------------------
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS orzeczenia (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,          -- ms | nsa | kio
    doc_id         TEXT NOT NULL,
    signature      TEXT,
    doc_type       TEXT,
    doc_type_raw   TEXT,
    court          TEXT,
    division       TEXT,
    judgment_date  TEXT,                   -- RRRR-MM-DD
    publication_date TEXT,
    outcome        TEXT,
    excerpt        TEXT,
    panel          TEXT NOT NULL DEFAULT '[]',   -- JSON, lista nazwisk
    thematic       TEXT NOT NULL DEFAULT '[]',   -- JSON
    chairman       TEXT,
    legal_basis    TEXT,
    importance     TEXT,
    sentencja      TEXT,
    uzasadnienie   TEXT,
    full_text      TEXT,
    source_url     TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,          -- kiedy TA aplikacja to zobaczyła
    last_seen_at   TEXT NOT NULL,
    UNIQUE (source, doc_id)
);
CREATE INDEX IF NOT EXISTS ix_orz_first_seen ON orzeczenia (first_seen_at DESC);
CREATE INDEX IF NOT EXISTS ix_orz_judgment   ON orzeczenia (judgment_date DESC);
CREATE INDEX IF NOT EXISTS ix_orz_source     ON orzeczenia (source);
CREATE INDEX IF NOT EXISTS ix_orz_signature  ON orzeczenia (signature);

CREATE TABLE IF NOT EXISTS przebiegi (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    source       TEXT NOT NULL,
    pages        INTEGER NOT NULL DEFAULT 0,
    seen         INTEGER NOT NULL DEFAULT 0,   -- ile pozycji obejrzano
    added        INTEGER NOT NULL DEFAULT 0,   -- ile było nowych
    status       TEXT NOT NULL DEFAULT 'ok',   -- ok | blad
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS ix_run_started ON przebiegi (started_at DESC);
"""

SCHEMA_PG = SCHEMA_SQLITE.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")

COLUMNS = ("source", "doc_id", "signature", "doc_type", "court", "division",
           "judgment_date", "publication_date", "outcome", "excerpt", "panel",
           "thematic", "source_url", "first_seen_at", "last_seen_at")

# Kolumny dopisane po pierwszej wersji schematu - potrzebne do zapisu pełnej
# treści dokumentu (upsert_documents). Istniejące bazy dostają je przez
# migrację w Store._migrate(), bo `CREATE TABLE IF NOT EXISTS` nic im nie doda.
_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("doc_type_raw", "TEXT"),
    ("chairman", "TEXT"),
    ("legal_basis", "TEXT"),
    ("importance", "TEXT"),
    ("sentencja", "TEXT"),
    ("uzasadnienie", "TEXT"),
    ("full_text", "TEXT"),
    ("judges", "TEXT"),      # JSON [{"name":...,"role":...}] - pełny skład z rolami
    ("purchaser", "TEXT"),   # KIO: zamawiający
)

# Pola pełnego dokumentu (patrz orzeczenia/sources/*.document()) zapisywane
# przez upsert_documents - nadpisuje to, co ewentualnie już wpisał lekki upsert().
DOC_COLUMNS = ("source", "doc_id", "signature", "doc_type", "doc_type_raw", "court",
               "division", "judgment_date", "publication_date", "outcome", "purchaser",
               "excerpt", "panel", "thematic", "chairman", "legal_basis", "importance",
               "sentencja", "uzasadnienie", "full_text", "judges", "source_url",
               "first_seen_at", "last_seen_at")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_iso_date(value: Any) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            return None
    return None


def _doc_row(d: dict[str, Any]) -> tuple:
    """Wartości pełnego dokumentu w kolejności `DOC_COLUMNS[:-2]` (bez
    first_seen_at/last_seen_at - te dopisuje wywołujący)."""
    excerpt = d.get("excerpt") or squash((d.get("sentencja") or d.get("full_text") or "")[:400]) or None
    judges = d.get("judges") or []
    names = [j.get("name") for j in judges if j.get("name")]
    return (
        d.get("source"), d.get("doc_id"), d.get("signature"), d.get("doc_type"),
        d.get("doc_type_raw"), d.get("court"), d.get("division"),
        _as_iso_date(d.get("judgment_date")), _as_iso_date(d.get("publication_date")),
        d.get("outcome"), d.get("purchaser"), excerpt,
        json.dumps(names, ensure_ascii=False),
        json.dumps(d.get("thematic") or [], ensure_ascii=False),
        d.get("chairman"), d.get("legal_basis"), d.get("importance"),
        d.get("sentencja"), d.get("uzasadnienie"), d.get("full_text"),
        json.dumps(judges, ensure_ascii=False),
        d.get("source_url") or "",
    )


def _search_text(d: dict[str, Any]) -> str:
    """Tekst, z którego Postgres buduje `search_vector` (polska konfiguracja FTS)."""
    parts = [d.get("signature"), d.get("court"), d.get("division"), d.get("chairman"),
             d.get("legal_basis"), " ".join(d.get("thematic") or []),
             d.get("sentencja"), d.get("uzasadnienie"), d.get("full_text")]
    return " ".join(p for p in parts if p)


@dataclass
class RunResult:
    source: str
    pages: int = 0
    seen: int = 0
    added: int = 0
    status: str = "ok"
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Store:
    def __init__(self, url: str, keep_days: int = 400):
        self.url = url
        self.keep_days = keep_days
        self.is_pg = urlparse(url).scheme.startswith("postgres")
        self._lock = threading.Lock()
        if self.is_pg:
            try:
                import psycopg                      # noqa: F401
            except ImportError as exc:              # pragma: no cover
                raise RuntimeError(
                    "DATABASE_URL wskazuje Postgresa, ale brakuje pakietu psycopg "
                    "(dopisz `psycopg[binary]` do requirements.txt)") from exc
            self._psycopg = __import__("psycopg")
            self._dsn = url.replace("postgresql+psycopg://", "postgresql://")
            self._conn = self._psycopg.connect(self._dsn, autocommit=True)
            self._exec_script(SCHEMA_PG)
            self._migrate()
        else:
            path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url
            p = Path(path)
            if p.parent and str(p.parent) not in ("", "."):
                p.parent.mkdir(parents=True, exist_ok=True)
            self.path = str(p)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._exec_script(SCHEMA_SQLITE)
            self._migrate()

    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        """Dodaje kolumny/indeksy, których nie ma jeszcze baza założona przed
        wprowadzeniem pełnych dokumentów. `CREATE TABLE IF NOT EXISTS` tego nie
        robi na już istniejącej tabeli."""
        with self._lock:
            if self.is_pg:
                with self._conn.cursor() as cur:
                    for col, sqltype in _MIGRATION_COLUMNS:
                        cur.execute(f"ALTER TABLE orzeczenia ADD COLUMN IF NOT EXISTS "
                                    f"{col} {sqltype}")
                    # PostgreSQL nie ma wbudowanej konfiguracji 'polish' (Snowball nie
                    # zna polskiego). Bez prawdziwej odmiany słów zostaje nam 'simple'
                    # (tokenizacja + małe litery) + unaccent (ą/ę/ł... jak a/e/l) - gorzej
                    # niż SAOS-owy Lucene+morfologik, ale wciąż realne pełnotekstowe
                    # wyszukiwanie z rankingiem, zamiast samego LIKE.
                    cur.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
                    cur.execute("ALTER TABLE orzeczenia ADD COLUMN IF NOT EXISTS "
                                "search_vector tsvector")
                    cur.execute("CREATE INDEX IF NOT EXISTS ix_orz_search "
                                "ON orzeczenia USING GIN (search_vector)")
            else:
                existing = {row[1] for row in
                            self._conn.execute("PRAGMA table_info(orzeczenia)").fetchall()}
                for col, sqltype in _MIGRATION_COLUMNS:
                    if col not in existing:
                        self._conn.execute(f"ALTER TABLE orzeczenia ADD COLUMN {col} {sqltype}")
                self._conn.commit()

    # ------------------------------------------------------------------
    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_pg else sql

    def _exec_script(self, script: str) -> None:
        with self._lock:
            if self.is_pg:
                with self._conn.cursor() as cur:
                    cur.execute(script)
            else:
                self._conn.executescript(script)
                self._conn.commit()

    def _run(self, sql: str, params: Sequence[Any] = (), *, many: bool = False):
        with self._lock:
            try:
                return self._run_once(sql, params, many)
            except Exception as exc:
                if self.is_pg and self._looks_like_dead_connection(exc):
                    # Neon (i inne managed Postgresy) zamykają długo bezczynne
                    # połączenia - typowe przy przebiegu obserwatora, który
                    # spędza długie minuty na pobieraniu treści z portali
                    # między jednym zapisem a drugim. Odtwarzamy połączenie
                    # zamiast tracić cały przebieg na ostatnim kroku.
                    log.warning("połączenie z bazą padło, odtwarzam: %s", exc)
                    self._reconnect()
                    return self._run_once(sql, params, many)
                raise

    def _run_once(self, sql: str, params: Sequence[Any], many: bool):
        cur = self._conn.cursor()
        if many:
            cur.executemany(self._sql(sql), params)
        else:
            cur.execute(self._sql(sql), tuple(params))
        if not self.is_pg:
            self._conn.commit()
        return cur

    def _reconnect(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = self._psycopg.connect(self._dsn, autocommit=True)

    @staticmethod
    def _looks_like_dead_connection(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "connection" in msg and any(
            w in msg for w in ("closed", "ssl", "terminat", "reset", "eof"))

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cur = self._run(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    def known_ids(self, source: str, doc_ids: Iterable[str]) -> set[str]:
        ids = list(dict.fromkeys(doc_ids))
        if not ids:
            return set()
        holes = ",".join("?" for _ in ids)
        rows = self._rows(
            f"SELECT doc_id FROM orzeczenia WHERE source = ? AND doc_id IN ({holes})",
            [source, *ids])
        return {r["doc_id"] for r in rows}

    def upsert(self, hits: list[Hit]) -> int:
        """Zapisuje pozycje, których jeszcze nie ma. Zwraca liczbę NOWYCH."""
        if not hits:
            return 0
        now = _now()
        added = 0
        by_source: dict[str, list[Hit]] = {}
        for h in hits:
            by_source.setdefault(h.source, []).append(h)

        for source, group in by_source.items():
            known = self.known_ids(source, (h.doc_id for h in group))
            fresh: list[tuple] = []
            seen_now: set[str] = set()
            for h in group:
                if h.doc_id in known or h.doc_id in seen_now:
                    continue
                seen_now.add(h.doc_id)
                fresh.append((
                    h.source, h.doc_id, h.signature, h.doc_type, h.court, h.division,
                    _as_iso_date(h.judgment_date), _as_iso_date(h.publication_date),
                    h.outcome, h.excerpt,
                    json.dumps(h.panel, ensure_ascii=False),
                    json.dumps(h.thematic, ensure_ascii=False),
                    h.source_url, now, now))
            if fresh:
                cols = ", ".join(COLUMNS)
                holes = ", ".join("?" for _ in COLUMNS)
                self._run(f"INSERT INTO orzeczenia ({cols}) VALUES ({holes})",
                          fresh, many=True)
                added += len(fresh)
            if known:
                holes = ",".join("?" for _ in known)
                self._run(
                    f"UPDATE orzeczenia SET last_seen_at = ? "
                    f"WHERE source = ? AND doc_id IN ({holes})",
                    [now, source, *known])
        return added

    def upsert_documents(self, docs: list[dict[str, Any]]) -> int:
        """Zapisuje pełne dokumenty (wynik `Source.document()`). W przeciwieństwie
        do `upsert()` nadpisuje też treść już znanych pozycji - re-import może
        poprawić dane, nie tylko odświeżyć `last_seen_at`. Zwraca liczbę NOWYCH."""
        if not docs:
            return 0
        now = _now()
        added = 0
        value_cols = DOC_COLUMNS[:-2]              # bez first_seen_at/last_seen_at
        set_cols = value_cols[2:]                  # bez source/doc_id (są w WHERE)
        by_source: dict[str, list[dict[str, Any]]] = {}
        for d in docs:
            if d.get("source") and d.get("doc_id"):
                by_source.setdefault(d["source"], []).append(d)

        for source, group in by_source.items():
            known = self.known_ids(source, (d["doc_id"] for d in group))
            fresh: list[tuple] = []
            updates: list[tuple] = []
            seen_now: set[str] = set()
            for d in group:
                doc_id = d["doc_id"]
                if doc_id in seen_now:
                    continue
                seen_now.add(doc_id)
                row = _doc_row(d)
                text = _search_text(d)
                if doc_id in known:
                    tail = (text, now, source, doc_id) if self.is_pg else (now, source, doc_id)
                    updates.append((*row[2:], *tail))
                else:
                    tail = (text, now, now) if self.is_pg else (now, now)
                    fresh.append((*row, *tail))

            if fresh:
                cols = ", ".join(value_cols)
                holes = ", ".join("?" for _ in value_cols)
                if self.is_pg:
                    sql = (f"INSERT INTO orzeczenia ({cols}, search_vector, "
                           f"first_seen_at, last_seen_at) "
                           f"VALUES ({holes}, to_tsvector('simple', unaccent(?)), ?, ?)")
                else:
                    sql = (f"INSERT INTO orzeczenia ({cols}, first_seen_at, last_seen_at) "
                           f"VALUES ({holes}, ?, ?)")
                self._run(sql, fresh, many=True)
                added += len(fresh)

            if updates:
                assign = ", ".join(f"{c} = ?" for c in set_cols)
                if self.is_pg:
                    sql = (f"UPDATE orzeczenia SET {assign}, "
                           f"search_vector = to_tsvector('simple', unaccent(?)), "
                           f"last_seen_at = ? WHERE source = ? AND doc_id = ?")
                else:
                    sql = (f"UPDATE orzeczenia SET {assign}, last_seen_at = ? "
                           f"WHERE source = ? AND doc_id = ?")
                self._run(sql, updates, many=True)
        return added

    def get_document(self, source: str, doc_id: str) -> dict[str, Any] | None:
        """Pełny dokument z bazy, albo None jeśli jeszcze nie zaimportowany
        (wtedy wywołujący ma dociągnąć go na żywo - patrz web/app.py)."""
        rows = self._rows(
            "SELECT * FROM orzeczenia WHERE source = ? AND doc_id = ?", [source, doc_id])
        if not rows or not rows[0].get("full_text"):
            return None
        return self._decode(rows[0])

    def search_fulltext(self, query: str, source: str = "", limit: int = 20,
                        offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        """Szuka w już zaimportowanych dokumentach. Postgres: pełnotekstowo,
        tokenizacja + ranking przez `search_vector` (config 'simple' + unaccent -
        PostgreSQL nie ma wbudowanej odmiany polskich słów, więc to dopasowanie
        tokenów bez końcówek fleksyjnych, nie prawdziwa morfologia jak w SAOS).
        SQLite (lokalnie/dev): zwykłe LIKE - wystarczające do testów."""
        query = squash(query)
        if not query:
            return [], 0
        where = ["source = ?"] if source else []
        params: list[Any] = [source] if source else []
        if self.is_pg:
            where.append("search_vector @@ plainto_tsquery('simple', unaccent(?))")
            params.append(query)
            where_sql = " AND ".join(where)
            total = self._rows(
                f"SELECT COUNT(*) AS n FROM orzeczenia WHERE {where_sql}", params)[0]["n"]
            rows = self._rows(
                f"SELECT *, ts_rank(search_vector, "
                f"plainto_tsquery('simple', unaccent(?))) AS rank "
                f"FROM orzeczenia WHERE {where_sql} "
                f"ORDER BY rank DESC, judgment_date DESC LIMIT ? OFFSET ?",
                [query, *params, int(limit), int(offset)])
        else:
            like = f"%{query}%"
            where.append("(full_text LIKE ? OR signature LIKE ? OR sentencja LIKE ?)")
            params.extend([like, like, like])
            where_sql = " AND ".join(where)
            total = self._rows(
                f"SELECT COUNT(*) AS n FROM orzeczenia WHERE {where_sql}", params)[0]["n"]
            rows = self._rows(
                f"SELECT * FROM orzeczenia WHERE {where_sql} "
                f"ORDER BY judgment_date DESC LIMIT ? OFFSET ?",
                [*params, int(limit), int(offset)])
        return [self._decode(r) for r in rows], int(total)

    def count_fulltext_by_source(self, query: str) -> dict[str, int]:
        """Ile trafień ma `search_fulltext(query)` w każdym źródle - do zakładek
        'Sądy powszechne (N)' itd. na stronie wyników, gdy nie wybrano jednego źródła."""
        query = squash(query)
        if not query:
            return {}
        if self.is_pg:
            rows = self._rows(
                "SELECT source, COUNT(*) AS n FROM orzeczenia "
                "WHERE search_vector @@ plainto_tsquery('simple', unaccent(?)) "
                "GROUP BY source", [query])
        else:
            like = f"%{query}%"
            rows = self._rows(
                "SELECT source, COUNT(*) AS n FROM orzeczenia "
                "WHERE (full_text LIKE ? OR signature LIKE ? OR sentencja LIKE ?) "
                "GROUP BY source", [like, like, like])
        return {r["source"]: r["n"] for r in rows}

    # ------------------------------------------------------------------
    def latest(self, limit: int = 20, source: str = "", since: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM orzeczenia WHERE 1=1"
        params: list[Any] = []
        if source:
            sql += " AND source = ?"
            params.append(source)
        if d := _as_iso_date(since):
            sql += " AND judgment_date >= ?"
            params.append(d)
        sql += " ORDER BY first_seen_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return [self._decode(r) for r in self._rows(sql, params)]

    def count(self) -> dict[str, int]:
        rows = self._rows("SELECT source, COUNT(*) AS n FROM orzeczenia GROUP BY source")
        return {r["source"]: r["n"] for r in rows}

    def max_publication_date(self, source: str) -> str | None:
        """Najświeższa data publikacji, jaką już mamy dla danego źródła - kursor
        do przyrostowego importu ('daj mi wszystko od tego, co już znamy')."""
        rows = self._rows(
            "SELECT MAX(publication_date) AS m FROM orzeczenia WHERE source = ?", [source])
        return rows[0]["m"] if rows else None

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM przebiegi ORDER BY started_at DESC LIMIT ?", [int(limit)])

    def record_run(self, result: RunResult, started: str) -> None:
        self._run(
            "INSERT INTO przebiegi (started_at, finished_at, source, pages, seen, "
            "added, status, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [started, _now(), result.source, result.pages, result.seen,
             result.added, result.status, (result.detail or "")[:2000]])

    def prune(self) -> int:
        """Usuwa wpisy starsze niż `keep_days` - baza ma nie puchnąć w nieskończoność."""
        if not self.keep_days:
            return 0
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=self.keep_days)).isoformat(timespec="seconds")
        cur = self._run("DELETE FROM orzeczenia WHERE first_seen_at < ?", [cutoff])
        removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        self._run("DELETE FROM przebiegi WHERE started_at < ?", [cutoff])
        return removed

    # ------------------------------------------------------------------
    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        for key in ("panel", "thematic", "judges"):
            try:
                d[key] = json.loads(d.get(key) or "[]")
            except (TypeError, ValueError):
                d[key] = []
        d["url"] = f"/orzeczenie/{d['source']}/{d['doc_id']}"
        return d

    @staticmethod
    def to_hit(row: dict[str, Any]) -> Hit:
        return Hit(
            source=row["source"], doc_id=row["doc_id"], signature=row.get("signature"),
            doc_type=row.get("doc_type"), court=row.get("court"),
            division=row.get("division"), judgment_date=row.get("judgment_date"),
            publication_date=row.get("publication_date"), panel=row.get("panel") or [],
            thematic=row.get("thematic") or [], excerpt=row.get("excerpt"),
            outcome=row.get("outcome"), source_url=row.get("source_url") or "")
