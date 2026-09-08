"""Testy EliClient (akty prawne z ELI API Sejmu) - bez sieci, na spreparowanym HTML."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orzeczenia.config import EliConfig                              # noqa: E402
from orzeczenia.sources.sejm_eli import EliClient                    # noqa: E402

failures: list[str] = []


def check(label: str, got, expected) -> None:
    ok = got == expected
    print(f"  {'OK' if ok else 'FAIL'}   {label}: {got!r}")
    if not ok:
        failures.append(f"{label}: oczekiwano {expected!r}, jest {got!r}")


class FakeHttp:
    """Zwraca zawsze ten sam z góry ustalony HTML, niezależnie od adresu."""

    def __init__(self, html: str) -> None:
        self.html = html

    def get(self, url: str, ttl: int = 0) -> str:
        return self.html


# Uproszczony fragment strony ELI: PRZED właściwą treścią stoi nawigacyjny
# <div id="toc"> (spis treści z linkami) - dokładnie ta struktura, która na
# żywo (DU 2023/1550, tekst jednolity KPC) sprawiała, że setki wierszy tego
# spisu wypadały PRZED preambułą obwieszczenia, a czytelnik przewijający
# stronę od góry widział ścianę nagłówków zamiast treści.
FAKE_HTML = """
<html><body>
<div id="toc">
  <ul class="toc">
    <li><a href="#a1">Tytuł WSTĘPNY</a>
      <ul><li><a href="#a2">Art. 1.</a></li></ul>
    </li>
  </ul>
</div>
<p>Obwieszczenie w sprawie ogłoszenia tekstu jednolitego.</p>
<p>Na podstawie art. 16 ustawy ogłasza się tekst jednolity.</p>
<h2>Tytuł WSTĘPNY</h2>
<p>Art. 1.</p>
<p>Treść przepisu.</p>
</body></html>
"""

print("\n== EliClient.text(): usuwanie nawigacyjnego spisu tresci (div#toc) z HTML ==")
client = EliClient(EliConfig(), FakeHttp(FAKE_HTML))
text, source = client.text("DU", 2023, 1550, {"textHTML": True})
check("zrodlo to html", source, "html")
check("preambula na POCZATKU tekstu (nie po spisie tresci)",
     text.splitlines()[0], "Obwieszczenie w sprawie ogłoszenia tekstu jednolitego.")
check("link ze spisu tresci ('Art. 1.' jako sam link) NIE powtarza sie przed preambula",
     text.index("Na podstawie art. 16") < text.index("Treść przepisu"), True)
check("prawdziwa tresc przepisu nadal obecna", "Treść przepisu." in text, True)

print("\n" + "=" * 60)
if failures:
    print(f"NIEPOWODZENIA ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("WSZYSTKIE TESTY ELI PRZESZŁY")
