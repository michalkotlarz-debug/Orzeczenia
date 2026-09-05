"""Uruchamianie serwisu."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer

from .config import load_config

app = typer.Typer(add_completion=False, help="Orzecznik - wyszukiwarka orzeczeń na żywo.")


@app.command("serve")
def serve(host: str = typer.Option(None, "--host"),
          port: int = typer.Option(None, "--port"),
          reload: bool = typer.Option(False, "--reload"),
          verbose: bool = typer.Option(False, "--verbose", "-v"),
          config: Path = typer.Option("config.yaml", "--config", "-c")):
    """Uruchom serwis (domyślnie http://127.0.0.1:8000)."""
    import os
    import uvicorn
    os.environ.setdefault("ORZECZNIK_CONFIG", str(config))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        stream=sys.stdout)
    cfg = load_config(config)
    uvicorn.run("orzeczenia.web.app:app", host=host or cfg.web.host,
                port=port or cfg.web.port, reload=reload)


@app.command("obserwuj")
def obserwuj(verbose: bool = typer.Option(False, "--verbose", "-v"),
             config: Path = typer.Option("config.yaml", "--config", "-c")):
    """Jeden przebieg obserwatora: sprawdź, czy pojawiły się nowe orzeczenia.

    Do wpisania w crona / harmonogram Railway, np. codziennie o 5:00:
        0 5 * * *  cd /app && python -m orzeczenia obserwuj
    """
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        stream=sys.stdout)
    from .obserwator import run_once
    cfg = load_config(config)
    results = run_once(cfg)
    if not results:
        typer.echo("Żaden serwis nie ma włączonego obserwatora (sources.*.poll).")
        raise typer.Exit(0)
    total = sum(r.added for r in results)
    for r in results:
        mark = "ok " if r.status == "ok" else "BŁĄD"
        typer.echo(f"[{mark}] {r.source:4s} stron: {r.pages}  obejrzano: {r.seen:4d}  "
                   f"nowych: {r.added:4d}  {r.detail}")
    typer.echo(f"Razem nowych orzeczeń: {total}")
    raise typer.Exit(1 if any(r.status != "ok" for r in results) else 0)


@app.command("importuj-ms")
def importuj_ms(
    limit: int = typer.Option(1000, "--limit", help="Ile NOWYCH orzeczeń maksymalnie pobrać"),
    since: str = typer.Option(None, "--since",
                              help="RRRR-MM-DD - od kiedy szukać w archiwum "
                                   "(domyślnie 90 dni wstecz, z --full: całe archiwum)"),
    full: bool = typer.Option(False, "--full",
                              help="Sięgnij po CAŁY dostępny zbiór (ok. 465 tys. pozycji), "
                                   "nie tylko okno 90 dni. Do wielokrotnego, cyklicznego "
                                   "wywoływania (np. z GitHub Actions co 30 min) - stan "
                                   "'co już mamy' trzyma się sam w bazie, więc kolejne "
                                   "przebiegi same wznawiają się dalej."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    config: Path = typer.Option("config.yaml", "--config", "-c"),
):
    """Jednorazowy, większy import orzeczeń sądów powszechnych przez ncourt-api -
    nie tylko to, co nowe od ostatniego przebiegu obserwatora, tylko cała paczka
    z zadanego okna czasu. Do ręcznego uruchomienia, gdy zwykły `obserwuj`
    (ograniczony do najświeższych nowości) to za mało.
    """
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        stream=sys.stdout)
    from .obserwator import import_ms_batch
    cfg = load_config(config)
    r = import_ms_batch(cfg, limit=limit, since=since, full=full)
    mark = "ok " if r.status == "ok" else "BŁĄD"
    typer.echo(f"[{mark}] obejrzano: {r.seen}  nowych: {r.added}  {r.detail}")
    raise typer.Exit(0 if r.status == "ok" else 1)


@app.command("archiwizuj-ms")
def archiwizuj_ms(
    out: Path = typer.Option(Path("dane/archiwum/ms"), "--out",
                             help="Katalog docelowy - jeden plik JSON na orzeczenie"),
    limit: int = typer.Option(None, "--limit",
                              help="Maks. liczba NOWO pobranych dokumentów w tym przebiegu "
                                   "(domyślnie bez limitu)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    config: Path = typer.Option("config.yaml", "--config", "-c"),
):
    """Pełne pobranie archiwum orzeczeń sądów powszechnych na dysk (JSON, 1 plik/orzeczenie).

    W odróżnieniu od `importuj-ms` (nowości od zadanej daty) i `obserwuj`
    (najświeższe), to jest CAŁY dostępny zbiór z ncourt-api - dziś rzędu
    465 tysięcy pozycji. Przy odstępie `http.delay_seconds` z config.yaml
    (domyślnie 1,2 s) i dwóch zapytaniach na dokument do orzeczenia.ms.gov.pl,
    pełny przebieg trwa rzędu 1-2 tygodni ciągłego działania - portal karze
    szybsze odpytywanie CAPTCHĄ.

    Bezpiecznie przerwać w dowolnym momencie (Ctrl+C) i uruchomić ponownie -
    już zapisane pliki są pomijane, więc przebieg wznawia się od miejsca
    przerwania. Użyj --limit, żeby pobierać w mniejszych, kontrolowanych
    porcjach zamiast jednego bardzo długiego przebiegu.
    """
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        stream=sys.stdout)
    from .archiwum import run_archive
    cfg = load_config(config)
    r = run_archive(cfg, out, limit=limit)
    typer.echo(f"W źródle: {r.total_in_source}  już na dysku (przed tym przebiegiem): "
               f"{r.already_had}  pobrano teraz: {r.downloaded}  błędów: {r.failed}")
    if r.status == "blad":
        typer.echo(f"BŁĄD: {r.detail}")
    if r.interrupted:
        typer.echo("Przerwano - uruchom to samo polecenie ponownie, żeby kontynuować.")
    raise typer.Exit(0 if r.status in ("ok", "przerwano") else 1)


@app.command("akty-importuj")
def akty_importuj(
    year: int = typer.Option(..., "--year", help="Rocznik do pobrania, np. 2026"),
    publisher: str = typer.Option("DU,MP", "--publisher",
                                  help="DU, MP, albo oba oddzielone przecinkiem (domyślnie oba)"),
    limit: int = typer.Option(None, "--limit",
                              help="Ile NOWYCH pozycji maksymalnie pobrać w tym przebiegu "
                                   "(domyślnie bez limitu - cały brakujący rocznik)"),
    pos: str = typer.Option(None, "--pos",
                            help="Tylko wskazane pozycje rocznika, oddzielone przecinkami "
                                 "(np. --pos 2366,2362,2359) - zamiast całego rocznika. "
                                 "Przy kilku dziennikach naraz (--publisher DU,MP) te same "
                                 "pozycje są stosowane do każdego z nich."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    config: Path = typer.Option("config.yaml", "--config", "-c"),
):
    """Import aktów prawnych (Dziennik Ustaw / Monitor Polski) z ELI API Sejmu
    dla jednego rocznika. Bezpiecznie przerwać (Ctrl+C) i uruchomić ponownie -
    już zaimportowane pozycje są pomijane. Żeby zejść w głąb archiwum, wywołuj
    to samo polecenie z kolejnymi, coraz starszymi rocznikami:

        python -m orzeczenia akty-importuj --year 2026
        python -m orzeczenia akty-importuj --year 2025
        ...

    Albo dociągnij tylko wybrane pozycje (np. do ręcznego sprawdzenia czegoś
    konkretnego) przez --pos, bez importowania całego rocznika.
    """
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        stream=sys.stdout)
    from .akty_import import import_positions, import_year
    from .http import PoliteClient
    from .store import Store
    cfg = load_config(config)
    store = Store(cfg.store.url, cfg.store.keep_days)
    http = PoliteClient(cfg.http, cfg.cache)
    publishers = [p.strip().upper() for p in publisher.split(",") if p.strip()]
    positions = [int(p.strip()) for p in pos.split(",") if p.strip()] if pos else None
    ok = True
    try:
        for pub in publishers:
            if positions:
                r = import_positions(cfg, store, pub, year, positions, http=http)
            else:
                r = import_year(cfg, store, pub, year, http=http, limit=limit)
            mark = "ok " if r.status == "ok" else ("BŁĄD" if r.status == "blad" else "STOP")
            typer.echo(f"[{mark}] {pub} {year}: w źródle {r.total_in_source}  "
                      f"już w bazie {r.already_had}  pobrano {r.downloaded}  "
                      f"błędów {r.failed}  bez tekstu {r.without_text}  {r.detail}")
            ok = ok and r.status != "blad"
    finally:
        http.close()
        store.close()
    raise typer.Exit(0 if ok else 1)


@app.command("akty-obserwuj")
def akty_obserwuj(
    since: str = typer.Option(None, "--since",
                              help="ISO 8601, np. 2026-09-01T00:00:00 - domyślnie od "
                                   "najświeższego 'changeDate' już w bazie. Przy PUSTEJ "
                                   "bazie aktów podaj to jawnie (pierwsze uruchomienie)."),
    limit: int = typer.Option(500, "--limit", help="Bezpiecznik - maks. pozycji w tym przebiegu"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    config: Path = typer.Option("config.yaml", "--config", "-c"),
):
    """Przyrostowy import aktów prawnych - tylko to, co nowe/zmienione od
    ostatniego przebiegu (jedno zapytanie o listę zmian zamiast całych
    roczników). Do wpisania w crona / Harmonogram zadań, np. raz dziennie:

        0 6 * * *  cd /app && python -m orzeczenia akty-obserwuj
    """
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        stream=sys.stdout)
    from .akty_import import import_changes
    from .store import Store
    cfg = load_config(config)
    store = Store(cfg.store.url, cfg.store.keep_days)
    try:
        try:
            r = import_changes(cfg, store, since=since, max_items=limit)
        except ValueError as exc:
            typer.echo(f"BŁĄD: {exc}")
            raise typer.Exit(1)
        mark = "ok " if r.status == "ok" else "STOP"
        typer.echo(f"[{mark}] zmian w źródle: {r.total_in_source}  przetworzono: "
                  f"{r.downloaded}  bez tekstu: {r.without_text}")
    finally:
        store.close()


@app.command("akty-wstecz")
def akty_wstecz(
    batch: int = typer.Option(300, "--batch",
                              help="Ile NOWYCH pozycji na dziennik pobrać w tym wywołaniu"),
    start_year: int = typer.Option(2025, "--start-year",
                                   help="Od którego rocznika zacząć, jeśli nie ma jeszcze "
                                        "zapisanego postępu (plik stanu)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    config: Path = typer.Option("config.yaml", "--config", "-c"),
):
    """Jedna PACZKA cofania się w głąb archiwum aktów prawnych - pamięta sama,
    na którym roczniku stanęła (plik `dane/akty_wstecz_state.json`), i gdy
    rocznik jest już kompletny w obu dziennikach, następnym razem schodzi rok
    niżej. Do wpisania w crona / Harmonogram zadań co np. 30 minut:

        */30 * * * *  cd /app && python -m orzeczenia akty-wstecz
    """
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        stream=sys.stdout)
    from .akty_import import import_backfill_batch
    from .store import Store
    cfg = load_config(config)
    store = Store(cfg.store.url, cfg.store.keep_days)
    try:
        info = import_backfill_batch(cfg, store, batch_per_publisher=batch,
                                     start_year=start_year)
        if info.get("done"):
            typer.echo(f"Gotowe - rocznik {info['year']} jest poniżej najstarszego "
                      f"dostępnego w API, cofanie się zakończone.")
            return
        for r in info["results"]:
            mark = "ok " if r["status"] == "ok" else "BŁĄD"
            typer.echo(f"[{mark}] {r['publisher']} {r['year']}: w źródle {r['total_in_source']}  "
                      f"już w bazie {r['already_had']}  pobrano {r['downloaded']}  "
                      f"błędów {r['failed']}")
        if info["complete_this_year"]:
            typer.echo(f"Rocznik {info['year']} kompletny - następnym razem: {info['next_year']}.")
        else:
            typer.echo(f"Rocznik {info['year']} jeszcze niekompletny - kolejne wywołanie "
                      f"kontynuuje ten sam rok.")
    finally:
        store.close()


@app.command("scal-duplikaty")
def scal_duplikaty(
    source: str = typer.Option("ms", "--source", help="Które źródło porządkować"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    config: Path = typer.Option("config.yaml", "--config", "-c"),
):
    """Jednorazowe wsteczne porządkowanie: scala pary wyrok+uzasadnienie, które
    trafiły do bazy jako dwa osobne rekordy PRZED wprowadzeniem scalania na
    bieżąco w obserwatorze. Bezpieczne uruchomić wielokrotnie - już scalone
    grupy nie pojawią się drugi raz.
    """
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        stream=sys.stdout)
    from .obserwator import merge_existing_duplicates
    cfg = load_config(config)
    stats = merge_existing_duplicates(cfg, source=source)
    typer.echo(f"grup zdublowanych: {stats['grup']}  scalonych: {stats['scalonych']}  "
               f"zastąpionych okrojoną wersją: {stats['zastapionych']}  "
               f"pominiętych (niejednoznacznych): {stats['pominietych_niejednoznacznych']}")


@app.command("pokaz")
def pokaz(doc_id: str, source: str = typer.Option("ms", "--source"),
          config: Path = typer.Option("config.yaml", "--config", "-c")):
    """Debug: pokaż kluczowe pola jednego zaimportowanego dokumentu - do
    ręcznego sprawdzenia przed użyciem `scal-recznie`."""
    from .store import Store
    cfg = load_config(config)
    store = Store(cfg.store.url, cfg.store.keep_days)
    try:
        d = store.get_document(source, doc_id)
        if not d:
            typer.echo("nie znaleziono (albo dokument bez treści)")
            raise typer.Exit(1)
        typer.echo(f"doc_id={d['doc_id']}  typ={d.get('doc_type')}  typ_z_tytulu={d.get('doc_type_raw')}")
        typer.echo(f"sygnatura={d.get('signature')}  sad={d.get('court')}")
        typer.echo(f"data_orzeczenia={d.get('judgment_date')}  data_publikacji={d.get('publication_date')}")
        typer.echo(f"sentencja (poczatek): {(d.get('sentencja') or '(brak)')[:250]}")
        typer.echo(f"uzasadnienie (poczatek): {(d.get('uzasadnienie') or '(brak)')[:250]}")
        typer.echo(f"full_text (poczatek): {(d.get('full_text') or '(brak)')[:300]}")
    finally:
        store.close()


@app.command("scal-recznie")
def scal_recznie(
    keep: str = typer.Option(..., "--keep", help="doc_id, który zostaje jako kanoniczny link"),
    absorb: str = typer.Option(..., "--absorb", help="doc_id, którego treść dołączamy i który znika"),
    source: str = typer.Option("ms", "--source"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    config: Path = typer.Option("config.yaml", "--config", "-c"),
):
    """Ręcznie wskazane scalenie dwóch już zaimportowanych dokumentów - dla
    przypadków, gdy automatyczne rozpoznawanie typu z tytułu nadało jednemu z
    nich mylącą etykietę, więc `scal-duplikaty` uznał parę za niejednoznaczną i
    ją pominął. Sprawdź najpierw oba dokumenty przez `pokaz`.
    """
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        stream=sys.stdout)
    from .obserwator import merge_specific_pair
    from .store import Store
    cfg = load_config(config)
    store = Store(cfg.store.url, cfg.store.keep_days)
    try:
        ok = merge_specific_pair(store, source, keep, absorb)
        if ok:
            typer.echo(f"scalono: {absorb} -> {keep}")
        else:
            typer.echo("nie znaleziono jednego z dokumentów (albo bez treści) - nic nie scalono")
        raise typer.Exit(0 if ok else 1)
    finally:
        store.close()


@app.command("liczba")
def liczba(
    since: str = typer.Option(..., "--since", help="RRRR-MM-DD"),
    date_field: str = typer.Option("publication", "--date-field",
                                   help="'publication' (data publikacji) albo 'judgment' (data orzeczenia)"),
    source: str = typer.Option("", "--source"),
    config: Path = typer.Option("config.yaml", "--config", "-c"),
):
    """Debug: ile zaimportowanych dokumentów ma datę orzeczenia/publikacji >= since."""
    from .store import Store
    cfg = load_config(config)
    store = Store(cfg.store.url, cfg.store.keep_days)
    try:
        _, total = store.search_advanced(source=source, date_field=date_field,
                                         date_from=since, limit=1, offset=0)
        typer.echo(f"{total}")
    finally:
        store.close()


def _redact_url(url: str) -> str:
    """Adres bazy bez hasła - to trafia na ekran/w logi, hasło nie powinno."""
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url)
    if not parts.password:
        return url
    netloc = parts.hostname or ""
    if parts.username:
        netloc = f"{parts.username}:***@{netloc}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@app.command("baza")
def baza(config: Path = typer.Option("config.yaml", "--config", "-c")):
    """Pokaż, co obserwator ma już zebrane."""
    from .store import Store
    cfg = load_config(config)
    store = Store(cfg.store.url, cfg.store.keep_days)
    try:
        counts = store.count()
        typer.echo(f"Baza: {_redact_url(cfg.store.url)}")
        if not counts:
            typer.echo("Pusto - obserwator jeszcze nie zbierał.")
            return
        for src, n in sorted(counts.items()):
            typer.echo(f"  {src:4s} {n:6d}")
        typer.echo(f"  {'razem':4s} {sum(counts.values()):6d}")
        for row in store.runs(5):
            typer.echo(f"  przebieg {row['started_at']} {row['source']} "
                       f"+{row['added']} ({row['status']})")
    finally:
        store.close()


if __name__ == "__main__":
    app()
