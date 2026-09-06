"""Testy czystych funkcji z parse/pdf_tables.py (scalanie wierszy, HTML) -
bez prawdziwego PDF-a: fałszywy obiekt Table naśladuje tylko `.extract()` i
`.rows[i].bbox`, na których operuje logika."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orzeczenia.parse.pdf_tables import (                            # noqa: E402
    TABLE_SENTINEL, _assign_to_nearest_anchor, _find_anchors, _merge_group,
    _normalize_rows, _page_segments, table_to_html)

failures: list[str] = []


def check(label, got, expected=None, predicate=None):
    ok = predicate(got) if predicate else (got == expected)
    print(f"{'  OK  ' if ok else ' FAIL '} {label}: {got!r}" +
          ("" if ok or predicate else f"  (oczekiwano {expected!r})"))
    if not ok:
        failures.append(label)


class FakeRow:
    def __init__(self, top, bottom):
        self.bbox = (0, top, 100, bottom)


class FakeTable:
    """`rows` to lista (tekst_komorek, top, bottom) - jak realne surowe
    "podwiersze" pdfplumber przed scaleniem w wiersze logiczne."""
    def __init__(self, rows):
        self._cells = [r[0] for r in rows]
        self.rows = [FakeRow(r[1], r[2]) for r in rows]

    def extract(self):
        return self._cells


print("== _normalize_rows: wyrownanie dlugosci wierszy, sprzatanie None/newline ==")
rows, ncols = _normalize_rows([["a", None], ["b", "c\nd", "e"]])
check("dopelnia krotsze wiersze pustymi komorkami", ncols, 3)
check("None -> pusty string (dopelnione do wspolnej dlugosci 3)", rows[0], ["a", "", ""])
check("wewnetrzny newline zamieniony na spacje", rows[1], ["b", "c d", "e"])

print("\n== _find_anchors: kolumna 0 jako 'Lp.' + kolejne liczby ==")
anchors, header_found = _find_anchors([["", "x"], ["Lp.", "y"], ["", "z"], ["1", "w"], ["2", "v"]])
check("naglowek (pierwszy niepusty nie-liczbowy) + dwie liczby -> 3 kotwice", anchors, [1, 3, 4])
check("naglowek wykryty", header_found, True)

anchors2, header_found2 = _find_anchors([["I", "a"], ["II", "b"]])
check("rzymskie numery nie sa cyframi - pierwszy staje sie jedyna (naglowkowa) kotwica; "
     "nieszkodliwe, bo table_to_html i tak nie scala przy mniej niz dwoch kotwicach",
     anchors2, [0])

print("\n== _assign_to_nearest_anchor: przypisanie po odleglosci srodkow, nie po kolejnosci ==")
# Sprawdzone na żywo (DU 2026/1171): fragment komórki wielolinijkowej
# geometrycznie leżący NAD swoim wierszem musi trafić do NASTĘPNEJ kotwicy,
# nie do poprzedzającej (naglowka), mimo że w kolejności wypada wcześniej.
centers = [10, 20, 45, 60]   # kotwica0=idx0(10), "wystajacy" podwiersz=idx2(45), kotwica1=idx3(60)
assign = _assign_to_nearest_anchor(centers, [0, 3])
check("podwiersz blizej dalszej kotwicy trafia do niej, nie do wczesniejszej",
     assign, [0, 0, 1, 1])

print("\n== _merge_group: sklejanie tekstu z kilku podwierszy w jedna komorke ==")
rows_for_merge = [["", "tabletka do"], ["1", "rozgryzania"], ["", "i żucia"]]
merged = _merge_group(rows_for_merge, [0, 1, 2], 2)
check("fragmenty sklejone w kolejnosci, kolumna z liczba tez scalona",
     merged, ["1", "tabletka do rozgryzania i żucia"])

print("\n== table_to_html: pelny przypadek z naglowkiem i 'wystajacym' podwierszem ==")
# Odtwarza uproszczony realny przypadek DU 2026/1171: naglowek "Lp./Wartosc",
# potem wiersz 1 z fragmentem "X" lezacym NAD wlasciwym wierszem.
t = FakeTable([
    (["Lp.", "Kolumna"], 0, 10),
    (["", "X"], 13, 15),
    (["1", "Y"], 15, 25),
])
html = table_to_html(t)
check("naglowek z <thead>", "<thead><tr><th>Lp.</th><th>Kolumna</th></tr></thead>" in html, True)
check("wiersz 1 ma sklejone 'X Y', nie osobno", "<td>1</td><td>X Y</td>" in html, True)
check("caly blok owiniety w kontener przewijalny", html.startswith('<div class="pdf-table-wrap">'), True)

print("\n== table_to_html: bez kolumny licznikowej -> zaden wiersz nie jest scalany ==")
t2 = FakeTable([(["Kategoria", "Kwota"], 0, 10), (["I", "100"], 10, 20), (["II", "200"], 20, 30)])
html2 = table_to_html(t2)
check("brak thead (pierwszy wiersz trafia do tbody jako zwykly wiersz)",
     "<thead>" in html2, False)
check("trzy wiersze danych, bez scalania", html2.count("<tr>"), 3)

print("\n== pusta tabela nie crashuje ==")
check("pusta lista wierszy -> pusty string", table_to_html(FakeTable([])), "")

class FakeBand:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class FakePage:
    """Realny błąd (naprawiony w tej sesji): strona BEZ żadnej tabeli
    dawała dwa identyczne segmenty tekstu zamiast jednego - `_page_segments`
    dokładał zarówno pasmo 'po ostatniej tabeli' (całą stronę, bo `cursor`
    zostawał na 0) JAK I osobny fallback 'brak tabel -> caly tekst strony'.
    Sprawdzone na żywo na DU 2026/1171 - "Minister Rolnictwa i Rozwoju Wsi:
    S. Krajewski" i "WYKAZ PRODUKTÓW..." wychodziły w bazie podwójnie."""
    def __init__(self, text, tables=None):
        self._text = text
        self._tables = tables or []
        self.width = 100
        self.height = 200

    def find_tables(self):
        return self._tables

    def extract_text(self):
        return self._text

    def within_bbox(self, bbox, relative=False):
        return FakeBand(self._text)


print("\n== _page_segments: strona bez tabel daje JEDEN segment tekstu, nie dwa ==")
page = FakePage("Art. 1. Tresc bez zadnej tabeli na tej stronie.")
segs = _page_segments(page)
check("dokladnie jeden segment", len(segs), 1)
check("to segment tekstowy z pelna trescia", segs[0], ("text", "Art. 1. Tresc bez zadnej tabeli na tej stronie."))

print("\n== sentinel jest stala, niepusta ==")
check("TABLE_SENTINEL niepusty", bool(TABLE_SENTINEL), True)

print("\n" + "=" * 62)
if failures:
    print(f"NIEPOWODZENIA ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("Wszystko przeszło.")
