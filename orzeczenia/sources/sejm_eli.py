"""Akty prawne (Dziennik Ustaw / Monitor Polski) z ELI API Sejmu.

  https://api.sejm.gov.pl/eli - publiczne, bez autoryzacji, JSON.

  lista roku:  GET /acts/{DU|MP}/{rok}                       -> {"count", "items":[...]}
  szczegóły:   GET /acts/{DU|MP}/{rok}/{pozycja}              -> pełne metadane (keywords,
               entryIntoForce, references, ...) - lista roku ma tylko okrojony zestaw pól
  treść HTML:  GET /acts/{DU|MP}/{rok}/{pozycja}/text.html    (dostępna tylko gdy meta
               ma textHTML=true - dla najświeższych pozycji rządowe centrum legislacji
               najpierw publikuje sam PDF, HTML dochodzi z opóźnieniem)
  treść PDF:   GET /acts/{DU|MP}/{rok}/{pozycja}/text.pdf     (gdy textPDF=true)

Nie trzymamy oryginalnych PDF-ów - gdy HTML jeszcze nie istnieje, PDF pobieramy
tylko po to, żeby wyciągnąć z niego czysty tekst (`pdf_to_text`), a same bajty
od razu wyrzucamy.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from bs4 import BeautifulSoup

from ..parse.common import clean_pdf_text, html_text

log = logging.getLogger("orzecznik.eli")


def pdf_to_text(data: bytes) -> str:
    """Wyciąga czysty tekst z bajtów PDF. Same bajty nigdzie nie trafiają na
    dysk - wywołujący je od razu odrzuca po tym wywołaniu."""
    from pdfminer.high_level import extract_text
    return extract_text(BytesIO(data)) or ""


@dataclass
class EliClient:
    cfg: Any
    http: Any

    def _url(self, path: str) -> str:
        return f"{self.cfg.base_url}{path}"

    def list_year(self, publisher: str, year: int) -> list[dict[str, Any]]:
        """Wszystkie pozycje danego rocznika - API oddaje je w JEDNEJ odpowiedzi
        (bez paginacji), posortowane od najnowszej (najwyższa pozycja) do
        najstarszej."""
        raw = self.http.get(self._url(f"/acts/{publisher}/{year}"), ttl=1800)
        data = json.loads(raw)
        return data.get("items") or []

    def year_count(self, publisher: str, year: int) -> int:
        raw = self.http.get(self._url(f"/acts/{publisher}/{year}"), ttl=1800)
        return int(json.loads(raw).get("count") or 0)

    def detail(self, publisher: str, year: int, pos: int) -> dict[str, Any]:
        raw = self.http.get(self._url(f"/acts/{publisher}/{year}/{pos}"), ttl=21600)
        return json.loads(raw)

    def changes(self, since: str, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """Akty nowe/zmienione od `since` (ISO 8601, np. '2026-09-01T00:00:00') -
        do przyrostowego dociągania. W przeciwieństwie do listy rocznika, każda
        pozycja ma już PEŁNY zestaw pól (jak `detail()`) - nie trzeba osobnego
        zapytania o szczegóły. Paginowane (`totalCount`/`offset`)."""
        raw = self.http.get(
            self._url(f"/changes/acts?since={since}&offset={offset}&limit={limit}"),
            ttl=120)
        return json.loads(raw)

    def text(self, publisher: str, year: int, pos: int, meta: dict[str, Any]) -> tuple[str | None, str | None]:
        """Zwraca (tekst, źródło_tekstu) - źródło to 'html' albo 'pdf', albo
        (None, None) gdy akt nie ma jeszcze żadnej dostępnej treści."""
        if meta.get("textHTML"):
            html = self.http.get(self._url(f"/acts/{publisher}/{year}/{pos}/text.html"),
                                 ttl=21600)
            text = html_text(BeautifulSoup(html, "lxml").body)
            if text:
                return text, "html"
        if meta.get("textPDF"):
            try:
                pdf_bytes = self.http.get_bytes(
                    self._url(f"/acts/{publisher}/{year}/{pos}/text.pdf"))
                text = pdf_to_text(pdf_bytes).strip()
                text = clean_pdf_text(text, act_type=meta.get("type"))
                if text:
                    return text, "pdf"
            except Exception as exc:
                log.warning("%s/%s/%s: nie udało się wyciągnąć tekstu z PDF (%s)",
                           publisher, year, pos, exc)
        return None, None
