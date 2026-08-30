"""Wykrywanie nowych orzeczeń sądów powszechnych przez oficjalne REST/XML API
(`api.orzeczenia.wroclaw.sa.gov.pl/ncourt-api`) zamiast przez stronę wyszukiwania.

Odkryte przy analizie projektu SAOS (github.com/CeON/saos) - to ta sama
infrastruktura, z której korzysta officjalny system analizy orzeczeń, nie
udokumentowane publicznie API z gwarancją SLA. Zostawiamy w User-Agencie
adres kontaktowy (config.yaml -> http.user_agent) na wypadek pytań admina.

Używane WYŁĄCZNIE do wykrywania nowości przez obserwatora - listę i daty.
Pełną treść nadal pobiera i parsuje `MsSource.document()` (moduł `ms_gov.py`),
bo identyfikatory z obu miejsc są tożsame - sprawdzone empirycznie:
id z `/ncourt-api/judgements` działa wprost pod `orzeczenia.ms.gov.pl/details/$N/{id}`
i `/content/$N/{id}`. Dzięki temu żadna z przetestowanych ścieżek parsowania
treści się nie zmienia - zmienia się tylko to, jak znajdujemy identyfikatory.

Zysk względem scrapowania stron wyników:
  * zero ryzyka CAPTCHA na etapie wykrywania (dotąd najbardziej kruchy krok -
    patrz `http.py` / `BLOCK_MARKERS`, i uwaga w config.yaml o CAPTCHA przy ~0,7s),
  * do 1000+ pozycji na stronę zamiast 10 z HTML-a,
  * `publicationDateFrom` daje czyste okno "co przybyło od X" zamiast
    przeglądania kolejnych stron wyników w nadziei, że nic się nie prześlizgnęło.

To API NIE ma wyszukiwania po frazie/sygnaturze/sędzim - służy tylko do
importu. Interaktywne wyszukiwanie użytkownika nadal idzie przez `MsSource`
(HTML) albo przez własną bazę (`Store.search_fulltext`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Baza infrastruktury sądów powszechnych (Sąd Apelacyjny we Wrocławiu) - inny
# host niż orzeczenia.ms.gov.pl, więc PoliteClient limituje go osobno.
LIST_URL = "https://api.orzeczenia.wroclaw.sa.gov.pl/ncourt-api/judgements"

# Bezpiecznik: nawet przy błędnie ustawionym (bardzo starym) 'od' nie ciągniemy
# w nieskończoność - pełny backfill archiwum to osobna, świadoma decyzja (Faza 3).
_MAX_IDS = 20_000

_ID_RE = re.compile(r"<id>([^<]+)</id>")
_TOTAL_RE = re.compile(r'total="(\d+)"')


@dataclass
class NcourtApiSource:
    http: Any   # PoliteClient - ten sam co reszta źródeł (wspólny limiter/cache)

    def list_new_ids(self, publication_date_from: str, limit: int = 1000) -> list[str]:
        """Identyfikatory orzeczeń opublikowanych od `publication_date_from`
        (RRRR-MM-DD) włącznie, posortowane tak jak zwraca API (sygnatura)."""
        if not publication_date_from:
            raise ValueError(
                "publication_date_from jest wymagane - pełny backfill bez daty "
                "to osobna decyzja (Faza 3), nie coś, co obserwator robi po cichu")

        ids: list[str] = []
        offset = 0
        while True:
            url = f"{LIST_URL}?offset={offset}&limit={limit}&publicationDateFrom={publication_date_from}"
            xml = self.http.get(url, ttl=300)
            page_ids = _ID_RE.findall(xml)
            if not page_ids:
                break
            ids.extend(page_ids)
            total_match = _TOTAL_RE.search(xml)
            total = int(total_match.group(1)) if total_match else len(ids)
            if len(ids) >= total or len(ids) >= _MAX_IDS or len(page_ids) < limit:
                break
            offset += limit
        return ids[:_MAX_IDS]
