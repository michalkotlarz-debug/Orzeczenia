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


if __name__ == "__main__":
    app()
