"""Formatowanie wartości do prezentacji. Bez zależności od frameworka webowego,
dzięki czemu da się to testować bez uruchamiania FastAPI."""
from __future__ import annotations

PL_MONTHS = ("stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
             "lipca", "sierpnia", "września", "października", "listopada", "grudnia")


def date_pl(value: str | None) -> str:
    """'2026-08-21' -> '21 sierpnia 2026'. Wartość nierozpoznaną zwracamy bez zmian."""
    if not value:
        return "—"
    try:
        y, m, d = str(value).split("-")
        return f"{int(d)} {PL_MONTHS[int(m) - 1]} {y}"
    except (ValueError, IndexError):
        return str(value)


def plural_pl(n: int, one: str, few: str, many: str) -> str:
    """Polska odmiana liczebnika: 1 wynik / 2-4 wyniki / 5+ wyników."""
    if n == 1:
        return one
    last, last2 = n % 10, n % 100
    if 2 <= last <= 4 and not (12 <= last2 <= 14):
        return few
    return many
