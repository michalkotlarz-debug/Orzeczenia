"""Testy Store (SQLite) oraz logiki scalania wyrok+uzasadnienie w obserwator.py.

Bez sieci - wszystko na tymczasowej bazie SQLite, tak jak reszta zestawu.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orzeczenia.obserwator import (                                   # noqa: E402
    _fetch_and_store_each, _is_uzasadnienie_pair, _merge_wyrok_uzasadnienie,
    merge_existing_duplicates, merge_specific_pair)
from orzeczenia.store import RunResult, Store                         # noqa: E402

failures: list[str] = []


def check(label, got, expected=None, predicate=None):
    ok = predicate(got) if predicate else (got == expected)
    print(f"{'  OK  ' if ok else ' FAIL '} {label}: {got!r}" +
          ("" if ok or predicate else f"  (oczekiwano {expected!r})"))
    if not ok:
        failures.append(label)


def doc(doc_id, doc_type, **over):
    base = {
        "source": "ms", "doc_id": doc_id, "signature": "II K 971/25",
        "court": "Sąd Rejonowy w X", "judgment_date": "2025-05-01",
        "publication_date": "2025-05-10", "doc_type": doc_type, "doc_type_raw": doc_type,
        "sentencja": None, "uzasadnienie": None, "full_text": None,
        "judges": [], "thematic": [], "legal_basis": None, "importance": None,
        "source_url": f"http://example/{doc_id}",
    }
    base.update(over)
    return base


# ----------------------------------------------------------------------
print("== upsert_documents / get_document / search_fulltext ==")
with tempfile.TemporaryDirectory() as tmp:
    store = Store(f"sqlite:///{tmp}/test.sqlite3", keep_days=400)

    wyrok = doc("AAA", "wyrok", sentencja="Sentencja treść.", full_text="Sentencja treść.",
               judges=[{"name": "Jan Kowalski", "role": "przewodniczący"}],
               thematic=["Alimenty"], legal_basis="art. 1")
    check("pierwszy zapis dodaje", store.upsert_documents([wyrok]), 1)
    check("drugi zapis tego samego doc_id nie dubluje", store.upsert_documents([wyrok]), 0)
    check("liczniki per źródło", store.count(), {"ms": 1})

    got = store.get_document("ms", "AAA")
    check("pełny dokument z bazy", got is not None, True)
    check("sygnatura z bazy", got["signature"], "II K 971/25")
    check("hasła wracają jako lista, nie JSON", got["thematic"], ["Alimenty"])

    bez_tresci = doc("BEZ_TRESCI", "wyrok")   # full_text=None
    store.upsert_documents([bez_tresci])
    check("dokument bez treści nie liczy się jako dostępny", store.get_document("ms", "BEZ_TRESCI"),
         None)

    rows, total = store.search_fulltext("Sentencja")
    check("pełnotekstowe znajduje po treści", [r["doc_id"] for r in rows], ["AAA"])
    check("licznik trafień", total, 1)
    check("pusta fraza nie crashuje", store.search_fulltext(""), ([], 0))

    store.close()

# ----------------------------------------------------------------------
print("\n== search_advanced / count_advanced_by_source ==")
with tempfile.TemporaryDirectory() as tmp:
    store = Store(f"sqlite:///{tmp}/test.sqlite3", keep_days=400)
    d1 = doc("D1", "wyrok", signature="II K 971/25", full_text="treść 1",
             judges=[{"name": "Jan Kowalski", "role": "przewodniczący"}],
             legal_basis="art. 5 k.c.", judgment_date="2025-05-01",
             publication_date="2025-05-10")
    d2 = doc("D2", "wyrok", signature="III K 5/26", full_text="treść 2",
             judges=[{"name": "Anna Nowak", "role": "przewodniczący"}],
             legal_basis="art. 172 k.c.", judgment_date="2026-01-15",
             publication_date="2026-01-20")
    store.upsert_documents([d1, d2])

    check("bez żadnego kryterium nic nie zwraca (jak portal przy pustym)",
         store.search_advanced(), ([], 0))

    rows, total = store.search_advanced(signature="II K 971")
    check("filtr po sygnaturze (częściowej)", [r["doc_id"] for r in rows], ["D1"])
    check("licznik dla filtra po sygnaturze", total, 1)

    rows, _ = store.search_advanced(judge="Kowalski")
    check("filtr po sędzim (przez JSON judges)", [r["doc_id"] for r in rows], ["D1"])

    rows, _ = store.search_advanced(legal_basis="172")
    check("filtr po podstawie prawnej", [r["doc_id"] for r in rows], ["D2"])

    rows, _ = store.search_advanced(date_field="judgment", date_from="2026-01-01")
    check("filtr po dacie orzeczenia od", [r["doc_id"] for r in rows], ["D2"])

    rows, _ = store.search_advanced(date_field="publication", date_to="2025-12-31")
    check("filtr po dacie publikacji do", [r["doc_id"] for r in rows], ["D1"])

    counts = store.count_advanced_by_source(signature="K")
    check("liczniki per źródło dla filtra", counts, {"ms": 2})

    store.close()

# ----------------------------------------------------------------------
print("\n== latest(): kolejność chronologiczna wg wybranej daty, nie wg first_seen_at ==")
with tempfile.TemporaryDirectory() as tmp:
    store = Store(f"sqlite:///{tmp}/test.sqlite3", keep_days=400)
    # Zaimportowane w kolejności ODWROTNEJ niż ich daty - gdyby latest()
    # sortował po first_seen_at (kiedy MY to zobaczyliśmy), kolejność kart
    # byłaby nieprawidłowa względem wybranej zakładki "Data orzeczenia"/
    # "Data publikacji" (zgłoszony przypadek: 25 sierpnia przed 27 sierpnia).
    store.upsert_documents([
        doc("OLD", "wyrok", signature="A 1/26", judgment_date="2026-08-25",
           publication_date="2026-08-25", sentencja="s", full_text="s"),
        doc("NEW", "wyrok", signature="A 2/26", judgment_date="2026-08-27",
           publication_date="2026-08-20", sentencja="s", full_text="s"),
    ])
    check("wg daty publikacji: NEW ma wcześniejszą publikację niż OLD - powinien być NIŻEJ",
         [r["doc_id"] for r in store.latest(10, date_field="publication")], ["OLD", "NEW"])
    check("wg daty orzeczenia: NEW jest świeższy - powinien być WYŻEJ",
         [r["doc_id"] for r in store.latest(10, date_field="judgment")], ["NEW", "OLD"])
    store.close()

# ----------------------------------------------------------------------
print("\n== find_sibling / delete_document ==")
with tempfile.TemporaryDirectory() as tmp:
    store = Store(f"sqlite:///{tmp}/test.sqlite3", keep_days=400)
    wyrok = doc("AAA", "wyrok", sentencja="S.", full_text="S.")
    store.upsert_documents([wyrok])

    sib = store.find_sibling("ms", "II K 971/25", "Sąd Rejonowy w X",
                             "2025-05-01", "2025-05-10", exclude_doc_id="BBB")
    check("sibling znaleziony po sygnaturze/sądzie/datach", sib is not None and sib["doc_id"], "AAA")
    check("sibling wyklucza własne doc_id",
         store.find_sibling("ms", "II K 971/25", "Sąd Rejonowy w X",
                            "2025-05-01", "2025-05-10", exclude_doc_id="AAA"),
         None)
    check("brak sygnatury/sądu daje None",
         store.find_sibling("ms", None, None, "2025-05-01", "2025-05-10"), None)

    # Sprawdzone na żywo (sygnatura „II W 247/26"): portal potrafi dla
    # uzasadnienia zapisać INNĄ datę orzeczenia niż dla wyroku tej samej
    # sprawy - wystarczy więc zgodność samej daty publikacji.
    check("sibling znaleziony mimo różnej daty orzeczenia, gdy zgadza się data publikacji",
         store.find_sibling("ms", "II K 971/25", "Sąd Rejonowy w X",
                            "2099-01-01", "2025-05-10") is not None,
         True)
    check("sibling NIE znaleziony, gdy różni się i data orzeczenia, i data publikacji",
         store.find_sibling("ms", "II K 971/25", "Sąd Rejonowy w X",
                            "2099-01-01", "2099-01-01"),
         None)

    store.delete_document("ms", "AAA")
    check("delete_document faktycznie usuwa", store.count(), {})
    store.close()

# ----------------------------------------------------------------------
print("\n== scalanie wyrok+uzasadnienie (rozpoznane automatycznie) ==")
wyrok = doc("AAA_wyrok", "wyrok", sentencja="Sentencja treść.", full_text="Sentencja treść.",
           judges=[{"name": "Jan Kowalski", "role": "przewodniczący"}],
           thematic=["Alimenty"], legal_basis="art. 1")
uzas = doc("BBB_uzas", "uzasadnienie", uzasadnienie="Uzasadnienie treść.",
          full_text="Uzasadnienie treść.",
          judges=[{"name": "Anna Nowak", "role": "protokolant"}],
          thematic=["Alimenty", "Władza rodzicielska"], importance="wysoka")

check("para wyrok+uzasadnienie rozpoznana", _is_uzasadnienie_pair(wyrok, uzas), True)
check("dwa wyroki to NIE para", _is_uzasadnienie_pair(wyrok, doc("CCC", "wyrok")), False)
check("dwa uzasadnienia to NIE para",
     _is_uzasadnienie_pair(uzas, doc("DDD", "uzasadnienie")), False)

merged, absorbed = _merge_wyrok_uzasadnienie(uzas, wyrok)   # kolejność argumentów nieistotna
check("scalony dokument zostaje pod doc_id wyroku", merged["doc_id"], "AAA_wyrok")
check("wchłonięty to doc_id uzasadnienia", absorbed, "BBB_uzas")
check("scalona treść zawiera obie części",
     "Sentencja" in merged["full_text"] and "Uzasadnienie" in merged["full_text"], True)
check("pole uzasadnienie wypełnione z drugiego dokumentu",
     merged["uzasadnienie"], "Uzasadnienie treść.")
check("skład orzekający to suma obu (bez duplikatów)",
     sorted(j["name"] for j in merged["judges"]), ["Anna Nowak", "Jan Kowalski"])
check("hasła tematyczne to suma obu (bez duplikatów)",
     sorted(merged["thematic"]), ["Alimenty", "Władza rodzicielska"])
check("legal_basis zostaje z wyroku, gdy uzasadnienie go nie ma", merged["legal_basis"], "art. 1")
check("importance dociągnięte z uzasadnienia, gdy wyrok go nie ma", merged["importance"], "wysoka")

# ----------------------------------------------------------------------
print("\n== merge_existing_duplicates (backfill po już zaimportowanych) ==")
with tempfile.TemporaryDirectory() as tmp:
    store = Store(f"sqlite:///{tmp}/test.sqlite3", keep_days=400)

    class FakeCfg:
        class store:
            url = f"sqlite:///{tmp}/test.sqlite3"
            keep_days = 400

    w1 = doc("CCC1", "wyrok", signature="X K 1/25", sentencja="S", full_text="S")
    w2 = doc("CCC2", "wyrok", signature="X K 1/25", sentencja="S2", full_text="S2")
    # jak sygnatura "II W 247/26" na żywo: ta sama sygnatura/sąd/data
    # publikacji, ale INNA data orzeczenia - musi się scalić mimo to.
    w3 = doc("DDD_wyrok", "wyrok", signature="II W 247/26", sentencja="S3",
            full_text="S3", judgment_date="2026-08-06", publication_date="2026-08-28")
    u3 = doc("DDD_uzas", "uzasadnienie", signature="II W 247/26",
            uzasadnienie="U3", full_text="U3",
            judgment_date="2026-08-20", publication_date="2026-08-28")
    # jak sygnatura "I AGa 84/18" (Sąd Apelacyjny w Rzeszowie) na żywo: OBA
    # wiersze mają doc_type "wyrok" (portal oznacza drugi jako "wyrok z
    # uzasadnieniem", ale detekcja typu z tytułu i tak wykrywa "wyrok") -
    # jeden to sama sentencja, drugi to kompletna wersja (sentencja+
    # uzasadnienie) - kompletna ma zastąpić okrojoną, bez łączenia treści.
    bare = doc("EEE_bez", "wyrok", signature="I AGa 84/18", sentencja="Sentencja bez uzasadnienia.",
              full_text="Sentencja bez uzasadnienia.")
    full = doc("EEE_z", "wyrok", signature="I AGa 84/18", sentencja="Sentencja bez uzasadnienia.",
              uzasadnienie="I dochodzi jeszcze uzasadnienie.",
              full_text="Sentencja bez uzasadnienia. I dochodzi jeszcze uzasadnienie.")
    store.upsert_documents([wyrok, uzas, w1, w2, w3, u3, bare, full])
    check("osiem wierszy przed porządkowaniem", store.count(), {"ms": 8})

    stats = merge_existing_duplicates(FakeCfg(), store=store, source="ms")
    check("cztery grupy zdublowane", stats["grup"], 4)
    check("dwie prawdziwe pary scalone", stats["scalonych"], 2)
    check("jedna kompletna wersja zastąpiła okrojoną", stats["zastapionych"], 1)
    check("jedna grupa niejednoznaczna (dwa wyroki) pominięta",
         stats["pominietych_niejednoznacznych"], 1)
    check("po scaleniu zostaje pięć wierszy", store.count(), {"ms": 5})
    check("okrojona wersja zniknęła (I AGa 84/18)", store.get_document("ms", "EEE_bez"), None)
    check("kompletna wersja zostaje pod własnym doc_id, treść niezmieniona",
         (store.get_document("ms", "EEE_z") or {}).get("full_text"),
         "Sentencja bez uzasadnienia. I dochodzi jeszcze uzasadnienie.")
    check("para z różną datą orzeczenia scaliła się (II W 247/26)",
         store.get_document("ms", "DDD_uzas"), None)
    check("scalony DDD_wyrok ma teraz obie części",
         "S3" in (store.get_document("ms", "DDD_wyrok") or {}).get("full_text", "") and
         "U3" in (store.get_document("ms", "DDD_wyrok") or {}).get("full_text", ""),
         True)
    check("uzasadnienie zniknęło jako osobny wiersz", store.get_document("ms", "BBB_uzas"), None)
    check("wchłonięte oznaczone jako pominięte", store.skipped_ids("ms", ["BBB_uzas"]),
         {"BBB_uzas"})
    check("prawdziwe duplikaty (CCC1/CCC2) nietknięte",
         (store.get_document("ms", "CCC1") is not None,
          store.get_document("ms", "CCC2") is not None),
         (True, True))

    stats2 = merge_existing_duplicates(FakeCfg(), store=store, source="ms")
    check("ponowne uruchomienie nic już nie scala (idempotentność)", stats2["scalonych"], 0)
    store.close()

# ----------------------------------------------------------------------
print("\n== merge_specific_pair (ręczne scalenie mylącej etykiety) ==")
with tempfile.TemporaryDirectory() as tmp:
    store = Store(f"sqlite:///{tmp}/test.sqlite3", keep_days=400)
    keep = doc("KEEP1", "wyrok", signature="II K 771/15", sentencja="Sentencja.",
              full_text="Sentencja.")
    # doc_type "zarządzenie" mimo że treściowo to uzasadnienie - patrz II K 771/15
    absorb = doc("ABSORB1", "zarządzenie", signature="II K 771/15",
                full_text="Prawdziwa treść uzasadnienia mimo etykiety zarządzenie.",
                legal_basis="art. 5")
    store.upsert_documents([keep, absorb])

    ok = merge_specific_pair(store, "ms", "KEEP1", "ABSORB1")
    check("scalenie się powiodło", ok, True)
    merged = store.get_document("ms", "KEEP1")
    check("treść wchłonięta jako uzasadnienie", merged["uzasadnienie"],
         "Prawdziwa treść uzasadnienia mimo etykiety zarządzenie.")
    check("legal_basis dociągnięty od wchłoniętego", merged["legal_basis"], "art. 5")
    check("wchłonięty dokument zniknął z bazy", store.get_document("ms", "ABSORB1"), None)

    check("nieistniejące doc_id nie crashuje, zwraca False",
         merge_specific_pair(store, "ms", "NOPE1", "NOPE2"), False)
    store.close()

# ----------------------------------------------------------------------
print("\n== _fetch_and_store_each: samo uzasadnienie bez wyroku NIE jest publikowane ==")
with tempfile.TemporaryDirectory() as tmp:
    store = Store(f"sqlite:///{tmp}/test.sqlite3", keep_days=400)

    class FakeRegistry:
        """Zwraca zawsze samo uzasadnienie - jakby MsSource.document() nie
        znalazł na żywo żadnej pasującej pozycji wyroku (jeszcze
        nieopublikowanej albo jeszcze nie napotkanej w tym przebiegu)."""
        def document(self, source, doc_id):
            return {
                "source": "ms", "doc_id": doc_id, "signature": "II W 999/26",
                "court": "Sąd Rejonowy w Z", "doc_type": "uzasadnienie",
                "judgment_date": "2026-08-01", "publication_date": "2026-08-10",
                "uzasadnienie": "Treść.", "full_text": "Treść.",
                "judges": [], "thematic": [], "source_url": "http://x",
            }

    result = RunResult(source="ms")
    _fetch_and_store_each(FakeRegistry(), store, "ms", ["SOLO_UZAS"], result)
    check("samo uzasadnienie bez wyroku NIE trafia do bazy", store.count(), {})
    check("nie oznaczone jako trwale pominięte (ma szansę w kolejnym przebiegu)",
         store.skipped_ids("ms", ["SOLO_UZAS"]), set())
    check("nic nie policzone jako dodane", result.added, 0)
    store.close()

# ----------------------------------------------------------------------
print("\n" + "=" * 62)
if failures:
    print(f"NIEPOWODZENIA ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("Wszystko przeszło.")
