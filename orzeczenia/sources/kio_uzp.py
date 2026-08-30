"""Baza orzeczeń Krajowej Izby Odwoławczej (orzeczenia.uzp.gov.pl) - na żywo.

  lista:     /Home/Search?Phrase=&Fle=1&SCnt=1&Sign=&Dt=DD-MM-RRRR - DD-MM-RRRR&Pg=N
  metryka:   /Home/Details/{id}
  treść:     /Home/ContentHtml/{id}?Kind=KIO   (czysty HTML, nie PDF)

Serwis oddaje listę wyników tylko żądaniom wyglądającym na nawigację - nagłówki
Sec-Fetch-* ustawia PoliteClient.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from bs4 import BeautifulSoup

from ..parse.common import (KIO_SIG_RE, clean_person, detect_doc_type, extract_panel, html_text,
                            normalize_person, normalize_signature, parse_date, sort_panel,
                            split_sentencja_uzasadnienie, squash, strip_accents)
from .base import Hit, Query

log = logging.getLogger("orzecznik.kio")

PER_PAGE = 10


def _dmy(iso: str) -> str:
    """'2026-07-01' -> '01-07-2026' (format oczekiwany przez wyszukiwarkę UZP)."""
    try:
        y, m, d = iso.split("-")
        return f"{d}-{m}-{y}"
    except ValueError:
        return ""


@dataclass
class KioSource:
    cfg: Any
    http: Any
    key: str = "kio"
    # Wyszukiwarka UZP zna tylko datę wydania orzeczenia.
    supports_publication_date: bool = False

    @property
    def label(self) -> str:
        return self.cfg.label or "KIO"

    # ------------------------------------------------------------------
    def search_url(self, q: Query, page: int) -> str:
        params = {
            "Phrase": q.phrase or "",
            "Fle": "1",        # uwzględnij odmianę słów
            "SCnt": "1",       # szukaj również w treści
            "Sign": q.signature or "KIO",
            "CountStats": "True",
            "Pg": str(page),
        }
        # KIO filtruje tylko po dacie wydania orzeczenia
        if q.date_field != "publication" and (q.date_from or q.date_to):
            a = _dmy(q.date_from) or "01-01-2007"
            b = _dmy(q.date_to) or "31-12-2099"
            params["Dt"] = f"{a} - {b}"
        return f"{self.cfg.base_url}/Home/Search?" + urlencode(params, quote_via=quote)

    def details_url(self, doc_id: str) -> str:
        return f"{self.cfg.base_url}/Home/Details/{doc_id}"

    def content_url(self, doc_id: str) -> str:
        return f"{self.cfg.base_url}/Home/ContentHtml/{doc_id}?Kind=KIO&flection=0"

    def pdf_url(self, doc_id: str) -> str:
        return f"{self.cfg.base_url}/Home/PdfContent/{doc_id}?Kind=KIO"

    # ------------------------------------------------------------------
    def search(self, q: Query, page: int = 1) -> tuple[list[Hit], int]:
        # KIO nie wspiera filtrów po sędzim / haśle / przepisie - jeśli użytkownik
        # ich użył, ten serwis nie ma czego zwrócić i pomijamy go świadomie.
        if q.judge or q.thematic or q.legal_basis:
            return [], 0
        html = self.http.get(self.search_url(q, page))
        return self.parse_results(html), self.parse_count(html)

    @staticmethod
    def parse_count(html: str) -> int:
        if m := re.search(r"Liczba znalezionych dokument[^:]*:\s*([\d\s ]+)", html):
            digits = re.sub(r"\D", "", m.group(1))
            return int(digits) if digits else 0
        if m := re.search(r"total=(\d+)", html):
            return int(m.group(1))
        return 0

    def parse_results(self, html: str) -> list[Hit]:
        soup = BeautifulSoup(html, "lxml")
        out: list[Hit] = []
        seen: set[str] = set()
        for a in soup.select('a[href*="/Home/Details/"]'):
            m = re.search(r"/Home/Details/(\d+)", a["href"])
            if not m or m.group(1) in seen:
                continue
            doc_id = m.group(1)
            seen.add(doc_id)
            block = a.find_parent(class_=re.compile(r"search-list-item|result|row|item")) or a.parent
            text = squash(block.get_text(" ", strip=True)) if block else ""
            sig = None
            if sm := re.search(r"Sygnatura:\s*(KIO[^ ]*(?:\s*\d+/\d+)?)", text):
                sig = normalize_signature(sm.group(1))
            elif sm := KIO_SIG_RE.search(text):
                sig = normalize_signature(sm.group(0))
            dm = re.search(r"Data wydania:?\s*(\d{2}-\d{2}-\d{4})", text)
            tm = re.search(r"Rodzaj dokumentu:\s*([a-ząćęłńóśźż ]+?)\s+Sygnatura", text)
            out.append(Hit(
                source=self.key, doc_id=doc_id, signature=sig,
                doc_type=squash(tm.group(1)) if tm else None,
                court="Krajowa Izba Odwoławcza",
                judgment_date=parse_date(dm.group(1)) if dm else None,
                source_url=self.details_url(doc_id),
            ))
        return out

    # ------------------------------------------------------------------
    def document(self, doc_id: str) -> dict[str, Any]:
        ttl = self.http.cache_cfg.document_ttl_seconds
        details = self.http.get(self.details_url(doc_id), ttl=ttl)
        try:
            content = self.http.get(self.content_url(doc_id), ttl=ttl)
        except Exception as exc:
            log.warning("KIO %s: brak treści HTML (%s)", doc_id, exc)
            content = ""
        return self.parse_document(doc_id, details, content)

    def parse_document(self, doc_id: str, details_html: str, content_html: str) -> dict[str, Any]:
        meta = self._parse_details(details_html)
        full_text = html_text(BeautifulSoup(content_html, "lxml").body) if content_html else None

        signature = meta.get("sygnatura") or meta.get("_h2")
        if signature:
            m = KIO_SIG_RE.search(signature)
            signature = normalize_signature(m.group(0) if m else signature)
        if not signature and full_text:
            m = KIO_SIG_RE.search(full_text[:600])
            signature = normalize_signature(m.group(0)) if m else None

        sent, uzas = split_sentencja_uzasadnienie(full_text)
        panel_roles: list[dict[str, str]] = []
        seen: set[str] = set()
        chair = clean_person(meta.get("przewodniczacy") or meta.get("przewodniczący"))
        if chair:
            seen.add(normalize_person(chair))
            panel_roles.append({"name": chair, "role": "przewodniczący"})
        # Pełny skład (3-osobowy) jest tylko w treści: "Przewodniczący: X  Członkowie: Y, Z"
        for item in extract_panel(full_text):
            if normalize_person(item["name"]) not in seen:
                seen.add(normalize_person(item["name"]))
                panel_roles.append(item)

        return {
            "source": self.key, "source_label": self.label, "doc_id": doc_id,
            "signature": signature,
            "judgment_date": parse_date(meta.get("data wydania rozstrzygniecia")
                                        or meta.get("data wydania")),
            "publication_date": None, "valid_from_date": None,
            "doc_type": detect_doc_type(meta.get("rodzaj dokumentu"), (full_text or "")[:400]),
            "doc_type_raw": meta.get("rodzaj dokumentu"), "doc_types": [],
            "court": meta.get("organ wydajacy") or "Krajowa Izba Odwoławcza",
            "court_level": "KIO", "division": None,
            "chairman": chair, "judges": sort_panel(panel_roles),
            "thematic": [t for t in re.split(r"\s*\|\s*|\s*,\s*",
                                             meta.get("_zagadnienia") or "") if t],
            "legal_basis": meta.get("_przepisy"),
            "importance": None,
            "outcome": meta.get("_rozstrzygniecie"),
            "purchaser": meta.get("zamawiajacy") or meta.get("zamawiający"),
            "procedure": meta.get("tryb postepowania"),
            "sentencja": sent, "uzasadnienie": uzas, "full_text": full_text,
            "source_url": self.details_url(doc_id),
            "pdf_url": self.pdf_url(doc_id),
            "metryka": meta,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_details(html: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "lxml")
        out: dict[str, str] = {}
        h2 = soup.select_one("#pageContent h2.section-title")
        if h2:
            out["_h2"] = squash(h2.get_text(" ", strip=True).replace("Pobierz metrykę PDF", ""))

        for lab in soup.select(".details-metrics label"):
            holder = lab.parent
            if holder is None or len(holder.select("label")) != 1:
                continue        # pomijamy kontenery zbiorcze (np. całą kolumnę)
            label = strip_accents(squash(lab.get_text()).rstrip(":").lower())
            head = squash(lab.get_text())
            value = squash(holder.get_text(" ", strip=True))
            if value.startswith(head):
                value = value[len(head):]
            if label and (value := value.strip(" :")):
                out.setdefault(label, value)

        # "Sygnatura akt / Sposób rozstrzygnięcia" jest listą <li>
        for div in soup.select(".details-metrics div"):
            lab = div.find("label")
            if lab and "sygnatura akt" in strip_accents(lab.get_text().lower()):
                sigs, outcomes = [], []
                for it in (squash(li.get_text(" ", strip=True)) for li in div.select("li")):
                    left, _, right = it.partition(" / ")
                    sigs.append(squash(left))
                    if right:
                        outcomes.append(squash(right))
                if sigs:
                    out["sygnatura"] = ", ".join(sigs)
                    out["_rozstrzygniecie"] = "; ".join(dict.fromkeys(outcomes))

        for b in soup.select("b"):
            head = strip_accents(squash(b.get_text()).lower())
            nxt = b.find_next("p")
            val = squash(nxt.get_text(" ", strip=True)) if nxt else ""
            if head.startswith("kluczowe przepisy"):
                out["_przepisy"] = val
            elif head.startswith("zagadnienia"):
                out["_zagadnienia"] = val
        return out
