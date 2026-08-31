"""Testy Store (SQLite) oraz logiki scalania wyrok+uzasadnienie w obserwator.py.

Bez sieci - wszystko na tymczasowej bazie SQLite, tak jak reszta zestawu.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orzeczenia.obserwator import (                                   # noqa: E402
    _is_uzasadnienie_pair, _merge_wyrok_uzasadnienie, merge_existing_duplicates,
    merge_specific_pair)
from orzeczenia.store import Store                                    # noqa: E402

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
    store.upsert_documents([wyrok, uzas, w1, w2])
    check("cztery wiersze przed porządkowaniem", store.count(), {"ms": 4})

    stats = merge_existing_duplicates(FakeCfg(), store=store, source="ms")
    check("dwie grupy zdublowane", stats["grup"], 2)
    check("jedna prawdziwa para scalona", stats["scalonych"], 1)
    check("jedna grupa niejednoznaczna (dwa wyroki) pominięta",
         stats["pominietych_niejednoznacznych"], 1)
    check("po scaleniu zostają trzy wiersze", store.count(), {"ms": 3})
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
print("\n" + "=" * 62)
if failures:
    print(f"NIEPOWODZENIA ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("Wszystko przeszło.")
