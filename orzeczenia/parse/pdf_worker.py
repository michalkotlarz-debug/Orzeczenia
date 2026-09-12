"""Wyciąganie tekstu z PDF-a W OSOBNYM PROCESIE, z twardym limitem pamięci.

Po co osobny proces. Parsowanie PDF-a odbywało się dotąd wewnątrz serwera WWW,
więc pamięć parsera była pamięcią całej aplikacji: jeden nietypowy załącznik
(M.P. 2021 poz. 235 - 102 strony) rozdymał proces do 1,9 GB, OOM-killer ubijał
kontener, a pętla importu brała po restarcie ten sam akt od nowa. Portal
restartował się co 45 sekund i przez ten czas nie wchodził do bazy ani jeden
dokument - ani akt, ani orzeczenie. Sam wyciek jest już usunięty (patrz
`pdf_tables.pdf_to_text_with_tables`), ale zależy nam, żeby ŻADEN przyszły plik
nie mógł powtórzyć tej historii: proces potomny dostaje limit adresowy, a gdy
go przekroczy albo utknie, ginie sam i wraca stąd jako zwykły wyjątek.

Dodatkowo trzymanie parsowania poza procesem serwera zdejmuje z niego kilkunasto-
sekundowe odcinki liczenia CPU, które blokowały odpowiedzi dla użytkowników.

Protokół: bajty PDF-a na wejściu procesu, gotowy tekst (UTF-8) na wyjściu,
diagnostyka na stderr. Kod wyjścia 0 = tekst, cokolwiek innego = nie udało się.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from io import BytesIO, StringIO

log = logging.getLogger("orzecznik.pdf")

# Limit przestrzeni adresowej procesu potomnego. Zmierzone zapotrzebowanie
# najgrubszego znanego aktu po naprawie wycieku to ~220 MB, najgrubszego
# w ogóle (13 MB, 311 stron) ~130 MB ścieżką liniową - 1200 MB to zapas rzędu
# pięciokrotności, a jednocześnie bezpiecznie poniżej limitu kontenera (2 GB),
# więc nawet gdy potomek uderzy w sufit, serwer WWW tego nie odczuje.
MEM_LIMIT_MB = 1200
# Najdłuższy zmierzony przebieg to ~95 s (ścieżka liniowa, 102 strony).
TIMEOUT_S = 600


class PdfExtractionFailed(Exception):
    """Nie udało się wydobyć tekstu - wywołujący zapisuje akt bez treści."""


def pdf_to_text_linear(data: bytes) -> str:
    """Sam liniowy tekst, strona po stronie. Bez wykrywania tabel - ścieżka
    awaryjna, gdy geometria zawiedzie.

    Dlaczego nie `extract_text()` na całym dokumencie: pdfminer zamienia każdy
    znak w osobny obiekt Pythona (pozycja, wymiary, czcionka), a przy jednym
    wywołaniu na cały plik trzyma je wszystkie naraz i dokłada do tego cache
    czcionek. Na M.P. 2021 poz. 414 (311 stron, 775 czcionek, 13 MB) dawało to
    ponad 2,8 GB. Tutaj po każdej stronie zostaje już tylko jej gotowy tekst,
    a obiekty idą do wyrzucenia - w pamięci siedzi jedna strona zamiast
    trzystu."""
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


def pdf_to_text(data: bytes, *, mem_limit_mb: int = MEM_LIMIT_MB,
                timeout_s: int = TIMEOUT_S) -> str:
    """Tekst z PDF-a razem z tabelami, policzony w osobnym procesie.

    Podnosi `PdfExtractionFailed`, gdy potomek przekroczy pamięć, czas albo
    padnie - nigdy nie przenosi awarii parsera na proces wywołujący."""
    try:
        wynik = subprocess.run(
            [sys.executable, "-m", "orzeczenia.parse.pdf_worker"],
            input=data, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise PdfExtractionFailed(f"parser nie skończył w {timeout_s} s") from None
    except OSError as exc:                       # nie da się uruchomić potomka
        raise PdfExtractionFailed(f"nie udało się uruchomić parsera: {exc}") from None

    if wynik.returncode != 0:
        powod = (wynik.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        # Ujemny kod to sygnał (np. -9 = zabity przez jądro przy limicie pamięci).
        raise PdfExtractionFailed(
            f"parser zakończył się kodem {wynik.returncode}"
            + (f": {powod[-1]}" if powod else ""))
    return wynik.stdout.decode("utf-8", "replace")


def _ustaw_limit_pamieci(mb: int) -> None:
    """Twardy sufit na przestrzeń adresową. Po jego przekroczeniu alokacja
    kończy się `MemoryError` w tym procesie - zamiast ubicia całego kontenera
    przez OOM-killer. `resource` istnieje tylko na systemach POSIX; na Windows
    (środowisko deweloperskie) zostaje sam limit czasu po stronie rodzica."""
    try:
        import resource
    except ImportError:
        return
    limit = mb * 1024 * 1024
    miekki, twardy = resource.getrlimit(resource.RLIMIT_AS)
    if twardy != resource.RLIM_INFINITY:
        limit = min(limit, twardy)
    resource.setrlimit(resource.RLIMIT_AS, (limit, twardy))


def _main() -> int:
    _ustaw_limit_pamieci(MEM_LIMIT_MB)
    data = sys.stdin.buffer.read()
    if not data:
        print("pusty PDF na wejściu", file=sys.stderr)
        return 2

    from .pdf_tables import pdf_to_text_with_tables

    try:
        tekst = pdf_to_text_with_tables(data)
    except Exception as exc:
        # Geometria zawiodła (nietypowo zbudowany plik albo sufit pamięci) -
        # lepiej oddać sam tekst liniowy niż akt bez treści.
        print(f"wykrywanie tabel nie powiodło się ({type(exc).__name__}: {exc}), "
              f"tekst liniowy", file=sys.stderr)
        try:
            tekst = pdf_to_text_linear(data)
        except Exception as exc2:
            print(f"tekst liniowy też nie powiódł się ({type(exc2).__name__}: {exc2})",
                  file=sys.stderr)
            return 3

    sys.stdout.buffer.write(tekst.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
