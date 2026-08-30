"""Obserwator - cykliczne sprawdzanie, czy w portalach pojawiły się nowe orzeczenia.

Jeden przebieg to: dla każdego włączonego serwisu pobierz kilka pierwszych stron
najświeższych wyników, a dla każdej pozycji, której jeszcze nie ma w bazie, dociągnij
pełną treść (`registry.document()`) i zapisz ją. To właśnie ten przebieg zapełnia
własną bazę, z której czyta wyszukiwarka (`Store.search_fulltext`) - obserwator nie jest
już tylko "licznikiem nowości", tylko głównym mechanizmem importu danych.

Uruchamianie:
  * lokalnie / Railway / VPS:  python -m orzeczenia obserwuj
  * Vercel Cron:               GET /api/obserwator/uruchom  (z tokenem)

Każdy serwis liczony jest osobno: awaria jednego nie psuje przebiegu pozostałych.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from .config import Config
from .http import RateLimited, SourceUnavailable
from .sources.base import Hit, Query
from .sources.registry import Registry
from .store import RunResult, Store

log = logging.getLogger("orzecznik.obserwator")


def _window(days: int) -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def build_query(source: str, lookback_days: int) -> Query:
    """Każdy portal inaczej rozumie 'najnowsze'."""
    od, do = _window(lookback_days)
    if source == "ms":
        # Portal Orzeczeń sam potrafi posortować po dacie publikacji - a to ona,
        # nie data wydania, mówi "właśnie się pojawiło".
        return Query(sort="pub_desc")
    if source == "nsa":
        # CBOSA nie ma sortowania; jedynym sitem jest okno dat orzeczenia.
        return Query(date_from=od, date_to=do, date_field="judgment")
    return Query(sort="date_desc", date_from=od, date_to=do, date_field="judgment")


def run_source(registry: Registry, store: Store, cfg: Config, source: str) -> RunResult:
    src_cfg = cfg.sources()[source]
    result = RunResult(source=source)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    query = build_query(source, cfg.poll.lookback_days)
    collected: list[Hit] = []

    try:
        for page in range(1, max(1, src_cfg.poll_pages) + 1):
            hits, _total = registry.sources[source].search(query, page)  # type: ignore[attr-defined]
            result.pages = page
            if not hits:
                break
            collected.extend(hits)
            if len(collected) >= cfg.poll.max_new_per_run:
                break
    except (RateLimited, SourceUnavailable) as exc:
        result.status = "blad"
        result.detail = str(exc)
        log.warning("[%s] przebieg przerwany: %s", source, exc)
    except Exception as exc:                                   # nieoczekiwane
        result.status = "blad"
        result.detail = f"nieoczekiwany błąd: {exc}"
        log.exception("[%s] przebieg przerwany", source)

    collected = collected[: cfg.poll.max_new_per_run]
    result.seen = len(collected)

    # Dociągamy pełną treść tylko dla pozycji, których jeszcze nie ma w bazie -
    # `document()` to osobne zapytanie (albo dwa) do portalu źródłowego, więc nie
    # ma sensu robić tego dla czegoś, co już mamy.
    known = store.known_ids(source, (h.doc_id for h in collected))
    new_ids = list(dict.fromkeys(h.doc_id for h in collected if h.doc_id not in known))

    docs: list[dict] = []
    for doc_id in new_ids:
        try:
            docs.append(registry.document(source, doc_id))
        except (RateLimited, SourceUnavailable) as exc:
            log.warning("[%s] pominięto %s: %s", source, doc_id, exc)
        except Exception:
            log.exception("[%s] pominięto %s (nieoczekiwany błąd)", source, doc_id)

    if docs:
        try:
            result.added = store.upsert_documents(docs)
        except Exception as exc:
            result.status = "blad"
            result.detail = f"zapis do bazy: {exc}"
            log.exception("[%s] zapis nie powiódł się", source)

    store.record_run(result, started)
    log.info("[%s] obejrzano %s, nowych %s (%s)",
             source, result.seen, result.added, result.status)
    return result


def run_once(cfg: Config, registry: Registry | None = None,
             store: Store | None = None) -> list[RunResult]:
    """Jeden pełny przebieg po wszystkich serwisach oznaczonych `poll: true`."""
    own_registry = registry is None
    own_store = store is None
    registry = registry or Registry(cfg)
    store = store or Store(cfg.store.url, cfg.store.keep_days)
    try:
        targets = [k for k, sc in cfg.sources().items()
                   if sc.enabled and sc.poll and k in registry.sources]
        if not targets:
            log.info("żaden serwis nie ma włączonego obserwatora")
            return []
        results = [run_source(registry, store, cfg, key) for key in targets]
        removed = store.prune()
        if removed:
            log.info("usunięto %s przeterminowanych wpisów", removed)
        return results
    finally:
        if own_store:
            store.close()
        if own_registry:
            registry.close()
