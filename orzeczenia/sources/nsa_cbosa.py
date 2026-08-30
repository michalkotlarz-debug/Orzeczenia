"""Centralna Baza Orzeczeń Sądów Administracyjnych - orzeczenia.nsa.gov.pl.

Serwis nie ma adresów wyszukiwania w postaci GET. Przebieg jest trzyetapowy
i trzyma stan w sesji (ciasteczku):

  1. GET  /cbo/query          - czyści poprzednie wyszukiwanie w sesji
  2. POST /cbo/search         - właściwe zapytanie, oddaje stronę 1
  3. GET  /cbo/find?p=N       - kolejne strony TEGO SAMEGO zapytania

Dlatego wszystko idzie przez `http.session(...)`, a nie przez zwykłe `get()`.

Pułapki sprawdzone na żywo:
* pominięcie kroku 1. sprawia, że kolejny POST w tej samej sesji potrafi
  oddać "Nie znaleziono orzeczeń spełniających podany warunek!",
* daty działają tylko PARAMI - samo `odDaty` bez `doDaty` daje zero wyników,
* format daty to RRRR-MM-DD (tak podpisuje to sam formularz),
* identyfikator dokumentu to kilkanaście znaków szesnastkowych, np. /doc/226B5A6CD0,
* serwis odcina klientów pytających zbyt gęsto - stąd wysoki `delay_seconds`.

UWAGA: robots.txt CBOSA zabrania /cbo/search i /cbo/find wszystkim robotom.
Zapytania stąd idą tylko wtedy, gdy w konfiguracji ustawiono
`sources.nsa.ignore_robots: true`. Patrz README, rozdział "robots.txt".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup

from ..parse.common import (clean_person, court_level, detect_doc_types, html_text,
                            normalize_person, normalize_signature, parse_date, sort_panel,
                            squash)
from .base import Hit, Query

log = logging.getLogger("orzecznik.nsa")

PER_PAGE = 10
DOC_ID_RE = re.compile(r"/doc/([0-9A-Fa-f]{6,16})")
COUNT_RE = re.compile(r"Znaleziono\s+([\d\s ]+)\s+orzecze")
# "I SA/Łd 269/26 - Wyrok WSA w Łodzi z 2026-08-27"
TITLE_RE = re.compile(
    r"^\s*(?P<sig>.+?)\s+-\s+(?P<type>[^-]+?)\s+z\s+(?P<date>\d{4}-\d{2}-\d{2})\s*$")
NO_RESULTS = "Nie znaleziono orzeczeń spełniających podany warunek"

# Etykiety metryki -> klucze, których używa reszta aplikacji.
FIELDS = {
    "data orzeczenia": "judgment_date",
    "data wpływu": "received_date",
    "sąd": "court",
    "sędziowie": "judges_raw",
    "symbol z opisem": "symbol",
    "hasła tematyczne": "thematic_raw",
    "skarżony organ": "authority",
    "treść wyniku": "outcome",
    "powołane przepisy": "legal_basis",
    "publikacja w u.z.o.": "publication_ref",
    "info. o glosach": "glosy",
    "tezy": "tezy",
    "zdanie odrębne": "zdanie_odrebne",
    "sentencja": "sentencja",
    "uzasadnienie": "uzasadnienie",
}


@dataclass
class NsaSource:
    cfg: Any
    http: Any
    key: str = "nsa"
    # CBOSA nie prowadzi daty publikacji orzeczenia - tylko datę wydania
    # i datę wpływu sprawy. Filtr "data publikacji" nie ma tu odpowiednika.
    supports_publication_date: bool = False

    @property
    def label(self) -> str:
        return self.cfg.label or "Sądy administracyjne"

    @property
    def _skip_robots(self) -> bool:
        return bool(getattr(self.cfg, "ignore_robots", False))

    # ------------------------------------------------------------------
    def query_url(self) -> str:
        return f"{self.cfg.base_url}/cbo/query"

    def search_url(self) -> str:
        return f"{self.cfg.base_url}/cbo/search"

    def page_url(self, page: int) -> str:
        return f"{self.cfg.base_url}/cbo/find?p={page}"

    def doc_url(self, doc_id: str) -> str:
        return f"{self.cfg.base_url}/doc/{doc_id}"

    # ------------------------------------------------------------------
    def form_data(self, q: Query) -> dict[str, str]:
        """Zapytanie użytkownika przełożone na pola formularza CBOSA."""
        data: dict[str, str] = {
            "wszystkieSlowa": q.phrase or "",
            "wystepowanie": "gdziekolwiek",
            "odmiana": "on",              # uwzględnij odmianę słów
            "sygnatura": q.signature or "",
            "sad": "dowolny",
            "rodzaj": "dowolny",
            "symbole": "",
            "odDaty": "",
            "doDaty": "",
            "sedziowie": q.judge or "",
            "funkcja": "dowolna",
            "rodzaj_organu": "",
            "hasla": q.thematic or "",
            "akty": "",
            "przepisy": q.legal_basis or "",
            "publikacje": "",
            "glosy": "",
            "submit": "Szukaj",
        }
        # Daty tylko parami - inaczej CBOSA oddaje pustą listę.
        if q.date_field != "publication" and (q.date_from or q.date_to):
            data["odDaty"] = q.date_from or "1980-01-01"
            data["doDaty"] = q.date_to or "2099-12-31"
        return data

    def search(self, q: Query, page: int = 1) -> tuple[list[Hit], int]:
        # Filtr po dacie publikacji: CBOSA takiej daty nie prowadzi. Zamiast
        # udawać, że filtr zadziałał, świadomie pomijamy ten serwis - registry
        # dopisze o tym notkę na liście wyników.
        if q.date_field == "publication" and (q.date_from or q.date_to):
            return [], 0

        data = self.form_data(q)
        tag = "nsa|" + "&".join(f"{k}={v}" for k, v in sorted(data.items()) if v)
        ses = self.http.session(tag)
        ttl = self.http.cache_cfg.listing_ttl_seconds
        skip = self._skip_robots

        # Strona 1 zawsze przez POST; dopiero ona ustawia wynik w sesji.
        ses.get(self.query_url(), ttl=60, ignore_robots=skip)
        html = ses.post(self.search_url(), data, ttl=ttl, ignore_robots=skip)
        total = self.parse_count(html)
        if page > 1:
            html = ses.get(self.page_url(page), ttl=ttl, ignore_robots=skip)
        return self.parse_results(html), total

    # ------------------------------------------------------------------
    @staticmethod
    def parse_count(html: str) -> int:
        if NO_RESULTS in html:
            return 0
        if m := COUNT_RE.search(html):
            digits = re.sub(r"\D", "", m.group(1))
            return int(digits) if digits else 0
        return 0

    def parse_results(self, html: str) -> list[Hit]:
        soup = BeautifulSoup(html, "lxml")
        out: list[Hit] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=DOC_ID_RE):
            m = DOC_ID_RE.search(a.get("href") or "")
            if not m:
                continue
            doc_id = m.group(1).upper()
            if doc_id in seen:
                continue
            seen.add(doc_id)
            hit = self._hit_from_title(doc_id, squash(a.get_text(" ", strip=True)))
            hit.excerpt = self._excerpt_after(a)
            out.append(hit)
        return out

    def _hit_from_title(self, doc_id: str, title: str) -> Hit:
        sig = doc_type = court = date = None
        if m := TITLE_RE.match(title):
            sig = normalize_signature(m.group("sig"))
            doc_type = squash(m.group("type"))
            date = m.group("date")
        else:                       # tytuł w nieoczekiwanym kształcie - nie zgadujemy
            sig = normalize_signature(title) or title or None
        if doc_type:
            # "Wyrok WSA w Łodzi" -> rodzaj + sąd
            parts = doc_type.split(" ", 1)
            doc_type = parts[0]
            court = parts[1] if len(parts) > 1 else None
        types = detect_doc_types(doc_type)
        return Hit(
            source=self.key, doc_id=doc_id,
            signature=sig,
            doc_type=types[0] if types else (doc_type or None),
            court=court,
            judgment_date=date,
            publication_date=None,
            source_url=self.doc_url(doc_id),
        )

    @staticmethod
    def _excerpt_after(link) -> str | None:
        """Streszczenie stoi w NASTĘPNYM wierszu tabeli niż tytuł."""
        row = link.find_parent("tr")
        if row is None:
            return None
        nxt = row.find_next_sibling("tr")
        if nxt is None or nxt.find("a", href=DOC_ID_RE):
            return None
        text = squash(nxt.get_text(" ", strip=True))
        return text or None

    # ------------------------------------------------------------------
    def document(self, doc_id: str) -> dict[str, Any]:
        ttl = self.http.cache_cfg.document_ttl_seconds
        # /doc/ nie jest objęte zakazem w robots.txt CBOSA.
        html = self.http.get(self.doc_url(doc_id), ttl=ttl)
        return self.parse_document(doc_id, html)

    def parse_document(self, doc_id: str, html: str) -> dict[str, Any]:
        from ..http import looks_blocked
        if looks_blocked(html):
            raise ValueError(f"{doc_id}: zamiast orzeczenia zwrócono stronę blokady")
        soup = BeautifulSoup(html, "lxml")
        meta = self._parse_fields(soup)
        title = squash(soup.title.get_text() if soup.title else "")

        sig = court = doc_type = None
        date = meta.get("judgment_date")
        if m := TITLE_RE.match(title):
            sig = normalize_signature(m.group("sig"))
            head = squash(m.group("type")).split(" ", 1)
            doc_type = head[0]
            court = head[1] if len(head) > 1 else None
            date = date or m.group("date")
        elif " - " in title:
            sig = normalize_signature(title.split(" - ")[0])
        court = meta.get("court") or court

        types = detect_doc_types(doc_type, (meta.get("sentencja") or "")[:400])
        judges = self._parse_judges(meta.get("judges_raw"))
        thematic = [squash(t) for t in re.split(r"[;\n]", meta.get("thematic_raw") or "")
                    if squash(t)]
        sentencja = meta.get("sentencja")
        uzasadnienie = meta.get("uzasadnienie")
        full = "\n\n".join(x for x in (meta.get("tezy"), sentencja, uzasadnienie,
                                       meta.get("zdanie_odrebne")) if x) or None

        if not sig and not sentencja:
            raise ValueError(f"{doc_id}: brak metryki - to nie wygląda na orzeczenie")

        return {
            "source": self.key, "source_label": self.label, "doc_id": doc_id,
            "signature": sig,
            "judgment_date": parse_date(date),
            "publication_date": None,
            "received_date": parse_date(meta.get("received_date")),
            "valid_from_date": None,
            "doc_type": types[0] if types else doc_type,
            "doc_type_raw": doc_type,
            "doc_types": types,
            "court": court, "court_level": court_level(court),
            "division": None,
            "chairman": next((p["name"] for p in judges
                              if p["role"] == "przewodniczący"), None),
            "judges": sort_panel(judges),
            "thematic": thematic,
            "legal_basis": meta.get("legal_basis"),
            "importance": None,
            "outcome": meta.get("outcome"),
            "authority": meta.get("authority"),
            "symbol": meta.get("symbol"),
            "final": meta.get("final"),
            "purchaser": None,
            "tezy": meta.get("tezy"),
            "sentencja": sentencja,
            "uzasadnienie": uzasadnienie,
            "zdanie_odrebne": meta.get("zdanie_odrebne"),
            "full_text": full,
            "source_url": self.doc_url(doc_id),
            "metryka": {k: v for k, v in meta.items() if not k.endswith("_raw")},
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_fields(soup) -> dict[str, str]:
        """Metryka CBOSA to tabela etykieta/wartość; sentencja i uzasadnienie
        siedzą w wierszach z własnymi klasami (…-uzasadnienie)."""
        out: dict[str, str] = {}
        label_cls = re.compile(r"info-list-label")
        value_cls = re.compile(r"info-list-value")
        for row in soup.find_all("tr"):
            if row.find("tr") is not None:      # wiersz opakowujący inną tabelę
                continue
            label_el = row.find(class_=label_cls)
            value_el = row.find(class_=value_cls)
            if label_el is None or value_el is None:
                continue
            label = squash(label_el.get_text(" ", strip=True)).rstrip(":").lower()
            value = html_text(value_el)
            key = FIELDS.get(label)
            if key and value and not out.get(key):
                out[key] = value
        # "2026-08-27 orzeczenie nieprawomocne" -> rozdziel
        if raw := out.get("judgment_date"):
            if m := re.search(r"(\d{4}-\d{2}-\d{2})", raw):
                out["judgment_date"] = m.group(1)
            if "nieprawomocne" in raw:
                out["final"] = "nieprawomocne"
            elif "prawomocne" in raw:
                out["final"] = "prawomocne"
        return out

    @staticmethod
    def _parse_judges(raw: str | None) -> list[dict[str, str]]:
        """'Jan Kowalski /przewodniczący/Anna Nowak /sprawozdawca/Piotr Zych'."""
        if not raw:
            return []
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        # CBOSA nie stawia między osobami żadnego separatora - jedynym znakiem
        # orientacyjnym jest rola zapisana w ukośnikach ZA nazwiskiem.
        # re.split z grupą przechwytującą daje na przemian: nazwisko, rola, ...
        parts = re.split(r"/([^/]*)/", raw.strip())
        for i in range(0, len(parts), 2):
            chunk = parts[i]
            role_raw = squash(parts[i + 1]).lower() if i + 1 < len(parts) else ""
            name = clean_person(chunk)
            if not name:
                continue
            norm = normalize_person(name)
            if norm in seen:
                continue
            seen.add(norm)
            if "przewod" in role_raw:
                role = "przewodniczący"
            elif "protokol" in role_raw:
                role = "protokolant"
            else:
                role = "sędzia"
            item = {"name": name, "role": role}
            if role_raw and role == "sędzia":
                item["note"] = role_raw          # np. "sprawozdawca"
            out.append(item)
        return out
