"""Testy izolacji parsera PDF (parse/pdf_worker.py).

Sedno awarii, po której ten moduł powstał: parsowanie siedziało w procesie
serwera, więc jeden akt (M.P. 2021 poz. 235, 102 strony) rozdymał pamięć do
1,9 GB, OOM-killer ubijał kontener i portal restartował się co 45 sekund.
Testy pilnują tego, czego nie widać po samym tekście wyniku - że awaria
potomka wraca jako wyjątek, a nie jako śmierć procesu wywołującego.

Bez sieci i bez prawdziwego Dziennika Ustaw: minimalny PDF budujemy w kodzie.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orzeczenia.parse.pdf_worker import (                            # noqa: E402
    PdfExtractionFailed, pdf_to_text, pdf_to_text_linear)

failures: list[str] = []


def check(label, got, expected=None, predicate=None):
    ok = predicate(got) if predicate else (got == expected)
    wiersz = (f"{'  OK  ' if ok else ' FAIL '} {label}: {got!r}" +
              ("" if ok or predicate else f"  (oczekiwano {expected!r})"))
    # Komunikaty parsera potrafią nieść bajty nie do odtworzenia (stąd U+FFFD),
    # a konsola Windows bywa na cp1250 - wypisanie wyniku nie może wywrócić testu.
    print(wiersz.encode(sys.stdout.encoding or "utf-8", "replace")
                .decode(sys.stdout.encoding or "utf-8", "replace"))
    if not ok:
        failures.append(label)


def maly_pdf(tresc: str = "Art. 1. Test.") -> bytes:
    """Najprostszy poprawny PDF z jedną stroną tekstu - składany ręcznie,
    żeby test nie zależał od żadnej biblioteki do zapisu PDF-ów."""
    strumien = f"BT /F1 12 Tf 72 720 Td ({tresc}) Tj ET".encode("latin-1")
    obiekty = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(strumien)).encode() + b" >>\nstream\n"
        + strumien + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsety = []
    for i, tresc_obiektu in enumerate(obiekty, start=1):
        offsety.append(len(out))
        out += f"{i} 0 obj\n".encode() + tresc_obiektu + b"\nendobj\n"
    start_xref = len(out)
    out += f"xref\n0 {len(obiekty) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsety:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(obiekty) + 1} /Root 1 0 R >>\n"
            f"startxref\n{start_xref}\n%%EOF\n").encode()
    return bytes(out)


print("== parser w osobnym procesie ==")

pdf = maly_pdf("Art. 1. Ustawa wchodzi w zycie.")
tekst = pdf_to_text(pdf)
check("tekst strony wraca z procesu potomnego", tekst,
      predicate=lambda v: "wchodzi w zycie" in v)

check("ścieżka liniowa daje ten sam tekst", pdf_to_text_linear(pdf),
      predicate=lambda v: "wchodzi w zycie" in v)

print("\n== awarie potomka nie przenoszą się na wywołującego ==")

try:
    pdf_to_text(b"to nie jest PDF, tylko zwykly tekst")
    got = "BRAK WYJATKU"
except PdfExtractionFailed as exc:
    got = f"PdfExtractionFailed: {exc}"
except Exception as exc:                                   # noqa: BLE001
    got = f"ZLY WYJATEK: {type(exc).__name__}"
check("śmieci zamiast PDF-a -> PdfExtractionFailed", got,
      predicate=lambda v: v.startswith("PdfExtractionFailed"))

try:
    pdf_to_text(b"")
    got = "BRAK WYJATKU"
except PdfExtractionFailed:
    got = "PdfExtractionFailed"
except Exception as exc:                                   # noqa: BLE001
    got = f"ZLY WYJATEK: {type(exc).__name__}"
check("pusty wsad -> PdfExtractionFailed", got, "PdfExtractionFailed")

# Limit czasu jest ostatnią linią obrony przed plikiem, który zapętla parser.
t0 = time.time()
try:
    pdf_to_text(pdf, timeout_s=0)
    got = "BRAK WYJATKU"
except PdfExtractionFailed as exc:
    got = str(exc)
check("przekroczony limit czasu -> PdfExtractionFailed", got,
      predicate=lambda v: "nie skończył" in v)
check("limit czasu nie wisi dłużej niż trzeba", time.time() - t0,
      predicate=lambda v: v < 30)

print("\n" + "=" * 62)
if failures:
    print("NIEPOWODZENIA:", failures)
    sys.exit(1)
print("PARSER PDF JEST ODIZOLOWANY OD PROCESU WYWOLUJACEGO")
