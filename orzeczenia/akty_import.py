"""Import aktów prawnych (Dziennik Ustaw / Monitor Polski) z ELI API Sejmu.

Jeden przebieg = jeden rocznik jednego dziennika. Bezpiecznie przerwać
(Ctrl+C) i uruchomić ponownie - pozycje już zapisane w `akty_prawne` są
pomijane (`Store.known_akty`), więc kolejne wywołanie samo wznawia się od
miejsca przerwania. Do wielokrotnego wywoływania dla kolejnych roczników,
idąc od najnowszego wstecz - patrz `orzeczenia akty-importuj --help`.

Nie zapisujemy oryginalnych PDF-ów - gdy akt nie ma jeszcze HTML-a (typowe dla
bieżącego rocznika, rządowe centrum legislacji publikuje HTML z opóźnieniem),
PDF jest pobierany tylko po to, by wyciągnąć z niego tekst (sources/sejm_eli.py
`EliClient.text`), a bajty od razu porzucamy.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import json as _json
from pathlib import Path

from .config import Config
from .http import PoliteClient, RateLimited, SourceUnavailable
from .sources.sejm_eli import EliClient
from .store import Store

log = logging.getLogger("orzecznik.akty_import")

DEFAULT_BACKFILL_STATE = Path("dane/akty_wstecz_state.json")
EARLIEST_YEAR = 1918   # DU sięga 1918, MP 1930 - poniżej po prostu nie ma już czego szukać


def _load_backfill_year(state_path: Path, start_year: int) -> int:
    try:
        data = _json.loads(state_path.read_text(encoding="utf-8"))
        return int(data["year"])
    except (FileNotFoundError, KeyError, ValueError):
        return start_year


def _save_backfill_year(state_path: Path, year: int) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(_json.dumps({"year": year}), encoding="utf-8")


@dataclass
class AktyImportResult:
    publisher: str
    year: int
    total_in_source: int = 0
    already_had: int = 0
    downloaded: int = 0
    failed: int = 0
    without_text: int = 0
    interrupted: bool = False
    status: str = "ok"          # ok | przerwano | blad
    detail: str = ""


def _to_row(publisher: str, year: int, pos: int, meta: dict, text: str | None,
           text_source: str | None, source_url: str) -> dict:
    return {
        "publisher": publisher, "year": year, "pos": pos,
        "eli": meta.get("ELI"), "address": meta.get("address"), "title": meta.get("title"),
        "act_type": meta.get("type"), "status": meta.get("status"),
        "in_force": meta.get("inForce"),
        "promulgation_date": meta.get("promulgation"),
        "announcement_date": meta.get("announcementDate"),
        "entry_into_force_date": meta.get("entryIntoForce"),
        "released_by": meta.get("releasedBy") or [],
        "keywords": meta.get("keywords") or [],
        "act_references": meta.get("references") or {},
        "full_text": text, "text_source": text_source,
        "source_url": source_url, "changed_at": meta.get("changeDate"),
    }


def import_positions(cfg: Config, store: Store, publisher: str, year: int,
                     positions: list[int], http: PoliteClient | None = None) -> AktyImportResult:
    """Jak `import_year`, ale tylko dla wskazanych pozycji rocznika - do
    ręcznego dociągnięcia konkretnych aktów (np. przykładowych nieobowiązujących)
    bez importowania całego rocznika."""
    own_http = http is None
    http = http or PoliteClient(cfg.http, cfg.cache)
    client = EliClient(cfg.eli, http)
    result = AktyImportResult(publisher=publisher, year=year, total_in_source=len(positions))
    try:
        known = store.known_akty(publisher, year, positions)
        todo = [p for p in positions if p not in known]
        result.already_had = len(positions) - len(todo)
        for pos in todo:
            try:
                meta = client.detail(publisher, year, pos)
            except (RateLimited, SourceUnavailable) as exc:
                result.failed += 1
                log.warning("[%s/%s/%s] pominięto: %s", publisher, year, pos, exc)
                continue
            text, text_source = client.text(publisher, year, pos, meta)
            if text is None:
                result.without_text += 1
            source_url = f"{cfg.eli.base_url}/acts/{publisher}/{year}/{pos}"
            store.upsert_akty([_to_row(publisher, year, pos, meta, text, text_source, source_url)])
            result.downloaded += 1
        return result
    finally:
        if own_http:
            http.close()


def import_changes(cfg: Config, store: Store, since: str | None = None,
                   http: PoliteClient | None = None,
                   max_items: int = 500) -> AktyImportResult:
    """Przyrostowy import: tylko akty NOWE/ZMIENIONE od `since` (domyślnie: od
    najświeższego 'changeDate' już w bazie - patrz `Store.max_akty_changed`).
    Jedno zapytanie na stronę wystarcza (`/eli/changes/acts` oddaje już pełne
    metadane, bez osobnego `detail()`), więc to jest szybka ścieżka do
    codziennego uruchamiania - w przeciwieństwie do `import_year`, który
    ciągnie cały rocznik.

    `max_items` to bezpiecznik na wypadek bardzo starego kursora (np. pierwsze
    uruchomienie bez żadnych danych w bazie) - wtedy `since` musi być podane
    jawnie, inaczej ta funkcja nie wie, od kiedy zacząć."""
    since = since or store.max_akty_changed()
    if not since:
        raise ValueError("brak kursora 'since' - podaj go jawnie przy pierwszym uruchomieniu "
                         "(np. datę dzisiejszą pomniejszoną o kilka dni)")
    own_http = http is None
    http = http or PoliteClient(cfg.http, cfg.cache)
    client = EliClient(cfg.eli, http)
    result = AktyImportResult(publisher="*", year=0)
    try:
        offset = 0
        while True:
            page = client.changes(since, offset=offset, limit=100)
            items = page.get("items") or []
            total = int(page.get("totalCount") or len(items))
            result.total_in_source = total
            if not items:
                break
            for item in items:
                if result.downloaded >= max_items:
                    return result
                publisher, year, pos = item["publisher"], int(item["year"]), int(item["pos"])
                text, text_source = client.text(publisher, year, pos, item)
                if text is None:
                    result.without_text += 1
                source_url = f"{cfg.eli.base_url}/acts/{publisher}/{year}/{pos}"
                store.upsert_akty([_to_row(publisher, year, pos, item, text, text_source,
                                          source_url)])
                result.downloaded += 1
            offset += len(items)
            if offset >= total:
                break
        return result
    except KeyboardInterrupt:
        result.interrupted = True
        result.status = "przerwano"
        return result
    finally:
        if own_http:
            http.close()


def import_backfill_batch(cfg: Config, store: Store, publishers: list[str] | None = None,
                          batch_per_publisher: int = 300, start_year: int = 2025,
                          state_path: Path = DEFAULT_BACKFILL_STATE,
                          http: PoliteClient | None = None) -> dict:
    """Jedna 'paczka' cofania się w głąb archiwum - do wywoływania cyklicznie
    (np. co 30 minut z crona), zamiast ciągnąć cały rocznik naraz.

    Pamięta, na którym roczniku stanęła (`state_path`) - gdy oba dzienniki
    (DU i MP) mają już w bazie KOMPLET danego rocznika, przy następnym
    wywołaniu przechodzi o rok wstecz. `batch_per_publisher` ogranicza, ile
    NOWYCH pozycji na dziennik pobiera jedno wywołanie - tak, żeby pojedyncza
    paczka trwała rzędu kilku-kilkunastu minut, nie godzin.
    """
    publishers = publishers or list(cfg.eli.publishers)
    year = _load_backfill_year(state_path, start_year)
    own_http = http is None
    http = http or PoliteClient(cfg.http, cfg.cache)
    results: list[AktyImportResult] = []
    try:
        if year < EARLIEST_YEAR:
            return {"year": year, "done": True, "results": []}
        for pub in publishers:
            r = import_year(cfg, store, pub, year, http=http, limit=batch_per_publisher)
            results.append(r)

        all_complete = all(
            r.status == "ok" and (r.already_had + r.downloaded) >= r.total_in_source
            for r in results)
        next_year = year - 1 if all_complete else year
        _save_backfill_year(state_path, next_year)
        return {"year": year, "next_year": next_year, "complete_this_year": all_complete,
                "results": [r.__dict__ for r in results]}
    finally:
        if own_http:
            http.close()


def import_year(cfg: Config, store: Store, publisher: str, year: int,
                http: PoliteClient | None = None,
                limit: int | None = None) -> AktyImportResult:
    """Pobiera brakujące pozycje jednego rocznika. `limit` ogranicza liczbę
    NOWO pobranych w tym przebiegu (lista i sprawdzenie 'co już mamy' i tak
    obejmuje cały rocznik)."""
    own_http = http is None
    http = http or PoliteClient(cfg.http, cfg.cache)
    client = EliClient(cfg.eli, http)
    result = AktyImportResult(publisher=publisher, year=year)
    started = time.monotonic()
    try:
        try:
            items = client.list_year(publisher, year)
        except (RateLimited, SourceUnavailable) as exc:
            result.status = "blad"
            result.detail = f"nie udało się pobrać listy rocznika: {exc}"
            log.error("[%s/%s] %s", publisher, year, result.detail)
            return result

        result.total_in_source = len(items)
        known = store.known_akty(publisher, year, (i["pos"] for i in items))
        todo = [i for i in items if i["pos"] not in known]
        result.already_had = result.total_in_source - len(todo)
        if limit is not None:
            todo = todo[:limit]

        log.info("[%s/%s] %s w źródle, %s już w bazie, %s do pobrania w tym przebiegu",
                 publisher, year, result.total_in_source, result.already_had, len(todo))

        for n, item in enumerate(todo, start=1):
            pos = item["pos"]
            try:
                meta = client.detail(publisher, year, pos)
            except (RateLimited, SourceUnavailable) as exc:
                result.failed += 1
                log.warning("[%s/%s/%s] pominięto: %s", publisher, year, pos, exc)
                continue
            except Exception:
                result.failed += 1
                log.exception("[%s/%s/%s] pominięto (nieoczekiwany błąd)", publisher, year, pos)
                continue

            text, text_source = client.text(publisher, year, pos, meta)
            if text is None:
                result.without_text += 1

            source_url = f"{cfg.eli.base_url}/acts/{publisher}/{year}/{pos}"
            store.upsert_akty([_to_row(publisher, year, pos, meta, text, text_source, source_url)])
            result.downloaded += 1

            if n % 25 == 0 or n == len(todo):
                elapsed = time.monotonic() - started
                rate = n / elapsed if elapsed else 0
                remaining_min = ((len(todo) - n) / rate / 60) if rate else 0.0
                log.info("[%s/%s] postęp: %s/%s (%s błędów, %s bez tekstu) - "
                        "pozostało ok. %.1f min przy obecnym tempie",
                        publisher, year, n, len(todo), result.failed,
                        result.without_text, remaining_min)

        return result
    except KeyboardInterrupt:
        result.interrupted = True
        result.status = "przerwano"
        log.warning("[%s/%s] przerwano - już zapisane pozycje zostają w bazie, kolejne "
                    "uruchomienie wznowi od tego miejsca", publisher, year)
        return result
    finally:
        if own_http:
            http.close()
