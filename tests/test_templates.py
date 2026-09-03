"""Renderuje wszystkie strony na danych z fixture'ów i zapisuje podgląd HTML.
Wykrywa błędy w szablonach bez uruchamiania serwera (FastAPI nie jest potrzebne).
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jinja2 import Environment, FileSystemLoader, StrictUndefined      # noqa: E402

from orzeczenia.config import CacheConfig, Config, SourceConfig        # noqa: E402
from orzeczenia.format import date_pl, plural_pl                       # noqa: E402
from orzeczenia.http import SourceUnavailable                          # noqa: E402
from orzeczenia.sources.base import Query                              # noqa: E402
from orzeczenia.sources.kio_uzp import KioSource                       # noqa: E402
from orzeczenia.sources.ms_gov import MsSource                         # noqa: E402
from orzeczenia.sources.registry import Registry                       # noqa: E402

FX = Path(__file__).parent / "fixtures"
OUT = Path(__file__).parent / "preview"
OUT.mkdir(exist_ok=True)
fx = lambda n: (FX / n).read_text(encoding="utf-8")                    # noqa: E731

WEB = Path(__file__).resolve().parents[1] / "orzeczenia" / "web"
CSS = (WEB / "static" / "style.css").read_text()
JS = (WEB / "static" / "app.js").read_text()

LABELS = {"ms": "Sądy powszechne", "nsa": "Sądy administracyjne", "kio": "KIO"}
env = Environment(loader=FileSystemLoader(str(WEB / "templates")),
                  undefined=StrictUndefined, autoescape=True)
env.globals.update(
    site_name="Orzecznik", source_label=LABELS,
    date_field_labels={"judgment": "data orzeczenia", "publication": "data publikacji"},
    sort_labels={"relevance": "trafność", "date_desc": "data orzeczenia ↓",
                 "date_asc": "data orzeczenia ↑", "pub_desc": "data publikacji ↓"},
    plural_pl=plural_pl)
env.filters["datepl"] = date_pl


def _qs(p, **o):
    m = {k: v for k, v in p.items() if v not in (None, "")}
    m.update({k: v for k, v in o.items() if v not in (None, "")})
    return urlencode(m)


env.filters["urlencode_page"] = lambda p, page: _qs(p, page=page)
env.filters["urlencode_extra"] = lambda p, k, v: _qs(p, **{k: v, "page": 1})


class R:
    class url:
        query = "q=wadium"
    query_params = {"q": "wadium"}


class FakeHttp:
    cache_cfg = CacheConfig()

    def __init__(self, fail=None):
        self.fail = fail or set()

    def close(self):
        pass

    def get(self, url, *, ttl=None):
        for t in self.fail:
            if t in url:
                raise SourceUnavailable("serwis nie odpowiedział (test)")
        if "/search/advanced/" in url:
            return fx("ms_results.html")
        if "/details/$N/" in url:
            return fx("ms_details_wyrok.html")
        if "/content/$N/" in url:
            return fx("ms_content_wyrok.html")
        if "/Home/Search" in url:
            return fx("kio_results.html")
        if "/Home/Details/" in url:
            return fx("kio_details.html")
        if "/Home/ContentHtml/" in url:
            return fx("kio_content.html")
        raise AssertionError(url)


def make_registry(fail=None):
    # Serwisy podmieniamy na atrapy, więc CBOSA (które nie ma tu fixture'a
    # listy) wyłączamy, żeby test nie próbował wychodzić do sieci.
    cfg = Config(ms=SourceConfig(label=LABELS["ms"], base_url="https://orzeczenia.ms.gov.pl"),
                 kio=SourceConfig(label=LABELS["kio"], base_url="https://orzeczenia.uzp.gov.pl"),
                 nsa=SourceConfig(enabled=False))
    reg = Registry(cfg)
    reg.http.close()
    reg.http = FakeHttp(fail)
    reg.sources["ms"] = MsSource(cfg.ms, reg.http)
    reg.sources["kio"] = KioSource(cfg.kio, reg.http)
    return reg


def render(tpl, name, **ctx):
    html = env.get_template(tpl).render(request=R(), **ctx)
    html = (html.replace('<link rel="stylesheet" href="/static/style.css">',
                         f"<style>{CSS}</style>")
                .replace('<script src="/static/app.js" defer></script>', f"<script>{JS}</script>"))
    (OUT / name).write_text(html, encoding="utf-8")
    print(f"  OK   {tpl} -> tests/preview/{name} ({len(html)} znaków)")
    return html


failures: list[str] = []
print("== renderowanie stron ==")
try:
    reg = make_registry()
    q = Query(phrase="wadium")

    render("home.html", "1-strona-glowna.html", latest_rows=reg.search(Query(sort="pub_desc")).hits,
          date_field="publication")

    res = reg.search(q, page=1)
    h = render("results.html", "2-wyniki.html", q="wadium", res=res, query=q, page=1,
               params={"q": "wadium"}, source="")
    assert "Wszystkie" in h and "Sądy powszechne" in h, "brak zakładek źródeł"
    assert "/orzeczenie/ms/" in h, "brak linku do pełnej treści"
    assert "Powodowie" in h, "opis ze źródła nie trafił na kartę"

    doc = reg.document("kio", "35751")
    h = render("document.html", "3-orzeczenie-kio.html", d=doc, q="")
    assert "Sentencja" in h and "Uzasadnienie" in h, "brak sentencji lub uzasadnienia"
    assert "oddala odwołanie" in h, "treść sentencji nie trafiła na stronę"
    assert "Zamawiający prowadzi" in h, "treść uzasadnienia nie trafiła na stronę"
    assert "17 lipca 2026" in h, "data nie po polsku"
    assert h.index("przewodniczący") < h.index("protokolant"), "zła kolejność składu"
    assert "/pobierz.txt" in h, "brak odnośnika do pobrania"

    doc2 = reg.document("ms", "151010000000503_I_C_000438_2025_Uz_2026-08-05_001")
    h = render("document.html", "4-orzeczenie-sad.html", d=doc2, q="")
    assert "utrzymuje w mocy" in h, "treść orzeczenia nie trafiła na stronę"

    empty = reg.search(Query(judge="Kowalski"), page=1)
    empty.hits = []
    render("results.html", "6-brak-wynikow.html", q="", res=empty, query=Query(), page=1,
           params={}, source="")

    render("nowe.html", "7-nowe-orzeczenia.html", rows=[{
        "source": "nsa", "doc_id": "226B5A6CD0", "signature": "I SA/Łd 269/26",
        "doc_type": "wyrok", "court": "WSA w Łodzi", "division": None,
        "judgment_date": "2026-08-27", "publication_date": "2026-08-29",
        "excerpt": "w przedmiocie podatku od towarów i usług oddala skargę.",
        "first_seen_at": "2026-08-29T05:00:00+00:00", "thematic": ["Podatek od towarów i usług"],
        "panel": ["Agnieszka Gortych-Ratajczyk"],
        "url": "/orzeczenie/nsa/226B5A6CD0"}],
        source="", date_field="publication", blad="")

    render("nowe.html", "8-nowe-pusto.html", rows=[], source="", date_field="publication",
           blad="baza obserwatora niedostępna: brak DATABASE_URL")

    render("ulubione.html", "9-ulubione.html")
    render("blad.html", "10-blad.html", tytul="Nie udało się pobrać orzeczenia",
           opis="serwis chwilowo ogranicza zapytania (pokazał CAPTCHA).")
    reg.close()
except Exception as exc:
    import traceback
    traceback.print_exc()
    failures.append(str(exc))

print("\n" + "=" * 62)
if failures:
    print("NIEPOWODZENIA:", failures)
    sys.exit(1)
print("WSZYSTKIE SZABLONY RENDERUJĄ SIĘ POPRAWNIE")
