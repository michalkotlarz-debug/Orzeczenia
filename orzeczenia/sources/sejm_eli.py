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
from io import BytesIO, StringIO
from typing import Any

from bs4 import BeautifulSoup

from ..parse.common import clean_akt_html_text, clean_pdf_text, html_text
from ..parse.pdf_tables import pdf_to_text_with_tables

log = logging.getLogger("orzecznik.eli")

# Progi rozmiaru PDF-a. Zwykły akt waży poniżej megabajta; grube załączniki
# (programy wieloletnie, mapy, tabele na setki stron) potrafią mieć kilkanaście
# i to one wywracały import - parsowanie odbywa się w tym samym procesie co
# serwer WWW, więc jego pamięć jest pamięcią całej aplikacji.
_PDF_TABLES_MAX_BYTES = 8 * 1024 * 1024    # powyżej: bez wykrywania tabel
_PDF_MAX_BYTES = 20 * 1024 * 1024          # powyżej: akt zapisany bez treści


class _PdfTooBigForTables(Exception):
    """Sygnał do zejścia na lżejszy parser - łapany przez ten sam `except`,
    który obsługuje awarie wykrywania tabel."""


def pdf_to_text(data: bytes) -> str:
    """Wyciąga czysty tekst z bajtów PDF, strona po stronie. Same bajty nigdzie
    nie trafiają na dysk - wywołujący je od razu odrzuca po tym wywołaniu.

    Dlaczego nie `extract_text()` na całym dokumencie: pdfminer zamienia każdy
    znak w osobny obiekt Pythona (pozycja, wymiary, czcionka), a przy jednym
    wywołaniu na cały plik trzyma je wszystkie naraz i dokłada do tego cache
    czcionek. Na M.P. 2021 poz. 414 (311 stron, 775 czcionek, 13 MB) dawało to
    ponad 2,8 GB i proces ginął, blokując import na dobę. Tutaj po każdej stronie
    zostaje już tylko jej gotowy tekst, a obiekty idą do wyrzucenia - w pamięci
    siedzi jedna strona zamiast trzystu."""
    from pdfminer.converter import TextConverter
    from pdfminer.layout import LAParams
    from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
    from pdfminer.pdfpage import PDFPage

    strony: list[str] = []
    # caching=False: bez tego menedżer zasobów trzyma rozpakowane czcionki
    # i strumienie wszystkich stron do końca dokumentu.
    manager = PDFResourceManager(caching=False)
    with BytesIO(data) as fp:
        for page in PDFPage.get_pages(fp, caching=False):
            buf = StringIO()
            device = TextConverter(manager, buf, laparams=LAParams())
            try:
                PDFPageInterpreter(manager, device).process_page(page)
                strony.append(buf.getvalue())
            finally:
                device.close()
                buf.close()
    return "\n".join(strony)


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
            soup = BeautifulSoup(html, "lxml")
            # ELI stawia PRZED właściwą treścią pełny spis treści aktu jako
            # nawigacyjny <div id="toc"> (lista linków do każdego
            # Tytułu/Działu/Rozdziału/Artykułu) - dla obszernych aktów (np.
            # cały Kodeks postępowania cywilnego) to setki wierszy, które w
            # kolejności dokumentu wypadają PRZED właściwym wstępem/treścią
            # obwieszczenia, więc czytelnik przewijający stronę od góry widzi
            # najpierw ścianę samych nagłówków i wygląda, jakby reszta treści
            # zniknęła - sprawdzone na żywo na DU 2023/1550 (użytkownik: "wszystko
            # wyciąłeś"). Te same nagłówki i tak pojawiają się naturalnie w
            # dalszej części dokumentu przed każdą sekcją, więc ten blok jest
            # czystym duplikatem - bezpiecznie go usuwamy w całości.
            toc = soup.find(id="toc")
            if toc:
                toc.decompose()
            text = clean_akt_html_text(html_text(soup.body))
            if text:
                return text, "html"
        if meta.get("textPDF"):
            try:
                pdf_bytes = self.http.get_bytes(
                    self._url(f"/acts/{publisher}/{year}/{pos}/text.pdf"))
                if len(pdf_bytes) > _PDF_MAX_BYTES:
                    # Import zatrzymał się na M.P. 2021 poz. 414 (13 MB, załącznik
                    # z programem wieloletnim): parser zjadał całą pamięć procesu,
                    # kontener ginął i po restarcie brał ten sam akt od nowa - przez
                    # dobę nie wszedł do bazy ani jeden nowy dokument. Sam akt jest
                    # wart zapisania (metryka, tytuł, data), więc zwracamy brak
                    # treści zamiast blokować kolejkę.
                    log.warning("%s/%s/%s: PDF %.1f MB przekracza limit %.0f MB - "
                                "zapisuję akt bez treści",
                                publisher, year, pos, len(pdf_bytes) / 1048576,
                                _PDF_MAX_BYTES / 1048576)
                    return None, None
                try:
                    # Ścieżka z wykrywaniem tabel po geometrii (pdfplumber) -
                    # patrz parse/pdf_tables.py. Awaria tego kroku (np.
                    # nietypowo zbudowany PDF) nie może przekreślić importu
                    # całego aktu - wracamy wtedy do zwykłego liniowego
                    # tekstu pdfminer, tak jak dotąd.
                    if len(pdf_bytes) > _PDF_TABLES_MAX_BYTES:
                        # Wykrywanie tabel trzyma w pamięci geometrię każdej strony,
                        # więc przy grubym dokumencie kosztuje wielokrotność jego
                        # rozmiaru. Zwykły akt ma poniżej megabajta - powyżej progu
                        # idziemy od razu lżejszą ścieżką liniową.
                        raise _PdfTooBigForTables
                    text = pdf_to_text_with_tables(pdf_bytes).strip()
                except Exception:
                    log.warning("%s/%s/%s: wykrywanie tabel w PDF nie powiodło się, "
                               "zwykły tekst liniowy", publisher, year, pos)
                    text = pdf_to_text(pdf_bytes).strip()
                text = clean_pdf_text(text, act_type=meta.get("type"))
                if text:
                    return text, "pdf"
            except Exception as exc:
                log.warning("%s/%s/%s: nie udało się wyciągnąć tekstu z PDF (%s)",
                           publisher, year, pos, exc)
        return None, None
