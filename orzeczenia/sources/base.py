"""Wspólny model danych dla wszystkich serwisów źródłowych."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Query:
    """Kryteria wyszukiwania podane przez użytkownika."""
    phrase: str = ""
    signature: str = ""
    judge: str = ""
    thematic: str = ""
    legal_basis: str = ""
    date_from: str = ""
    date_to: str = ""
    date_field: str = "judgment"          # judgment | publication
    sort: str = "relevance"               # relevance | date_desc | date_asc | pub_desc

    def is_empty(self) -> bool:
        return not any((self.phrase, self.signature, self.judge, self.thematic,
                        self.legal_basis, self.date_from, self.date_to))


@dataclass
class Hit:
    """Jedna pozycja listy wyników - dokładnie to, co pokazuje serwis źródłowy."""
    source: str
    doc_id: str
    signature: str | None = None
    doc_type: str | None = None
    court: str | None = None
    division: str | None = None
    judgment_date: str | None = None
    publication_date: str | None = None
    panel: list[str] = field(default_factory=list)
    thematic: list[str] = field(default_factory=list)
    excerpt: str | None = None
    outcome: str | None = None
    source_url: str = ""

    @property
    def url(self) -> str:
        return f"/orzeczenie/{self.source}/{self.doc_id}"


@dataclass
class SearchPage:
    hits: list[Hit] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)     # ile znalazł każdy serwis
    errors: dict[str, str] = field(default_factory=dict)     # serwisy, które zawiodły
    page: int = 1
    per_page: int = 10

    @property
    def total(self) -> int:
        return sum(self.totals.values())


class Source(Protocol):
    key: str
    label: str

    def search(self, q: Query, page: int) -> tuple[list[Hit], int]:
        """Zwraca (pozycje ze strony `page`, łączną liczbę trafień w serwisie)."""

    def document(self, doc_id: str) -> dict[str, Any]:
        """Pobiera i parsuje pełną treść jednego orzeczenia."""
