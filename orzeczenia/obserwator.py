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
from .sources.ms_ncourt_api import NcourtApiSource
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


def _discover_ids_html(registry: Registry, cfg: Config, source: str,
                       result: RunResult) -> list[str]:
    """Odkrywanie nowości przez scrapowanie stron wyników - używane dla NSA i KIO,
    które (w przeciwieństwie do sądów powszechnych) nie mają publicznego API do
    masowego wylistowania identyfikatorów."""
    src_cfg = cfg.sources()[source]
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
    return [h.doc_id for h in collected]


def _discover_ids_ms(registry: Registry, store: Store, cfg: Config,
                     result: RunResult) -> list[str]:
    """Odkrywanie nowości dla sądów powszechnych przez oficjalne REST/XML API
    (`ncourt-api`, patrz `sources/ms_ncourt_api.py`) zamiast scrapowania stron
    wyników - bez ryzyka CAPTCHA, do 1000+ pozycji na żądanie. Punktem odcięcia
    jest najświeższa data publikacji, jaką już mamy - a nie stały okres wstecz."""
    since = store.max_publication_date("ms") or _window(cfg.poll.lookback_days)[0]
    try:
        ids = NcourtApiSource(registry.http).list_new_ids(since)
    except (RateLimited, SourceUnavailable) as exc:
        result.status = "blad"
        result.detail = str(exc)
        log.warning("[ms] przebieg przerwany (ncourt-api): %s", exc)
        return []
    except Exception as exc:                                   # nieoczekiwane
        result.status = "blad"
        result.detail = f"nieoczekiwany błąd (ncourt-api): {exc}"
        log.exception("[ms] przebieg przerwany (ncourt-api)")
        return []
    result.pages = 1                                            # jedno wywołanie ncourt-api
    return ids


def _fetch_and_store_each(registry: Registry, store: Store, source: str,
                          doc_ids: list[str], result: RunResult) -> None:
    """Dociąga pełną treść każdej pozycji i zapisuje ją OD RAZU, jedna po drugiej -
    nie zbiorczo na końcu. Pobranie treści z portalu potrafi zająć długie minuty
    (setki dokumentów, limiter tempa) - trzymanie ich w pamięci do jednego
    zbiorczego zapisu na końcu oznacza, że awaria połączenia z bazą (Neon zamyka
    długo bezczynne połączenia) albo błąd w środku przebiegu kasuje całą
    dotychczasową pracę. Częstszy, mniejszy zapis dodatkowo trzyma połączenie
    z bazą "żywe"."""
    for doc_id in doc_ids:
        try:
            doc = registry.document(source, doc_id)
        except (RateLimited, SourceUnavailable) as exc:
            log.warning("[%s] pominięto %s: %s", source, doc_id, exc)
            continue
        except Exception:
            log.exception("[%s] pominięto %s (nieoczekiwany błąd)", source, doc_id)
            continue
        try:
            result.added += store.upsert_documents([doc])
        except Exception as exc:
            result.status = "blad"
            result.detail = f"zapis do bazy ({doc_id}): {exc}"
            log.exception("[%s] zapis %s nie powiódł się", source, doc_id)


def run_source(registry: Registry, store: Store, cfg: Config, source: str) -> RunResult:
    result = RunResult(source=source)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if source == "ms":
        doc_ids = _discover_ids_ms(registry, store, cfg, result)
    else:
        doc_ids = _discover_ids_html(registry, cfg, source, result)

    doc_ids = doc_ids[: cfg.poll.max_new_per_run]
    result.seen = len(doc_ids)

    # Dociągamy pełną treść tylko dla pozycji, których jeszcze nie ma w bazie -
    # `document()` to osobne zapytanie (albo dwa) do portalu źródłowego, więc nie
    # ma sensu robić tego dla czegoś, co już mamy.
    known = store.known_ids(source, doc_ids)
    new_ids = list(dict.fromkeys(i for i in doc_ids if i not in known))

    _fetch_and_store_each(registry, store, source, new_ids, result)

    store.record_run(result, started)
    log.info("[%s] obejrzano %s, nowych %s (%s)",
             source, result.seen, result.added, result.status)
    return result


# Wcześniej niż powstał portal - z takim 'od' ncourt-api oddaje CAŁY zbiór
# (patrz orzeczenia/archiwum.py - ten sam próg, tam do zapisu na dysk zamiast
# do bazy).
EARLIEST_DATE = "2000-01-01"


def _known_ids_chunked(store: Store, source: str, ids: list[str], chunk: int = 2000) -> set[str]:
    """`known_ids` w kawałkach - przy pełnym archiwum (setki tysięcy id) jedno
    zapytanie z tyloma parametrami w klauzuli IN przeciążyłoby Postgresa."""
    known: set[str] = set()
    for i in range(0, len(ids), chunk):
        known |= store.known_ids(source, ids[i:i + chunk])
    return known


def import_ms_batch(cfg: Config, registry: Registry | None = None, store: Store | None = None,
                    limit: int = 1000, since: str | None = None, full: bool = False) -> RunResult:
    """Jednorazowy, większy import orzeczeń sądów powszechnych przez `ncourt-api` -
    do ręcznego wywołania (`python -m orzeczenia.cli importuj-ms`) albo z crona
    (GitHub Actions), gdy codzienny obserwator (ograniczony do nowości od
    ostatniego przebiegu) to za mało. Cofa się w oknie dat na tyle, żeby złapać
    kandydatów do `limit` jeszcze nieznanych pozycji - w przeciwieństwie do
    `run_source` NIE zatrzymuje się na tym, co już mamy jako punkt odcięcia,
    tylko sam szuka wstecz.

    `full=True`: sięga po CAŁY dostępny zbiór (dziś ok. 465 tys. pozycji) zamiast
    zwykłego okna 90 dni, i zdejmuje bezpiecznik `_MAX_IDS` na liście - listowanie
    jest tanie (sam XML, bez pobierania treści), więc powtarzanie go co przebieg
    jest OK; to pobranie treści (`registry.document`) jest wolne i ograniczone
    przez `limit`. Stan "co już mamy" trzyma się sam w bazie (`known_ids`), więc
    kolejne uruchomienia (np. co 30 min z GitHub Actions) same wznawiają się
    tam, gdzie poprzednie skończyło - bez żadnego pliku/kursora do pilnowania."""
    own_registry = registry is None
    own_store = store is None
    registry = registry or Registry(cfg)
    store = store or Store(cfg.store.url, cfg.store.keep_days)
    result = RunResult(source="ms")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        since = since or (EARLIEST_DATE if full else _window(90)[0])
        # Przy pełnym archiwum strona 1000 pozycji oznacza setki zapytań tylko
        # do wylistowania id. Kod SAOS deklaruje limit do 5000/stronę, ale to
        # był ich WŁASNY, klientowy bezpiecznik - sprawdzone empirycznie na
        # żywym API: 3000 i więcej to 404, 2000 działa. Bierzemy zweryfikowaną
        # wartość, nie deklarowaną w cudzym kodzie.
        list_kwargs = {"max_ids": None, "limit": 2000} if full else {}
        try:
            ids = NcourtApiSource(registry.http).list_new_ids(since, **list_kwargs)
        except (RateLimited, SourceUnavailable) as exc:
            result.status = "blad"
            result.detail = str(exc)
            store.record_run(result, started)
            return result
        except Exception as exc:                                # nieoczekiwane
            result.status = "blad"
            result.detail = f"nieoczekiwany błąd (ncourt-api): {exc}"
            log.exception("[ms] przebieg przerwany (ncourt-api)")
            store.record_run(result, started)
            return result

        result.pages = 1
        known = _known_ids_chunked(store, "ms", ids)
        new_ids = list(dict.fromkeys(i for i in ids if i not in known))[:limit]
        result.seen = len(new_ids)

        _fetch_and_store_each(registry, store, "ms", new_ids, result)

        store.record_run(result, started)
        log.info("[ms] (import wsadowy od %s) obejrzano %s, nowych %s (%s)",
                 since, result.seen, result.added, result.status)
        return result
    finally:
        if own_store:
            store.close()
        if own_registry:
            registry.close()


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
