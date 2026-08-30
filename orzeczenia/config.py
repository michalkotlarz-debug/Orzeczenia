"""Konfiguracja. Aplikacja nie ma bazy danych - są tylko ustawienia sieci i cache'u."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(os.environ.get("ORZECZNIK_CONFIG", "config.yaml"))


@dataclass
class HttpConfig:
    delay_seconds: float = 1.2
    jitter_pct: float = 0.25
    timeout_seconds: float = 20.0
    max_retries: int = 2
    backoff_base_seconds: float = 3.0
    cooldown_seconds: float = 60.0
    user_agent: str = "Orzecznik/2.0"
    respect_robots: bool = True


@dataclass
class CacheConfig:
    listing_ttl_seconds: int = 600
    document_ttl_seconds: int = 21600
    max_entries: int = 500


@dataclass
class SourceConfig:
    enabled: bool = True
    label: str = ""
    base_url: str = ""


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    site_name: str = "Orzecznik"
    # Adresy, z których wolno wołać /api/* z przeglądarki (pusta lista = tylko ten sam host)
    cors_origins: list[str] = field(default_factory=list)


@dataclass
class Config:
    http: HttpConfig = field(default_factory=HttpConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    ms: SourceConfig = field(default_factory=lambda: SourceConfig(
        label="Sądy powszechne", base_url="https://orzeczenia.ms.gov.pl"))
    kio: SourceConfig = field(default_factory=lambda: SourceConfig(
        label="KIO", base_url="https://orzeczenia.uzp.gov.pl"))
    web: WebConfig = field(default_factory=WebConfig)


def _sub(cls, raw: dict[str, Any] | None, **defaults):
    data = {**defaults, **(raw or {})}
    known = set(cls.__dataclass_fields__)          # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    sources = raw.get("sources") or {}
    cfg = Config(
        http=_sub(HttpConfig, raw.get("http")),
        cache=_sub(CacheConfig, raw.get("cache")),
        ms=_sub(SourceConfig, sources.get("ms"),
                label="Sądy powszechne", base_url="https://orzeczenia.ms.gov.pl"),
        kio=_sub(SourceConfig, sources.get("kio"),
                 label="KIO", base_url="https://orzeczenia.uzp.gov.pl"),
        web=_sub(WebConfig, raw.get("web")),
    )
    if ua := os.environ.get("ORZECZNIK_USER_AGENT"):
        cfg.http.user_agent = ua
    return cfg
