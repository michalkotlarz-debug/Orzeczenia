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
                                   "(domyślnie 90 dni wstecz)"),
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
    r = import_ms_batch(cfg, limit=limit, since=since)
    mark = "ok " if r.status == "ok" else "BŁĄD"
    typer.echo(f"[{mark}] obejrzano: {r.seen}  nowych: {r.added}  {r.detail}")
    raise typer.Exit(0 if r.status == "ok" else 1)


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
