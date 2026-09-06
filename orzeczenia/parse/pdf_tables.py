"""Wykrywanie i renderowanie tabel z PDF-ów dzienników urzędowych.

`pdfminer` (patrz `orzeczenia/sources/sejm_eli.py:pdf_to_text`) wyciąga sam
liniowy tekst - dla tabel to katastrofa: komórki lądują jako osobne linie w
przypadkowej kolejności (kolumnami zamiast wierszami, albo losowo poprzeplatane
dla komórek wielolinijkowych). `pdfplumber` (ta sama zależność bazowa -
pdfminer.six - więc nie dokłada drugiego silnika PDF) czyta geometrię strony,
więc potrafi poprawnie odtworzyć siatkę wiersz/kolumna.

Dwa realne problemy przy odtwarzaniu WIERSZY z surowych danych pdfplumber:
1. Komórka rozciągnięta na kilka linii (np. "tabletka do\nrozgryzania\ni żucia")
   NIE jest jedną pozycją w `table.extract()` - pdfplumber dzieli ją na tyle
   "surowych" wierszy, ile ma linii, z pustymi komórkami w pozostałych
   kolumnach na tych dodatkowych liniach.
2. Jeżeli sąsiednia kolumna w tym samym wierszu logicznym ma WIĘCEJ linii niż
   inne (bo jej tekst jest dłuższy), pojedyncze wartości z krótszych kolumn
   bywają wizualnie WYŚRODKOWANE względem tamtej wysokiej komórki - część
   surowych "podwierszy" należących do wiersza N fizycznie leży NAD jego
   właściwym wierszem (sprawdzone na żywo, DU 2026/1171: fragment "tabletka do"
   należący do wiersza 1 wypadał wyżej niż wiersz z "1" w kolumnie Lp.).
   Naiwne "wszystko przed pierwszą cyfrą w kolumnie Lp. to nagłówek" myli się
   właśnie w takich przypadkach.

Rozwiązanie: każdy surowy "podwiersz" przypisujemy do NAJBLIŻSZEGO wiersza
kotwiczącego (nagłówek albo wiersz z liczbą w pierwszej kolumnie) po
odległości środków pionowych (`(top+bottom)/2`), nie po kolejności - to
poprawnie łapie podwiersze, które geometrycznie "wystają" poza swój wiersz.
"""
from __future__ import annotations

from html import escape as _esc
from typing import Any

TABLE_SENTINEL = "@@TABLE@@"


def _normalize_rows(raw_rows: list[list[str | None]]) -> tuple[list[list[str]], int]:
    ncols = max(len(r) for r in raw_rows)
    out = []
    for r in raw_rows:
        r = list(r) + [None] * (ncols - len(r))
        out.append([(c or "").replace("\n", " ").strip() for c in r])
    return out, ncols


def _find_anchors(rows: list[list[str]]) -> tuple[list[int], bool]:
    """Wiersze "kotwiczące" - pierwszy niepusty, NIE-liczbowy wpis w kolumnie 0
    to nagłówek (typowo "Lp."), każdy kolejny liczbowy wpis to nowy wiersz
    danych. Tabele bez takiej kolumny licznikowej (np. proste dwukolumnowe
    zestawienia) po prostu nie dają wystarczająco kotwic - patrz `table_to_html`,
    wtedy nic nie scalamy, bo nie ma po czym."""
    anchors: list[int] = []
    header_found = False
    for i, r in enumerate(rows):
        c0 = r[0].strip() if r[0] else ""
        if not c0:
            continue
        if c0.isdigit():
            anchors.append(i)
        elif not header_found:
            anchors.append(i)
            header_found = True
    return anchors, header_found


def _assign_to_nearest_anchor(centers: list[float], anchor_idxs: list[int]) -> list[int]:
    anchor_centers = [centers[i] for i in anchor_idxs]
    return [min(range(len(anchor_centers)), key=lambda k: abs(anchor_centers[k] - c))
            for c in centers]


def _merge_group(rows: list[list[str]], idxs: list[int], ncols: int) -> list[str]:
    merged = [""] * ncols
    for i in sorted(idxs):
        for col in range(ncols):
            v = rows[i][col]
            if v:
                merged[col] = f"{merged[col]} {v}".strip() if merged[col] else v
    return merged


def table_to_html(table: Any) -> str:
    """`table` to obiekt `pdfplumber.table.Table` (z `page.find_tables()`)."""
    raw = table.extract()
    if not raw:
        return ""
    rows, ncols = _normalize_rows(raw)
    centers = [(r.bbox[1] + r.bbox[3]) / 2 for r in table.rows]

    anchors, header_found = _find_anchors(rows)
    if len(anchors) < 2:
        # Brak wyraźnej kolumny licznikowej (np. proste zestawienie bez "Lp.") -
        # każdy surowy wiersz pdfplumber to już jedna linia tabeli, nie ma
        # czego scalać.
        header_row, body_rows = None, rows
    else:
        assignment = _assign_to_nearest_anchor(centers, anchors)
        groups: dict[int, list[int]] = {}
        for i, g in enumerate(assignment):
            groups.setdefault(g, []).append(i)
        merged = [_merge_group(rows, groups[g], ncols) for g in sorted(groups)]
        header_row, body_rows = (merged[0], merged[1:]) if header_found else (None, merged)

    parts = ['<div class="pdf-table-wrap"><table class="pdf-table">']
    if header_row and any(header_row):
        parts.append("<thead><tr>" + "".join(f"<th>{_esc(c)}</th>" for c in header_row) +
                     "</tr></thead>")
    parts.append("<tbody>")
    for r in body_rows:
        if any(r):
            parts.append("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def _page_segments(page: Any) -> list[tuple[str, Any]]:
    """Dzieli stronę na pasma tekstu i tabel, w kolejności pionowej - dzięki
    temu tabela ląduje w tekście DOKŁADNIE tam, gdzie jest w oryginale, a nie
    np. na końcu dokumentu."""
    tables = sorted(page.find_tables(), key=lambda t: t.bbox[1])
    if not tables:
        # Strona bez tabel - cały jej tekst to jeden segment, bez cięcia na
        # pasma (te istnieją tylko po to, żeby okalać tabele).
        return [("text", page.extract_text() or "")]
    segments: list[tuple[str, Any]] = []
    cursor = 0.0
    for t in tables:
        top = t.bbox[1]
        if top > cursor + 2:
            band = page.within_bbox((0, cursor, page.width, top), relative=False)
            txt = band.extract_text() or ""
            if txt.strip():
                segments.append(("text", txt))
        segments.append(("table", t))
        cursor = t.bbox[3]
    if cursor < page.height - 2:
        band = page.within_bbox((0, cursor, page.width, page.height), relative=False)
        txt = band.extract_text() or ""
        if txt.strip():
            segments.append(("text", txt))
    return segments


def pdf_to_text_with_tables(data: bytes) -> str:
    """Jak `pdf_to_text()` w `sources/sejm_eli.py`, ale tabele wykrywa po
    geometrii i wstawia jako gotowe znaczniki `<table>` - każdy blok tabeli
    poprzedzony `TABLE_SENTINEL`, żeby `clean_pdf_text()` (patrz tam) mógł go
    rozpoznać i zostawić w spokoju zamiast próbować składać jako zwykły
    akapit, a szablon (`akt.html`) mógł wyrenderować jako HTML, nie zwykły
    tekst.

    UWAGA - `pdfplumber`/`pypdfium2` potrafią zostawiać sporo pamięci między
    kolejnymi otwarciami dokumentu w tym samym, długo działającym procesie
    (import wsadowy przechodzi przez setki/tysiące PDF-ów pod rząd) - bez
    jawnego `flush_cache()` per strona i `gc.collect()` po każdym dokumencie
    proces urósł na żywo do ~3 GB i padł zabity przez OOM (sprawdzone na
    produkcji przy migracji ~6800 aktów). Oba wywołania są tanie i nie
    zmieniają wyniku - tylko szybciej oddają pamięć."""
    import gc
    import pdfplumber
    from io import BytesIO

    out: list[str] = []
    with pdfplumber.open(BytesIO(data)) as pdf:
        for page in pdf.pages:
            for kind, payload in _page_segments(page):
                if kind == "text":
                    out.append(payload)
                else:
                    out.append(TABLE_SENTINEL + table_to_html(payload))
            page.flush_cache()
    gc.collect()
    return "\n\n".join(out)
