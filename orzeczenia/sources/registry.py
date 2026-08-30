"""Odpytywanie kilku serwisów naraz i scalanie wyników."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from ..http import PoliteClient, RateLimited, SourceUnavailable
from .base import Hit, Query, SearchPage
from .kio_uzp import KioSource
from .ms_gov import MsSource
from .nsa_cbosa import NsaSource

log = logging.getLogger("orzecznik.registry")


class Registry:
    def __init__(self, cfg):
        self.cfg = cfg
        self.http = PoliteClient(cfg.http, cfg.cache)
        self.sources: dict[str, object] = {}
        if cfg.ms.enabled:
            self.sources["ms"] = MsSource(cfg.ms, self.http)
        if cfg.nsa.enabled:
            self.sources["nsa"] = NsaSource(cfg.nsa, self.http)
        if cfg.kio.enabled:
            self.sources["kio"] = KioSource(cfg.kio, self.http)

    def close(self) -> None:
        self.http.close()

    @property
    def labels(self) -> dict[str, str]:
        return {k: s.label for k, s in self.sources.items()}   # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    def search(self, q: Query, page: int = 1, only: str = "", per_page: int = 10) -> SearchPage:
        keys = [only] if only in self.sources else list(self.sources)
        result = SearchPage(page=page, per_page=per_page)

        # Nie każdy serwis prowadzi datę publikacji. Zamiast po cichu oddawać
        # z niego zero wyników, mówimy wprost, że został pominięty.
        if q.date_field == "publication" and (q.date_from or q.date_to):
            skipped = [k for k in keys
                       if not getattr(self.sources[k], "supports_publication_date", True)]
            for k in skipped:
                result.notes[k] = ("ten serwis nie prowadzi daty publikacji — "
                                   "przy tym filtrze go pomijamy")
            keys = [k for k in keys if k not in skipped]
            if not keys:
                return result

        # UWAGA: celowo NIE mnożymy zapytań do portalu przez per_page. Wcześniejsza
        # wersja pobierała tyle natywnych stron portalu (po NATIVE_PAGE_SIZE), ile
        # trzeba było, żeby złożyć per_page=50/100 - ale to sekwencyjne zapytania
        # (limiter ~1,2s między nimi na ten sam host, żeby nie oberwać CAPTCHĄ),
        # więc "Następna" na wynikach live potrafiła trwać kilka-kilkanaście sekund.
        # Dla wyszukiwania na żywo per_page jest więc tylko górnym limitem
        # WYŚWIETLANIA (ile pokazać z tego, co i tak przyszło z jednej natywnej
        # strony), nie mnożnikiem zapytań - szybkość ważniejsza niż dobicie do
        # dokładnie żądanej liczby w tym (i tak drugorzędnym od Fazy 1) trybie.
        def run(key: str):
            src = self.sources[key]
            try:
                hits, total = src.search(q, page)                  # type: ignore[attr-defined]
                return key, (hits, total), None
            except (RateLimited, SourceUnavailable) as exc:
                return key, ([], 0), str(exc)
            except Exception as exc:                            # nieoczekiwane
                log.exception("[%s] błąd wyszukiwania", key)
                return key, ([], 0), f"nieoczekiwany błąd: {exc}"

        with ThreadPoolExecutor(max_workers=max(1, len(keys))) as pool:
            for key, (hits, total), err in pool.map(run, keys):
                if err:
                    result.errors[key] = err
                    continue
                result.totals[key] = total
                result.hits.extend(hits)

        result.hits = self._post_filter(result.hits, q)
        result.hits = self._sort(result.hits, q.sort, interleave=len(keys) > 1)[:per_page]
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _post_filter(hits: list[Hit], q: Query) -> list[Hit]:
        """Jedyny filtr stosowany po naszej stronie: Portal Orzeczeń potrafi
        filtrować wyłącznie po dacie orzeczenia, nie po dacie publikacji."""
        if q.date_field != "publication" or not (q.date_from or q.date_to):
            return hits
        lo = q.date_from or "0000-00-00"
        hi = q.date_to or "9999-99-99"
        return [h for h in hits if h.publication_date and lo <= h.publication_date <= hi]

    @staticmethod
    def _sort(hits: list[Hit], sort: str, interleave: bool) -> list[Hit]:
        if sort == "date_desc":
            return sorted(hits, key=lambda h: h.judgment_date or "", reverse=True)
        if sort == "date_asc":
            return sorted(hits, key=lambda h: h.judgment_date or "9999")
        if sort == "pub_desc":
            return sorted(hits, key=lambda h: h.publication_date or h.judgment_date or "",
                          reverse=True)
        if not interleave:
            return hits
        # trafność liczy każdy serwis po swojemu, więc przeplatamy po równo
        buckets: dict[str, list[Hit]] = {}
        for h in hits:
            buckets.setdefault(h.source, []).append(h)
        out: list[Hit] = []
        for i in range(max((len(v) for v in buckets.values()), default=0)):
            for key in buckets:
                if i < len(buckets[key]):
                    out.append(buckets[key][i])
        return out

    # ------------------------------------------------------------------
    def document(self, source: str, doc_id: str) -> dict:
        src = self.sources.get(source)
        if src is None:
            raise SourceUnavailable(f"nieznane źródło: {source}")
        return src.document(doc_id)                             # type: ignore[attr-defined]

    def latest(self, limit: int = 8) -> SearchPage:
        """Ostatnio opublikowane - pobierane na żywo, cache'owane na kilka minut."""
        page = self.search(Query(sort="pub_desc"), page=1)
        page.hits = page.hits[:limit]
        return page
