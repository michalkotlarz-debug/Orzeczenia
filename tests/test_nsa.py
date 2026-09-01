"""Testy źródła CBOSA (orzeczenia.nsa.gov.pl) oraz bazy obserwatora.

Ani jednego zapytania sieciowego - wszystko na zapisanych fragmentach HTML
z tests/fixtures i na tymczasowej bazie SQLite.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orzeczenia.config import Config, SourceConfig                   # noqa: E402
from orzeczenia.obserwator import build_query                        # noqa: E402
from orzeczenia.parse.common import court_level, normalize_signature  # noqa: E402
from orzeczenia.sources.base import Hit, Query                       # noqa: E402
from orzeczenia.sources.nsa_cbosa import NsaSource                   # noqa: E402
from orzeczenia.sources.registry import Registry                     # noqa: E402
from orzeczenia.store import RunResult, Store                        # noqa: E402

FX = Path(__file__).parent / "fixtures"
fx = lambda n: (FX / n).read_text(encoding="utf-8")                  # noqa: E731

failures: list[str] = []


def check(label, got, expected=None, predicate=None):
    ok = predicate(got) if predicate else (got == expected)
    print(f"{'  OK  ' if ok else ' FAIL '} {label}: {got!r}" +
          ("" if ok or predicate else f"  (oczekiwano {expected!r})"))
    if not ok:
        failures.append(label)


CFG = SourceConfig(label="Sądy administracyjne",
                   base_url="https://orzeczenia.nsa.gov.pl", ignore_robots=True)
SRC = NsaSource(CFG, None)

# ----------------------------------------------------------------------
print("== adresy CBOSA ==")
check("dokument", SRC.doc_url("226B5A6CD0"),
      "https://orzeczenia.nsa.gov.pl/doc/226B5A6CD0")
check("kolejna strona", SRC.page_url(3), "https://orzeczenia.nsa.gov.pl/cbo/find?p=3")
check("wyszukiwanie", SRC.search_url(), "https://orzeczenia.nsa.gov.pl/cbo/search")
check("czyszczenie sesji", SRC.query_url(), "https://orzeczenia.nsa.gov.pl/cbo/query")

# ----------------------------------------------------------------------
print("\n== formularz wyszukiwania ==")
f = SRC.form_data(Query(phrase="podatek", date_from="2026-08-01"))
# CBOSA przy samym odDaty oddaje ZERO wyników - zakres musi być domknięty
check("data od dopełniona datą do", (f["odDaty"], f["doDaty"]),
      ("2026-08-01", "2099-12-31"))
f = SRC.form_data(Query(date_to="2026-08-31"))
check("data do dopełniona datą od", (f["odDaty"], f["doDaty"]),
      ("1980-01-01", "2026-08-31"))
f = SRC.form_data(Query(phrase="podatek"))
check("bez dat pola puste", (f["odDaty"], f["doDaty"]), ("", ""))
f = SRC.form_data(Query(judge="Kowalski", thematic="podatki", legal_basis="art. 86"))
check("sędzia, hasło i przepis trafiają do właściwych pól",
      (f["sedziowie"], f["hasla"], f["przepisy"]), ("Kowalski", "podatki", "art. 86"))

check("filtr daty publikacji pomija CBOSA",
      SRC.search(Query(date_field="publication", date_from="2026-01-01")), ([], 0))
check("CBOSA nie prowadzi daty publikacji", SRC.supports_publication_date, False)

# ----------------------------------------------------------------------
print("\n== lista wyników ==")
lista = fx("nsa_lista.html")
check("liczba znalezionych", SRC.parse_count(lista), 763)
check("brak wyników to zero, nie wyjątek", SRC.parse_count(fx("nsa_pusto.html")), 0)
check("pusta lista bez trafień", SRC.parse_results(fx("nsa_pusto.html")), [])

hits = SRC.parse_results(lista)
check("identyfikatory dokumentów", [h.doc_id for h in hits],
      ["759CC12522", "226B5A6CD0", "8FB2F6A835"])
check("link 'orzeczenia powiązane' nie jest osobnym wynikiem", len(hits), 3)
a, b, c = hits
check("tytuł rozbity na części",
      (a.signature, a.doc_type, a.court, a.judgment_date),
      ("I FSK 1865/23", "wyrok", "NSA", "2026-08-28"))
check("kod sądu w sygnaturze zachowuje małe litery", b.signature, "I SA/Łd 269/26")
check("sąd wojewódzki z tytułu", b.court, "WSA w Łodzi")
check("postanowienie rozpoznane", c.doc_type, "postanowienie")
check("streszczenie z następnego wiersza tabeli",
      "podatku od towarów i usług" in (a.excerpt or ""), True)
check("brak streszczenia to None, nie tytuł sąsiada", c.excerpt, None)
check("adres oryginału", b.source_url,
      "https://orzeczenia.nsa.gov.pl/doc/226B5A6CD0")

# ----------------------------------------------------------------------
print("\n== pojedyncze orzeczenie ==")
d = SRC.parse_document("226B5A6CD0", fx("nsa_dokument.html"))
check("sygnatura", d["signature"], "I SA/Łd 269/26")
check("data orzeczenia", d["judgment_date"], "2026-08-27")
check("data wpływu", d["received_date"], "2026-05-14")
check("brak daty publikacji", d["publication_date"], None)
check("sąd", d["court"], "Wojewódzki Sąd Administracyjny w Łodzi")
check("szczebel sądu", d["court_level"], "WSA")
check("typ", d["doc_type"], "wyrok")
check("treść wyniku", d["outcome"], "Oddalono skargę")
check("skarżony organ", d["authority"], "Dyrektor Krajowej Informacji Skarbowej")
check("prawomocność", d["final"], "nieprawomocne")
check("hasła tematyczne", d["thematic"],
      ["Podatek od towarów i usług", "Interpretacje podatkowe"])
check("powołane przepisy", d["legal_basis"], "Dz.U. 2024 poz 361 art. 86 ust. 1")

check("skład orzekający", [j["name"] for j in d["judges"]],
      ["Agnieszka Gortych-Ratajczyk", "Grzegorz Potiopa", "Tomasz Furmanek"])
check("przewodniczący", d["chairman"], "Agnieszka Gortych-Ratajczyk")
check("sprawozdawca odnotowany", d["judges"][1].get("note"), "sprawozdawca")

check("sentencja osobno", d["sentencja"].startswith("Wojewódzki Sąd Administracyjny"), True)
check("uzasadnienie osobno", d["uzasadnienie"].startswith("Zaskarżoną interpretacją"), True)
check("pełny tekst zawiera obie części",
      d["sentencja"] in d["full_text"] and d["uzasadnienie"] in d["full_text"], True)

# ----------------------------------------------------------------------
print("\n== sygnatury i szczeble sądów administracyjnych ==")
check("mała litera w kodzie sądu przetrwa normalizację",
      normalize_signature("i sa/łd 269 / 26"), "I SA/Łd 269/26")
check("zwykła sygnatura bez zmian", normalize_signature("II C 123/20"), "II C 123/20")
check("NSA rozpoznany", court_level("Naczelny Sąd Administracyjny"), "NSA")
check("WSA rozpoznany", court_level("Wojewódzki Sąd Administracyjny w Łodzi"), "WSA")

# ----------------------------------------------------------------------
print("\n== rejestr źródeł ==")
reg = Registry(Config(nsa=CFG, ms=SourceConfig(enabled=False),
                      kio=SourceConfig(enabled=False)))
check("CBOSA zarejestrowane", sorted(reg.sources), ["nsa"])
page = reg.search(Query(date_field="publication", date_from="2026-01-01"), page=1)
check("pominięcie opisane notatką, nie błędem", list(page.notes), ["nsa"])
check("to nie jest błąd serwisu", page.errors, {})
reg.close()

# ----------------------------------------------------------------------
print("\n== obserwator: zapytania o nowości ==")
check("dla portalu MS sortujemy po dacie publikacji",
      build_query("ms", 14).sort, "pub_desc")
q_nsa = build_query("nsa", 14)
check("dla CBOSA sitem jest okno dat orzeczenia",
      bool(q_nsa.date_from and q_nsa.date_to), True)

# ----------------------------------------------------------------------
print("\n== baza obserwatora ==")
with tempfile.TemporaryDirectory() as tmp:
    store = Store(f"sqlite:///{tmp}/test.sqlite3", keep_days=400)
    nowe = [
        Hit(source="nsa", doc_id="226B5A6CD0", signature="I SA/Łd 269/26",
            doc_type="wyrok", court="WSA w Łodzi", judgment_date="2026-08-27",
            thematic=["Podatek od towarów i usług"],
            source_url="https://orzeczenia.nsa.gov.pl/doc/226B5A6CD0"),
        Hit(source="ms", doc_id="abc", signature="I C 1/24", judgment_date="2026-08-01",
            publication_date="2026-08-20", source_url="https://orzeczenia.ms.gov.pl/x"),
    ]
    check("pierwszy zapis dodaje wszystko", store.upsert(nowe), 2)
    check("drugi zapis nie dubluje", store.upsert(nowe), 0)
    check("liczniki per źródło", store.count(), {"ms": 1, "nsa": 1})
    check("znane identyfikatory", store.known_ids("nsa", ["226B5A6CD0", "XXX"]),
          {"226B5A6CD0"})

    rows = store.latest(10, source="nsa")
    check("odczyt po źródle", len(rows), 1)
    check("hasła wracają jako lista, nie JSON", rows[0]["thematic"],
          ["Podatek od towarów i usług"])
    check("adres w aplikacji", rows[0]["url"], "/orzeczenie/nsa/226B5A6CD0")
    check("powrót do obiektu Hit", store.to_hit(rows[0]).signature, "I SA/Łd 269/26")
    check("filtr po dacie orzeczenia",
          [r["doc_id"] for r in store.latest(10, since="2026-08-15")], ["226B5A6CD0"])
    check("filtr po dacie publikacji (używane przez /nowe)",
          [r["doc_id"] for r in store.latest(10, since="2026-08-15", date_field="publication")],
          ["abc"])

    store.record_run(RunResult(source="nsa", pages=2, seen=20, added=2),
                     "2026-08-29T05:00:00+00:00")
    runs = store.runs(5)
    check("przebieg zapisany", (runs[0]["source"], runs[0]["added"], runs[0]["status"]),
          ("nsa", 2, "ok"))
    check("świeże wpisy przeżywają porządki", store.prune(), 0)
    store.close()

# ----------------------------------------------------------------------
print("\n" + "=" * 62)
if failures:
    print(f"NIEPOWODZENIA ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("Wszystko przeszło.")
