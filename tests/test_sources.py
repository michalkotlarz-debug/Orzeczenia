"""Testy warstwy źródeł: budowa adresów wyszukiwania, parsowanie list i dokumentów,
scalanie wyników z dwóch serwisów. Bez ani jednego zapytania sieciowego —
wszystko na zapisanych fragmentach HTML z tests/fixtures.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orzeczenia.config import CacheConfig, Config, SourceConfig      # noqa: E402
from orzeczenia.format import date_pl, plural_pl                     # noqa: E402
from orzeczenia.http import SourceUnavailable, TTLCache, looks_blocked  # noqa: E402
from orzeczenia.parse.common import (detect_doc_type, detect_doc_types,  # noqa: E402
                                     extract_panel, normalize_signature, parse_date,
                                     split_sentencja_uzasadnienie)
from orzeczenia.sources.base import Query                            # noqa: E402
from orzeczenia.sources.kio_uzp import KioSource                     # noqa: E402
from orzeczenia.sources.ms_gov import MsSource                       # noqa: E402
from orzeczenia.sources.registry import Registry                     # noqa: E402

FX = Path(__file__).parent / "fixtures"
fx = lambda n: (FX / n).read_text(encoding="utf-8")                  # noqa: E731

failures: list[str] = []


def check(label, got, expected=None, predicate=None):
    ok = predicate(got) if predicate else (got == expected)
    print(f"{'  OK  ' if ok else ' FAIL '} {label}: {got!r}" +
          ("" if ok or predicate else f"  (oczekiwano {expected!r})"))
    if not ok:
        failures.append(label)


class FakeHttp:
    """Atrapa klienta: oddaje fixture'y i zapamiętuje, o co ją pytano."""

    def __init__(self, fail: set[str] | None = None):
        self.cache_cfg = CacheConfig()
        self.calls: list[str] = []
        self.fail = fail or set()

    def close(self) -> None:
        pass

    def get(self, url: str, *, ttl: int | None = None) -> str:
        self.calls.append(url)
        for token in self.fail:
            if token in url:
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
        raise AssertionError("nieoczekiwany adres: " + url)


ms_cfg = SourceConfig(label="Sądy powszechne", base_url="https://orzeczenia.ms.gov.pl")
kio_cfg = SourceConfig(label="KIO", base_url="https://orzeczenia.uzp.gov.pl")
ms = MsSource(ms_cfg, FakeHttp())
kio = KioSource(kio_cfg, FakeHttp())


def tapestry_unescape(seg: str) -> str:
    """Odwrotność kodowania Tapestry: '$0020' -> ' ', '$002f' -> '/'."""
    return re.sub(r"\$([0-9a-f]{4})", lambda m: chr(int(m.group(1), 16)), seg)


def ms_segments(query, page=1):
    """Segmenty ścieżki. '/' w sygnaturze jest zakodowany jako $002f,
    więc podział po '/' jest bezpieczny."""
    raw = ms.search_url(query, page).split("/search/advanced/")[1]
    return [tapestry_unescape(x) for x in raw.split("/")]


# ----------------------------------------------------------------------
print("== budowa adresu wyszukiwania: sądy powszechne ==")
segs = ms_segments(Query(phrase="zasiedzenie"))
check("18 segmentów ścieżki", len(segs), 18)
check("fraza na 1. pozycji", segs[0], "zasiedzenie")
check("sygnatura pominięta", segs[1], "$N")
check("sortowanie i strona na końcu", segs[15:], ["score", "descending", "1"])

segs = ms_segments(Query(signature="II C 123/20", judge="Kowalski",
                         thematic="Zasiedzenie", legal_basis="art. 172 k.c.",
                         date_from="2024-01-01", date_to="2024-12-31",
                         sort="date_desc"), 3)
check("sygnatura", segs[1], "II C 123/20")
check("data od", segs[7], "2024-01-01")
check("data do", segs[8], "2024-12-31")
check("sędzia", segs[9], "Kowalski")
check("hasło tematyczne", segs[11], "Zasiedzenie")
check("podstawa prawna", segs[12], "art. 172 k.c.")
check("sortowanie po dacie", segs[15:17], ["data", "descending"])
check("numer strony", segs[17], "3")

# Portal używa własnego kodowania Tapestry ('$' + 4 cyfry hex). Kodowanie
# procentowe w ścieżce kończy się odpowiedzią HTTP 400 - sprawdzone na żywo.
u_sig = ms.search_url(Query(signature="II C 438/25"), 1)
check("ukośnik jako $002f, nigdy %2F", u_sig,
      predicate=lambda x: "$002f" in x and "%2F" not in x and "%2f" not in x)
check("spacja jako $0020", u_sig, predicate=lambda x: "$0020" in x and " " not in x)
check("polskie znaki zakodowane po tapestry'owemu",
      ms.search_url(Query(phrase="zadośćuczynienie"), 1),
      predicate=lambda x: "$015b$0107" in x and "%" not in x)
check("ukośnik nie rozbija ścieżki",
      len(u_sig.split("/search/advanced/")[1].split("/")), 18)
check("puste zapytanie dostaje zakres dat", ms_segments(Query())[7],
      predicate=lambda v: v != "$N")

print("\n== budowa adresu wyszukiwania: KIO ==")
u = kio.search_url(Query(phrase="wadium", date_from="2026-07-01", date_to="2026-07-05"), 2)
check("fraza", u, predicate=lambda x: "Phrase=wadium" in x)
check("zakres dat w formacie DD-MM-RRRR", u,
      predicate=lambda x: "Dt=01-07-2026%20-%2005-07-2026" in x)
check("numer strony", u, predicate=lambda x: "Pg=2" in x)
check("szukanie w treści włączone", u, predicate=lambda x: "SCnt=1" in x)
check("filtr po dacie publikacji nie trafia do KIO",
      kio.search_url(Query(date_field="publication", date_from="2026-01-01"), 1),
      predicate=lambda x: "Dt=" not in x)

# ----------------------------------------------------------------------
print("\n== parsowanie listy wyników ==")
hits = ms.parse_results(fx("ms_results.html"))
check("liczba pozycji", len(hits), 3)
check("sygnatura", hits[0].signature, "I C 438/25")
check("data orzeczenia", hits[0].judgment_date, "2026-08-05")
check("data publikacji", hits[0].publication_date, "2026-08-07")
check("sąd", hits[0].court, "Sąd Okręgowy w Elblągu")
check("opis (urywek) przeniesiony", hits[0].excerpt,
      predicate=lambda v: v and v.startswith("Sygn. akt I C 438/25"))
check("adres wewnętrzny", hits[0].url,
      "/orzeczenie/ms/151010000000503_I_C_000438_2025_Uz_2026-08-05_001")
check("licznik trafień", MsSource.parse_count(fx("ms_results.html")), 13)

khits = kio.parse_results(fx("kio_results.html"))
check("KIO: liczba pozycji", len(khits), 2)
check("KIO: sygnatura", khits[0].signature, "KIO 3312/26")
check("KIO: typ", khits[1].doc_type, "wyrok")
check("KIO: licznik", KioSource.parse_count(fx("kio_results.html")), 34065)

# ----------------------------------------------------------------------
print("\n== pełna treść orzeczenia ==")
d = ms.parse_document("abc", fx("ms_details_wyrok.html"), fx("ms_content_wyrok.html"))
check("sygnatura", d["signature"], "IV Ka 352/26")
check("typ", d["doc_type"], "wyrok")
check("data orzeczenia", d["judgment_date"], "2026-06-02")
check("data publikacji", d["publication_date"], "2026-08-27")
check("sąd", d["court"], "Sąd Okręgowy w Piotrkowie Trybunalskim")
check("wydział", d["division"], "IV Wydział Karny Odwoławczy")
check("hasła", d["thematic"], ["Swobodna ocena dowodów", "Kara"])
check("przewodniczący", d["chairman"], "Agnieszka Szulc-Wroniszewska")
check("sentencja niepusta", d["sentencja"], predicate=lambda v: v and "utrzymuje w mocy" in v)
check("link do oryginału", d["source_url"], predicate=lambda v: v.endswith("/content/$N/abc"))

k = kio.parse_document("35751", fx("kio_details.html"), fx("kio_content.html"))
check("KIO: sygnatura", k["signature"], "KIO 3192/26")
check("KIO: zamawiający", k["purchaser"], predicate=lambda v: v and "GAZ-SYSTEM" in v)
check("KIO: rozstrzygnięcie", k["outcome"], "oddalone")
check("KIO: pełny skład, protokolant na końcu",
      [(j["name"], j["role"]) for j in k["judges"]],
      [("Mateusz Paczkowski", "przewodniczący"), ("Aleksandra Patyk", "członek"),
       ("Anna Chudzik", "członek"), ("Mikołaj Kraska", "protokolant")])
check("KIO: uzasadnienie oddzielone", k["uzasadnienie"],
      predicate=lambda v: v and v.startswith("Zamawiający prowadzi"))
check("KIO: link do PDF ze źródła", k["pdf_url"], predicate=lambda v: "PdfContent" in v)

print("\n== dokument złożony i strona blokady ==")
d2 = ms.parse_document("x", fx("ms_details_dwa_typy.html"), "<html><body></body></html>")
check("typ wiodący z dwóch", d2["doc_type"], "zarządzenie")
check("oba typy zachowane", d2["doc_types"], ["zarządzenie", "uzasadnienie"])
check("CAPTCHA rozpoznana", looks_blocked(fx("ms_captcha.html")), True)
check("zwykła metryka nie jest blokadą", looks_blocked(fx("ms_details_wyrok.html")), False)
try:
    ms.parse_document("z", fx("ms_captcha.html"), fx("ms_captcha.html"))
    check("parser odrzuca stronę blokady", "nie zgłosił błędu", predicate=lambda v: False)
except ValueError:
    check("parser odrzuca stronę blokady", True, True)

# ----------------------------------------------------------------------
print("\n== scalanie wyników z dwóch serwisów ==")
# NSA wyłączone celowo: ten plik sprawdza scalanie MS + KIO,
# CBOSA ma własny zestaw testów (tests/test_nsa.py).
cfg = Config(ms=ms_cfg, kio=kio_cfg, nsa=SourceConfig(enabled=False))
reg = Registry(cfg)
reg.http.close()
reg.http = FakeHttp()
reg.sources["ms"] = MsSource(ms_cfg, reg.http)
reg.sources["kio"] = KioSource(kio_cfg, reg.http)

page = reg.search(Query(phrase="wadium"), page=1)
check("pozycje z obu źródeł", sorted({h.source for h in page.hits}), ["kio", "ms"])
check("łączna liczba trafień", page.total, 13 + 34065)
check("liczniki per źródło", page.totals, {"ms": 13, "kio": 34065})
check("brak błędów", page.errors, {})
check("wyniki przeplatane", [h.source for h in page.hits][:4], ["ms", "kio", "ms", "kio"])

reg.http.calls.clear()
page = reg.search(Query(phrase="wadium"), page=1, only="ms")
check("zawężenie do jednego źródła", {h.source for h in page.hits}, {"ms"})
check("pytamy tylko wybrany serwis", [c for c in reg.http.calls if "uzp.gov.pl" in c], [])

page = reg.search(Query(phrase="x", sort="date_asc"), page=1)
dates = [h.judgment_date for h in page.hits if h.judgment_date]
check("sortowanie rosnąco po dacie", dates, sorted(dates))

print("\n== filtry, których KIO nie obsługuje ==")
page = reg.search(Query(judge="Kowalski"), page=1)
check("KIO pominięte przy filtrze po sędzim", page.totals.get("kio"), 0)
check("sądy powszechne nadal odpytane", page.totals.get("ms"), 13)

print("\n== awaria jednego serwisu nie psuje strony ==")
reg.http = FakeHttp(fail={"uzp.gov.pl"})
reg.sources["ms"] = MsSource(ms_cfg, reg.http)
reg.sources["kio"] = KioSource(kio_cfg, reg.http)
page = reg.search(Query(phrase="wadium"), page=1)
check("wyniki z działającego źródła są", len(page.hits), 3)
check("błąd zgłoszony osobno", list(page.errors), ["kio"])
check("łączna liczba tylko z działającego", page.total, 13)

print("\n== filtrowanie po dacie publikacji (po naszej stronie) ==")
reg.http = FakeHttp()
reg.sources["ms"] = MsSource(ms_cfg, reg.http)
reg.sources["kio"] = KioSource(kio_cfg, reg.http)
page = reg.search(Query(date_field="publication", date_from="2026-08-20",
                        date_to="2026-08-25"), page=1)
check("zostają tylko opublikowane w zakresie",
      [h.publication_date for h in page.hits], ["2026-08-24"])

# ----------------------------------------------------------------------
print("\n== pamięć podręczna ==")
c = TTLCache(max_entries=2)
c.put("a", "1", ttl=60)
c.put("b", "2", ttl=60)
c.put("c", "3", ttl=60)
check("najstarszy wpis wypada po przekroczeniu limitu", c.get("a"), None)
check("nowsze zostają", (c.get("b"), c.get("c")), ("2", "3"))
c.put("d", "4", ttl=-1)
check("wpis po terminie nie jest zwracany", c.get("d"), None)

print("\n== formatowanie ==")
check("data po polsku", date_pl("2026-08-21"), "21 sierpnia 2026")
check("brak daty", date_pl(None), "—")
check("odmiana 1/3/5", (plural_pl(1, "a", "b", "c"), plural_pl(3, "a", "b", "c"),
                        plural_pl(5, "a", "b", "c")), ("a", "b", "c"))
check("odmiana 12", plural_pl(12, "a", "b", "c"), "c")

print("\n== pozostałe parsery ==")
check("data PL", parse_date("2 czerwca 2026"), "2026-06-02")
check("sygnatura", normalize_signature(" kio  1919 / 16 "), "KIO 1919/16")
check("typ z tytułu", detect_doc_type("postanowienie"), "postanowienie")
check("dwa typy", detect_doc_types("zarządzenie, uzasadnienie"),
      ["zarządzenie", "uzasadnienie"])
s, u = split_sentencja_uzasadnienie("WYROK\nsąd orzeka jak w sentencji powyżej\n"
                                    "U z a s a d n i e n i e\nDlatego tak.")
check("podział sentencja/uzasadnienie", (bool(s), u), (True, "Dlatego tak."))
p1 = extract_panel("Sąd w składzie:\nPrzewodniczący: SSO Jan Kowalski\n"
                   "Sędziowie: SSO Anna Nowak, SSR del. Piotr Zieliński\n"
                   "Protokolant: Maria Wójcik\npo rozpoznaniu")
check("skład z treści", [x["name"] for x in p1],
      ["Jan Kowalski", "Maria Wójcik", "Anna Nowak", "Piotr Zieliński"])

reg.close()
print("\n" + "=" * 62)
if failures:
    print(f"NIEPOWODZENIA ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("WSZYSTKIE TESTY ŹRÓDEŁ PRZESZŁY")
