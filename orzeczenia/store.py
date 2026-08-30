"""Baza NOWYCH orzeczeń zebranych przez obserwatora.

To nie jest kopia archiwum portali. Wyszukiwanie w aplikacji nadal idzie
na żywo do serwisów źródłowych - tutaj lądują wyłącznie pozycje, które
obserwator zobaczył po raz pierwszy, żeby dało się odpowiedzieć na pytanie
"co się pojawiło od wczoraj" bez odpytywania portali przy każdym wejściu.

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
    court          TEXT,
    division       TEXT,
    judgment_date  TEXT,                   -- RRRR-MM-DD
    publication_date TEXT,
    outcome        TEXT,
    excerpt        TEXT,
    panel          TEXT NOT NULL DEFAULT '[]',   -- JSON
    thematic       TEXT NOT NULL DEFAULT '[]',   -- JSON
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
            cur = self._conn.cursor()
            if many:
                cur.executemany(self._sql(sql), params)
            else:
                cur.execute(self._sql(sql), tuple(params))
            if not self.is_pg:
                self._conn.commit()
            return cur

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
        for key in ("panel", "thematic"):
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
