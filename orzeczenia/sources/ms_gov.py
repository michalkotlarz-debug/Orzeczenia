"""Portal Orzeczeń Sądów Powszechnych (orzeczenia.ms.gov.pl) - odpytywany na żywo.

Adres wyszukiwarki zaawansowanej to 18 segmentów ścieżki (ustalone empirycznie
przez wysłanie formularza ze znacznikami); '$N' oznacza "parametr pominięty":

  /search/advanced/{1}/{2}/.../{18}

   1 fraza             7 (nieużywane)      13 podstawa prawna
   2 sygnatura         8 data orzeczenia od 14 (nieużywane)
   3 kod sądu          9 data orzeczenia do 15 (nieużywane)
   4 kod wydziału     10 sędzia             16 pole sortowania
   5 kod okręgu       11 rola sędziego      17 kierunek sortowania
   6 (nieużywane)     12 hasło tematyczne   18 numer strony

Strona wyników: 10 pozycji. Metryka: /details/$N/{id}. Treść: /content/$N/{id}.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from bs4 import BeautifulSoup

from ..http import RateLimited, SourceUnavailable
from ..parse.common import (clean_person, combine_wyrok_uzasadnienie, court_level,
                            detect_doc_type, detect_doc_types, extract_panel, html_text,
                            is_uzasadnienie_pair, normalize_person, normalize_signature,
                            parse_date, sort_panel, split_sentencja_uzasadnienie, squash,
                            strip_accents)
from .base import Hit, Query

log = logging.getLogger("orzecznik.ms")

N = "$N"
PER_PAGE = 10

# Poprzedza cały dostępny cyfrowy zbiór portalu (sprawdzone na żywo: to samo
# co brak dolnego ograniczenia w ogóle, gdyby portal na to pozwalał - patrz
# EARLIEST_DATE w obserwator.py/archiwum.py, ten sam próg).
EARLIEST_DATE = "2000-01-01"

SORTS = {
    "relevance": ("score", "descending"),
    "date_desc": ("data", "descending"),
    "date_asc": ("data", "ascending"),
    "pub_desc": ("datapublikacji", "descending"),
}

# Tapestry nie używa kodowania procentowego w ścieżce - ma własne: znak zapisuje
# jako '$' + 4 cyfry szesnastkowe. Spacja to $0020, ukośnik $002f. Zwykłe %2F
# w sygnaturze ("II C 438/25") portal odrzuca błędem HTTP 400.
_SAFE_SEG = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_")


def _seg(value: str | None) -> str:
    """Segment ścieżki w kodowaniu Tapestry; pusta wartość to '$N'."""
    v = (value or "").strip()
    if not v:
        return N
    return "".join(c if c in _SAFE_SEG else f"${ord(c):04x}" for c in v)


@dataclass
class MsSource:
    cfg: Any                # SourceConfig
    http: Any               # PoliteClient
    key: str = "ms"
    # Portal Orzeczeń podaje zarówno datę orzeczenia, jak i datę publikacji.
    supports_publication_date: bool = True

    @property
    def label(self) -> str:
        return self.cfg.label or "Sądy powszechne"

    # ------------------------------------------------------------------
    def search_url(self, q: Query, page: int) -> str:
        sort_field, sort_dir = SORTS.get(q.sort, SORTS["relevance"])
        date_from, date_to = q.date_from, q.date_to
        # Portal filtruje wyłącznie po dacie ORZECZENIA. Gdy użytkownik pyta
        # o datę publikacji, bierzemy szeroki zakres i sortujemy po publikacji,
        # a dokładne odsianie robimy już u nas (Registry._post_filter).
        if q.date_field == "publication" and (date_from or date_to):
            date_from, date_to = "", ""
            sort_field, sort_dir = "datapublikacji", "descending"
        if q.is_empty():
            # Bez żadnego kryterium (włącznie z datami) portal nie zwraca nic -
            # trzeba podać jakiś zakres. "2000-01-01" poprzedza cały cyfrowy
            # zbiór (sprawdzone na żywo: daje 464 809 - tyle samo co brak
            # dolnego ograniczenia w ogóle, gdyby portal na to pozwalał), więc
            # to praktycznie "od początku", nie sztuczne obcięcie do ostatnich
            # N lat.
            date_from = EARLIEST_DATE
            date_to = date.today().isoformat()
            sort_field, sort_dir = "datapublikacji", "descending"

        parts = [
            _seg(q.phrase), _seg(q.signature), N, N, N, N, N,
            _seg(date_from), _seg(date_to),
            _seg(q.judge), N, _seg(q.thematic), _seg(q.legal_basis), N, N,
            sort_field, sort_dir, str(page),
        ]
        return f"{self.cfg.base_url}/search/advanced/" + "/".join(parts)

    def details_url(self, doc_id: str) -> str:
        return f"{self.cfg.base_url}/details/{N}/{doc_id}"

    def content_url(self, doc_id: str) -> str:
        return f"{self.cfg.base_url}/content/{N}/{doc_id}"

    # ------------------------------------------------------------------
    def search(self, q: Query, page: int = 1) -> tuple[list[Hit], int]:
        html = self.http.get(self.search_url(q, page))
        hits = self._filter_and_merge_hits(self.parse_results(html))
        return hits, self.parse_count(html)

    def _has_content(self, doc_id: str) -> bool:
        """Sprawdza (i buforuje przez PoliteClient - kolejne otwarcie tej samej
        pozycji jest już darmowe), czy /content/ dla tej pozycji w ogóle coś
        zwraca. Część orzeczeń portal trwale odmawia wydać (`IX U 1515/12`),
        mimo że pojawiają się na liście wyników - stamtąd samą tego nie widać."""
        try:
            self.http.get(self.content_url(doc_id), ttl=self.http.cache_cfg.document_ttl_seconds)
            return True
        except RateLimited:
            raise
        except SourceUnavailable:
            return False

    def _filter_and_merge_hits(self, hits: list[Hit]) -> list[Hit]:
        """1) odsiewa pozycje bez treści (`_has_content`) - nie prezentujemy
        orzeczeń, których nie da się otworzyć; 2) dla tego, co zostało, scala
        pary wyrok+uzasadnienie opublikowane jako dwa osobne dokumenty tej samej
        sprawy (ta sama sygnatura/sąd/data orzeczenia/data publikacji - patrz
        `parse.common.is_uzasadnienie_pair`, ten sam mechanizm co przy imporcie
        w `obserwator.py`); 3) wynik trafia do prezentacji na stronie.

        Sprawdzone na żywo (dwa różne przypadki): (a) portal potrafi oddać
        HTTP 400 na /content/ dla KAŻDEJ pozycji strony naraz, także dla
        dokumentów z tego samego dnia - to wygląda na chwilową blokadę/
        przeciążenie po większej liczbie zapytań; (b) zdarza się też, że CAŁA
        pierwsza strona wyników dla konkretnego, wąskiego zapytania trwale
        składa się z samych orzeczeń bez treści (np. jeden sąd/rocznik ze
        specyficznym problemem archiwizacji) - tu "spróbuj później" nic nie
        da. Nie da się tego odróżnić z zewnątrz, więc komunikat jest celowo
        neutralny co do przyczyny. W obu przypadkach chodzi o to samo: żeby
        całkowity brak treści na całej (niepustej) stronie wyników zgłosić
        jak każdą inną niedostępność serwisu, zamiast po cichu zwracać pustą
        listę (fałszywe "brak wyników")."""
        verified = [h for h in hits if self._has_content(h.doc_id)]
        if hits and not verified:
            raise SourceUnavailable(
                "portal nie oddał treści żadnej z pozycji na tej stronie wyników - "
                "spróbuj innych filtrów, kolejnej strony, albo wróć za jakiś czas")

        groups: dict[tuple, list[Hit]] = {}
        order: list[tuple] = []
        for h in verified:
            key = (h.signature, h.court, h.judgment_date, h.publication_date)
            if key not in groups:
                order.append(key)
            groups.setdefault(key, []).append(h)

        out: list[Hit] = []
        for key in order:
            group = groups[key]
            if len(group) == 2 and is_uzasadnienie_pair(group[0].doc_type, group[1].doc_type):
                a, b = group
                wyrok, uzas = (a, b) if a.doc_type != "uzasadnienie" else (b, a)
                out.append(replace(wyrok, excerpt=wyrok.excerpt or uzas.excerpt))
            else:
                out.extend(group)
        return out

    @staticmethod
    def parse_count(html: str) -> int:
        soup = BeautifulSoup(html, "lxml")
        el = soup.select_one("#sorting .big_number, .big_number")
        digits = re.sub(r"\D", "", el.get_text()) if el else ""
        return int(digits) if digits else 0

    def parse_results(self, html: str) -> list[Hit]:
        soup = BeautifulSoup(html, "lxml")
        out: list[Hit] = []
        for block in soup.select("div.single_result"):
            a = block.select_one("h4 a[href]")
            if not a:
                continue
            doc_id = a["href"].rstrip("/").split("/")[-1]
            if not doc_id or doc_id == N:
                continue
            jd = pd = court = doc_type = None
            for p in block.select(".title p"):
                t = squash(p.get_text())
                low = strip_accents(t.lower())
                if low.startswith("data orzeczenia"):
                    jd = parse_date(t)
                elif low.startswith("data publikacji"):
                    pd = parse_date(t)
                elif low.startswith("sad "):
                    court = t
                elif not doc_type and len(t) < 40:
                    doc_type = detect_doc_type(t)
            quote_el = block.select_one(".excerpt blockquote")
            out.append(Hit(
                source=self.key, doc_id=doc_id,
                signature=normalize_signature(squash(a.get_text())),
                doc_type=doc_type, court=court,
                judgment_date=jd, publication_date=pd,
                excerpt=squash(quote_el.get_text()) if quote_el else None,
                source_url=self.content_url(doc_id),
            ))
        return out

    # ------------------------------------------------------------------
    def _sibling_hit(self, doc_id: str, own_type: str | None, signature: str | None,
                     court: str | None, judgment_date: str | None,
                     publication_date: str | None) -> Hit | None:
        """Szuka - przez wyszukiwanie po samej sygnaturze - drugiej połowy pary
        wyrok+uzasadnienie opublikowanej jako osobny dokument tej samej sprawy
        (ta sama sygnatura/sąd/data orzeczenia/data publikacji). Używane przez
        `document()`, gdy własna treść jest niedostępna albo gdy ten dokument
        sam jest tylko uzasadnieniem."""
        if not signature or not court or not judgment_date:
            return None
        try:
            html = self.http.get(self.search_url(Query(signature=signature), 1))
        except (RateLimited, SourceUnavailable):
            return None
        for h in self.parse_results(html):
            if h.doc_id == doc_id or h.court != court:
                continue
            if h.judgment_date != judgment_date or h.publication_date != publication_date:
                continue
            if is_uzasadnienie_pair(own_type, h.doc_type):
                return h
        return None

    def document(self, doc_id: str) -> dict[str, Any]:
        ttl = self.http.cache_cfg.document_ttl_seconds
        details = self.http.get(self.details_url(doc_id), ttl=ttl)
        meta = self._parse_details(details)
        title = meta.get("_title", "")
        own_type = detect_doc_type(self._type_from_title(title))
        signature = meta.get("sygnatura") or self._signature_from_title(title)
        court = meta.get("sad") or meta.get("sąd")
        judgment_date = parse_date(meta.get("data orzeczenia"))
        publication_date = parse_date(meta.get("data publikacji"))

        # Dla części orzeczeń (najczęściej starsze, ale nie tylko) portal
        # trwale zwraca błąd na /content/, mimo że /details/ działa poprawnie.
        try:
            own_content: str | None = self.http.get(self.content_url(doc_id), ttl=ttl)
        except SourceUnavailable:
            own_content = None

        # 1) własnej treści brak -> szukamy drugiej połowy pary; 2) własna
        # treść jest, ale to samo uzasadnienie -> dociągamy wyrok tej samej
        # sprawy, żeby pokazać całość od razu (zamiast dwóch osobnych stron).
        sib = None
        if own_content is None or own_type == "uzasadnienie":
            sib = self._sibling_hit(doc_id, own_type, signature, court,
                                    judgment_date, publication_date)

        sib_details = sib_content = None
        if sib is not None:
            try:
                sib_details = self.http.get(self.details_url(sib.doc_id), ttl=ttl)
                sib_content = self.http.get(self.content_url(sib.doc_id), ttl=ttl)
            except SourceUnavailable:
                sib_details = sib_content = None

        if own_content is None and sib_content is None:
            # Ani ten dokument, ani jego ewentualna druga połowa nie mają
            # treści - zgodnie z wytycznymi w ogóle go nie prezentujemy.
            raise SourceUnavailable(f"{doc_id}: treść niedostępna w źródle")

        if own_content is None:
            return self.parse_document(sib.doc_id, sib_details, sib_content)

        doc = self.parse_document(doc_id, details, own_content)
        if sib_content is not None:
            sib_doc = self.parse_document(sib.doc_id, sib_details, sib_content)
            wyrok, uzas = (doc, sib_doc) if own_type != "uzasadnienie" else (sib_doc, doc)
            return combine_wyrok_uzasadnienie(wyrok, uzas)
        return doc

    def parse_document(self, doc_id: str, details_html: str, content_html: str) -> dict[str, Any]:
        from ..http import looks_blocked
        if looks_blocked(details_html) or looks_blocked(content_html):
            raise ValueError(f"{doc_id}: zamiast orzeczenia zwrócono stronę blokady/CAPTCHA")
        meta = self._parse_details(details_html)
        if not meta.get("_title") and not meta.get("sygnatura"):
            raise ValueError(f"{doc_id}: brak metryki - to nie wygląda na orzeczenie")
        body = self._parse_content(content_html)

        title = meta.pop("_title", "") or body.pop("_title", "")
        title_types = self._type_from_title(title)
        doc_types = detect_doc_types(title_types, (body.get("full_text") or "")[:400])
        signature = meta.get("sygnatura") or self._signature_from_title(title)
        court = meta.get("sad") or meta.get("sąd")
        sent, uzas = split_sentencja_uzasadnienie(body.get("full_text"))

        panel_roles: list[dict[str, str]] = []
        seen: set[str] = set()
        for label, role in (("przewodniczacy", "przewodniczący"),
                            ("przewodniczący", "przewodniczący"),
                            ("sedziowie", "sędzia"), ("sędziowie", "sędzia"),
                            ("sedzia", "sędzia"), ("sędzia", "sędzia"),
                            ("protokolant", "protokolant")):
            for part in re.split(r"[,;]|\n", meta.get(label) or ""):
                name = clean_person(part)
                if name and normalize_person(name) not in seen:
                    seen.add(normalize_person(name))
                    panel_roles.append({"name": name, "role": role})
        if not panel_roles:
            for item in extract_panel(body.get("full_text")):
                if normalize_person(item["name"]) not in seen:
                    seen.add(normalize_person(item["name"]))
                    panel_roles.append(item)

        thematic = [t for t in re.split(r"\s*,\s*", meta.get("hasla tematyczne")
                                        or meta.get("hasła tematyczne") or "") if t]
        return {
            "source": self.key, "source_label": self.label, "doc_id": doc_id,
            "signature": signature,
            "judgment_date": parse_date(meta.get("data orzeczenia")),
            "publication_date": parse_date(meta.get("data publikacji")),
            "valid_from_date": parse_date(meta.get("data uprawomocnienia")),
            "doc_type": doc_types[0] if doc_types else None,
            "doc_type_raw": title_types,
            "doc_types": doc_types,
            "court": court, "court_level": court_level(court),
            "division": meta.get("wydzial") or meta.get("wydział"),
            "chairman": next((p["name"] for p in panel_roles
                              if p["role"] == "przewodniczący"), None),
            "judges": sort_panel(panel_roles),
            "thematic": thematic,
            "legal_basis": meta.get("podstawa prawna"),
            "importance": meta.get("istotnosc") or meta.get("istotność"),
            "outcome": None, "purchaser": None,
            "sentencja": sent, "uzasadnienie": uzas, "full_text": body.get("full_text"),
            "source_url": self.content_url(doc_id),
            "metryka": meta,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _type_from_title(title: str) -> str | None:
        """"IV Ka 352/26 - wyrok Sąd Okręgowy ..." -> "wyrok".
        Przecinek jest dozwolony: "zarządzenie, uzasadnienie"."""
        m = re.search(r"-\s*([a-ząćęłńóśźż,\s]{3,60}?)\s+S[ąa]d", title or "")
        return squash(m.group(1)) if m else None

    @staticmethod
    def _signature_from_title(title: str) -> str | None:
        m = re.match(r"\s*([^-]{2,40}?)\s+-\s", title or "")
        return normalize_signature(m.group(1)) if m else None

    @staticmethod
    def _parse_details(html: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "lxml")
        out: dict[str, str] = {}
        h2 = soup.select_one("#content h2")
        out["_title"] = squash(h2.get_text()) if h2 else ""
        for dt in soup.select("dl dt"):
            label = strip_accents(squash(dt.get_text()).rstrip(":").lower())
            dd = dt.find_next_sibling("dd")
            if dd is not None:
                out[label] = squash(dd.get_text(" ", strip=True))
        rec = soup.select_one(".records_number")
        if rec:
            txt = squash(rec.get_text(" ", strip=True))
            if m := re.search(r"Opublikowa[łl]\(a\):\s*([^;]+?)\s*Podmiot", txt):
                out["publikujacy"] = squash(m.group(1))
        return out

    @staticmethod
    def _parse_content(html: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "lxml")
        h2 = soup.select_one("#content > h2")
        title = squash(h2.get_text()) if h2 else ""
        node = soup.select_one(".single_wrapper .single_result") or soup.select_one("#content")
        if node is None:
            return {"_title": title, "full_text": None}
        for bad in node.select("ul.tabs, script, style"):
            bad.decompose()
        return {"_title": title, "full_text": html_text(node) or None}
