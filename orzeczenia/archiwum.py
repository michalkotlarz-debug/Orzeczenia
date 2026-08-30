"""Pełne pobranie archiwum orzeczeń sądów powszechnych na lokalny dysk (Faza 3).

Inne od obserwatora (`obserwator.py`): nie interesuje nas tylko to, co nowe,
tylko CAŁY dostępny zbiór z `ncourt-api` - dziś rzędu 465 tys. pozycji. Wynik
to jeden plik JSON na orzeczenie w `out_dir`, z pełną treścią (`MsSource.document`,
te same pola co API `/orzeczenie/{źródło}/{id}`).

Zapisujemy TYLKO na dysk, mijając SQLite/Postgres `Store` - to celowe: `Store`
istnieje po to, żeby serwis odpowiadał na "co nowego", a nie żeby trzymać kopię
całego archiwum (patrz README, sekcja "Uwagi prawne" - my niczego nie
archiwizujemy w publicznym serwisie).

Bezpiecznie przerywać (Ctrl+C) i wznawiać: już zapisany plik jest pomijany,
zapis jest atomowy (`*.json.tmp` + `replace`), więc przerwanie w trakcie
zapisu jednego dokumentu nie zostawia połówkowego pliku, który udawałby,
że jest gotowy.

Skala: przy `http.delay_seconds` (domyślnie 1,2 s + jitter) i DWÓCH zapytaniach
na dokument (details + content) do orzeczenia.ms.gov.pl, pełny przebieg to
rzędu 1-2 tygodni ciągłego działania. To nie błąd konfiguracji - portal karze
szybsze odpytywanie CAPTCHĄ (patrz `http.py` / `BLOCK_MARKERS`). Stąd `--limit`
w CLI, żeby dało się to robić w kontrolowanych porcjach zamiast jednego przebiegu.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .http import RateLimited, SourceUnavailable
from .sources.ms_ncourt_api import NcourtApiSource
from .sources.registry import Registry

log = logging.getLogger("orzecznik.archiwum")

# Wcześniej niż powstał portal - z takim 'od' ncourt-api oddaje CAŁY zbiór.
EARLIEST_DATE = "2000-01-01"

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_filename(doc_id: str) -> str:
    """doc_id z ncourt-api to już bezpieczna nazwa pliku (litery/cyfry/_/-),
    ale nie ufamy temu w 100% - cokolwiek nietypowego zamieniamy na '_'."""
    return _UNSAFE.sub("_", doc_id) + ".json"


@dataclass
class ArchiveResult:
    total_in_source: int = 0
    already_had: int = 0
    downloaded: int = 0
    failed: int = 0
    interrupted: bool = False
    status: str = "ok"          # ok | przerwano | blad
    detail: str = ""


def run_archive(cfg: Config, out_dir: Path, limit: int | None = None,
                registry: Registry | None = None) -> ArchiveResult:
    """Jeden przebieg: dociągnij listę identyfikatorów, pobierz i zapisz na dysk
    te, których jeszcze nie ma. `limit` ogranicza liczbę NOWO pobranych w tym
    przebiegu (lista i sprawdzenie 'co już mamy' i tak obejmuje cały zbiór)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    own_registry = registry is None
    registry = registry or Registry(cfg)
    result = ArchiveResult()
    started = time.monotonic()
    try:
        try:
            ids = NcourtApiSource(registry.http).list_new_ids(EARLIEST_DATE, max_ids=None)
        except (RateLimited, SourceUnavailable) as exc:
            result.status = "blad"
            result.detail = f"nie udało się pobrać listy identyfikatorów: {exc}"
            log.error(result.detail)
            return result

        result.total_in_source = len(ids)
        todo = [i for i in ids if not (out_dir / _safe_filename(i)).exists()]
        result.already_had = result.total_in_source - len(todo)
        if limit is not None:
            todo = todo[:limit]

        log.info("archiwum ms: %s w źródle, %s już na dysku, %s do pobrania w tym przebiegu",
                 result.total_in_source, result.already_had, len(todo))

        for n, doc_id in enumerate(todo, start=1):
            try:
                doc = registry.document("ms", doc_id)
            except (RateLimited, SourceUnavailable) as exc:
                result.failed += 1
                log.warning("pominięto %s: %s", doc_id, exc)
                continue
            except Exception:
                result.failed += 1
                log.exception("pominięto %s (nieoczekiwany błąd)", doc_id)
                continue

            path = out_dir / _safe_filename(doc_id)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
            result.downloaded += 1

            if n % 50 == 0 or n == len(todo):
                elapsed = time.monotonic() - started
                rate = n / elapsed if elapsed else 0
                remaining_h = ((len(todo) - n) / rate / 3600) if rate else 0.0
                log.info("postęp: %s/%s pobranych w tym przebiegu (%s błędów) - "
                         "pozostało ok. %.1f godz. przy obecnym tempie",
                         n, len(todo), result.failed, remaining_h)

        return result
    except KeyboardInterrupt:
        result.interrupted = True
        result.status = "przerwano"
        log.warning("przerwano - już zapisane pliki zostają na dysku, kolejne "
                    "uruchomienie wznowi od tego miejsca (pomija to, co już jest)")
        return result
    finally:
        if own_registry:
            registry.close()
