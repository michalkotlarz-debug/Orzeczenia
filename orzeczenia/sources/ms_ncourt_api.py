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

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from ..http import RateLimited, SourceUnavailable

log = logging.getLogger("orzecznik.ncourt_api")

# Sprawdzone empirycznie: ten sam offset potrafi raz oddać puste 404, chwilę
# później 200 z pełnymi danymi - to niestabilność samego API (nieoficjalne,
# bez SLA), nie sygnał końca listy. Koniec listy API sygnalizuje inaczej:
# HTTP 200 z <judgements results="0">. Dlatego 404/5xx/blokadę na TEJ stronie
# próbujemy przeczekać, zamiast od razu uznawać to za koniec danych.
_PAGE_RETRIES = 4
_PAGE_RETRY_SECONDS = 3.0

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

    def list_new_ids(self, publication_date_from: str, limit: int = 1000,
                     max_ids: int | None = _MAX_IDS) -> list[str]:
        """Identyfikatory orzeczeń opublikowanych od `publication_date_from`
        (RRRR-MM-DD) włącznie, posortowane tak jak zwraca API (sygnatura).

        `max_ids=None` zdejmuje bezpiecznik `_MAX_IDS` - świadomie, do pełnego
        backfillu archiwum (Faza 3, patrz `archiwum.py`), gdzie ograniczeniem
        i tak jest tempo pobierania treści, nie długość tej listy."""
        if not publication_date_from:
            raise ValueError(
                "publication_date_from jest wymagane - pełny backfill bez daty "
                "to osobna decyzja (Faza 3), nie coś, co obserwator robi po cichu")

        ids: list[str] = []
        offset = 0
        while True:
            url = f"{LIST_URL}?offset={offset}&limit={limit}&publicationDateFrom={publication_date_from}"
            xml: str | None = None
            last_exc: Exception | None = None
            for attempt in range(1, _PAGE_RETRIES + 1):
                try:
                    xml = self.http.get(url, ttl=300)
                    break
                except (RateLimited, SourceUnavailable) as exc:
                    last_exc = exc
                    log.warning("lista identyfikatorów: offset=%s próba %s/%s nieudana (%s)",
                               offset, attempt, _PAGE_RETRIES, exc)
                    if attempt < _PAGE_RETRIES:
                        time.sleep(_PAGE_RETRY_SECONDS * attempt)
            if xml is None:
                # Nie tracimy tego, co już zebraliśmy - przy 465 tys. pozycji to
                # setki zapytań, jedna usterka w środku nie powinna kasować
                # całej dotychczasowej pracy tej strony wyliczania.
                log.warning("lista identyfikatorów: przerwano na offset=%s po %s próbach (%s) - "
                           "zwracam %s dotąd zebranych", offset, _PAGE_RETRIES, last_exc, len(ids))
                break
            page_ids = _ID_RE.findall(xml)
            if not page_ids:
                break
            ids.extend(page_ids)
            total_match = _TOTAL_RE.search(xml)
            total = int(total_match.group(1)) if total_match else len(ids)
            if len(ids) >= total or len(page_ids) < limit:
                break
            if max_ids is not None and len(ids) >= max_ids:
                break
            offset += limit
        return ids if max_ids is None else ids[:max_ids]
