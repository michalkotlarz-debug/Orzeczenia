"""Punkt wejścia dla Vercela.

Vercel szuka zmiennej `app` w pliku app.py / index.py / main.py w katalogu
głównym repozytorium. Cała aplikacja mieszka w pakiecie `orzeczenia`,
więc tutaj tylko ją importujemy — dzięki temu ten sam kod działa lokalnie
(`python -m orzeczenia.cli serve`) i na Vercelu bez żadnych rozgałęzień.
"""
from orzeczenia.web.app import app  # noqa: F401
