"""Testy MsSource: odsiewanie wyników bez treści i scalanie wyrok+uzasadnienie
na żywo (wyszukiwarka i pojedynczy dokument) - patrz sygnatura „IX U 1515/12"
(brak treści) i „II K 971/25" (wyrok+uzasadnienie jako dwa osobne dokumenty).

Bez ani jednego prawdziwego zapytania sieciowego - atrapa klienta HTTP.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orzeczenia.config import CacheConfig, SourceConfig            # noqa: E402
from orzeczenia.http import SourceUnavailable                      # noqa: E402
from orzeczenia.sources.base import Hit, Query                     # noqa: E402
from orzeczenia.sources.ms_gov import MsSource                     # noqa: E402

failures: list[str] = []


def check(label, got, expected=None, predicate=None):
    ok = predicate(got) if predicate else (got == expected)
    print(f"{'  OK  ' if ok else ' FAIL '} {label}: {got!r}" +
          ("" if ok or predicate else f"  (oczekiwano {expected!r})"))
    if not ok:
        failures.append(label)


CFG = SourceConfig(label="Sądy powszechne", base_url="https://orzeczenia.ms.gov.pl")


def hit(doc_id, doc_type, signature="II K 971/25", court="Sąd Rejonowy w X",
       judgment_date="2025-05-01", publication_date="2025-05-10", excerpt=None):
    return Hit(source="ms", doc_id=doc_id, signature=signature, doc_type=doc_type,
              court=court, judgment_date=judgment_date, publication_date=publication_date,
              excerpt=excerpt, source_url=f"https://orzeczenia.ms.gov.pl/content/$N/{doc_id}")


def details_html(doc_id, title, signature, court, judgment_date_pl, publication_date_pl):
    return (f'<html><body><div id="content"><h2>{title}</h2>'
           f'<dl><dt>Sygnatura:</dt><dd>{signature}</dd>'
           f'<dt>Sąd:</dt><dd>{court}</dd>'
           f'<dt>Data orzeczenia:</dt><dd>{judgment_date_pl}</dd>'
           f'<dt>Data publikacji:</dt><dd>{publication_date_pl}</dd></dl>'
           f'</div></body></html>')


def content_html(text):
    return f'<html><body><div id="content"><h2>t</h2></div><div class="single_wrapper"><div class="single_result"><p>{text}</p></div></div></body></html>'


def listing_html(*rows):
    """rows: (doc_id, signature, doc_type, court, jd_pl, pd_pl)"""
    blocks = []
    for doc_id, sig, typ, court, jd, pd in rows:
        blocks.append(
            f'<div class="single_result"><div class="title"><h4>'
            f'<a href="/details/$N/{doc_id}">{sig}</a></h4><p>{typ}</p><p>{court}</p>'
            f'<p>Data orzeczenia: {jd}</p><p>Data publikacji: {pd}</p></div>'
            f'<div class="excerpt"><blockquote>{typ} treść</blockquote></div></div>')
    return ('<html><body><section id="sorting"><span class="big_number">'
           f'{len(rows)}</span></section><section id="results">' + "".join(blocks)
           + "</section></body></html>")


# ----------------------------------------------------------------------
print("== _filter_and_merge_hits: odsiewanie bez treści ==")


class ContentOnlyHttp:
    """Odpowiada tylko na /content/ - do testów _has_content/_filter_and_merge_hits."""
    cache_cfg = CacheConfig()

    def __init__(self, missing: set[str]):
        self.missing = missing

    def close(self): pass

    def get(self, url, *, ttl=None):
        doc_id = url.rsplit("/", 1)[-1]
        if doc_id in self.missing:
            raise SourceUnavailable("HTTP 400 (test)")
        return "<html>ok</html>"


ms = MsSource(CFG, ContentOnlyHttp(missing={"BEZ_TRESCI"}))
hits = [hit("MA_TRESC", "wyrok", signature="A 1/25"),
       hit("BEZ_TRESCI", "wyrok", signature="A 2/25")]
out = ms._filter_and_merge_hits(hits)
check("dokument bez treści w ogóle nie trafia do wyników", [h.doc_id for h in out],
     ["MA_TRESC"])

print("\n== _filter_and_merge_hits: scalanie pary wyrok+uzasadnienie na tej samej stronie ==")
ms2 = MsSource(CFG, ContentOnlyHttp(missing=set()))
wyrok_h = hit("WYROK1", "wyrok", excerpt="treść wyroku")
uzas_h = hit("UZAS1", "uzasadnienie", excerpt="treść uzasadnienia")
out2 = ms2._filter_and_merge_hits([wyrok_h, uzas_h])
check("para scalona w jedną kartę", len(out2), 1)
check("scalona karta to doc_id wyroku (nie uzasadnienia)", out2[0].doc_id, "WYROK1")
check("brakujący excerpt karty dociągnięty z uzasadnienia",
     out2[0].excerpt, "treść wyroku")   # wyrok miał już własny excerpt - zostaje
out2b = ms2._filter_and_merge_hits([hit("WYROK2", "wyrok", excerpt=None),
                                    hit("UZAS2", "uzasadnienie", excerpt="treść uzasadnienia")])
check("brak własnego excerptu -> dociągnięty z drugiej połowy pary",
     out2b[0].excerpt, "treść uzasadnienia")

out3 = ms2._filter_and_merge_hits([hit("W1", "wyrok", signature="X 1/25"),
                                   hit("W2", "wyrok", signature="X 1/25")])
check("dwa wyroki tej samej sygnatury (prawdziwy duplikat) NIE są scalane", len(out3), 2)

# Sprawdzone na żywo (sygnatura „II W 247/26"): portal potrafi dla
# uzasadnienia zapisać INNĄ datę orzeczenia niż dla wyroku tej samej sprawy -
# ta sama sygnatura+sąd+data PUBLIKACJI wystarcza.
out4 = ms2._filter_and_merge_hits([
    hit("W247", "wyrok", signature="II W 247/26", judgment_date="2026-08-06",
       publication_date="2026-08-28"),
    hit("U247", "uzasadnienie", signature="II W 247/26", judgment_date="2026-08-20",
       publication_date="2026-08-28")])
check("para scalona mimo różnej daty orzeczenia (zgadza się data publikacji)",
     [h.doc_id for h in out4], ["W247"])

# Ale inny sąd to naprawdę inna sprawa - nie wolno scalić.
out5 = ms2._filter_and_merge_hits([
    hit("W5", "wyrok", signature="II K 1/25", court="Sąd Rejonowy A"),
    hit("U5", "uzasadnienie", signature="II K 1/25", court="Sąd Rejonowy B")])
check("ta sama sygnatura w INNYM sądzie NIE jest scalana", len(out5), 2)

print("\n== _filter_and_merge_hits: brak treści u WSZYSTKICH na stronie to nie 'brak wyników' ==")
# Sprawdzone na żywo: portal potrafi oddać 400 na /content/ dla całej strony
# naraz (chwilowa blokada/przeciążenie, nie brak treści u każdej pozycji z
# osobna) - takiej sytuacji nie wolno pokazać jako pustą listę wyników.
ms_blocked = MsSource(CFG, ContentOnlyHttp(missing={"A1", "A2", "A3"}))
try:
    ms_blocked._filter_and_merge_hits([hit("A1", "wyrok"), hit("A2", "wyrok"), hit("A3", "wyrok")])
    check("brak treści u wszystkich zgłoszony jako błąd, nie cicha pusta lista",
         "brak wyjątku", "SourceUnavailable")
except SourceUnavailable:
    check("brak treści u wszystkich zgłoszony jako błąd, nie cicha pusta lista",
         "SourceUnavailable", "SourceUnavailable")

# ----------------------------------------------------------------------
print("\n== document(): dokument bez treści i bez siostrzanej pozycji -> błąd ==")


class NoContentNoSiblingHttp:
    cache_cfg = CacheConfig()

    def close(self): pass

    def get(self, url, *, ttl=None):
        if "/details/$N/" in url:
            return details_html("DOC1", "IX U 1515/12 - wyrok Sąd Okręgowy w Gliwicach z 2015-01-26",
                                "IX U 1515/12", "Sąd Okręgowy w Gliwicach",
                                "26 stycznia 2015", "12 marca 2015")
        if "/content/$N/" in url:
            raise SourceUnavailable("HTTP 400 (test)")
        if "/search/advanced/" in url:
            return listing_html()   # pusto - brak jakiejkolwiek siostrzanej pozycji
        raise AssertionError(url)


ms3 = MsSource(CFG, NoContentNoSiblingHttp())
try:
    ms3.document("DOC1")
    check("wyjątek przy braku treści i braku siostry", "brak wyjątku", "SourceUnavailable")
except SourceUnavailable as exc:
    check("wyjątek przy braku treści i braku siostry", "SourceUnavailable", "SourceUnavailable")

# ----------------------------------------------------------------------
print("\n== document(): brak własnej treści, ale jest siostrzana pozycja z treścią ==")


class SiblingHasContentHttp:
    cache_cfg = CacheConfig()

    def close(self): pass

    def get(self, url, *, ttl=None):
        if "/search/advanced/" in url:
            return listing_html(
                ("SIB1", "II K 771/15", "uzasadnienie", "Sąd Rejonowy w Tarnobrzegu",
                 "20 stycznia 2016", "19 lipca 2016"))
        if "/details/$N/DOC1" in url:
            return details_html("DOC1", "II K 771/15 - wyrok Sąd Rejonowy w Tarnobrzegu z 2016-01-20",
                                "II K 771/15", "Sąd Rejonowy w Tarnobrzegu",
                                "20 stycznia 2016", "19 lipca 2016")
        if "/content/$N/DOC1" in url:
            raise SourceUnavailable("HTTP 400 (test)")
        if "/details/$N/SIB1" in url:
            return details_html("SIB1", "II K 771/15 - uzasadnienie Sąd Rejonowy w Tarnobrzegu z 2016-01-20",
                                "II K 771/15", "Sąd Rejonowy w Tarnobrzegu",
                                "20 stycznia 2016", "19 lipca 2016")
        if "/content/$N/SIB1" in url:
            return content_html("UZASADNIENIE tresc uzasadnienia z sib1")
        raise AssertionError(url)


ms4 = MsSource(CFG, SiblingHasContentHttp())
doc = ms4.document("DOC1")
check("dokument bez własnej treści pokazuje treść siostry",
     "uzasadnienie" in (doc.get("full_text") or "").lower(), True)
# własnej treści DOC1 nie ma wcale (i nigdy nie będzie) - pokazujemy więc
# to, co siostrzana pozycja faktycznie ma, pod JEJ doc_id (linki - "Pobierz
# tekst" itd. - działają, bo idą za zwróconym słownikiem, nie za URL-em).
check("scalony dokument zostaje pod ID siostry (jedyne co realnie ma treść)",
     doc["doc_id"], "SIB1")

# ----------------------------------------------------------------------
print("\n== document(): dokument to samo uzasadnienie -> dociąga wyrok tej samej sprawy ==")


class OwnIsUzasHttp:
    cache_cfg = CacheConfig()

    def close(self): pass

    def get(self, url, *, ttl=None):
        if "/search/advanced/" in url:
            return listing_html(
                ("RULING1", "III K 5/26", "wyrok", "Sąd Rejonowy w Y",
                 "1 lutego 2026", "10 lutego 2026"))
        if "/details/$N/UZAS1" in url:
            return details_html("UZAS1", "III K 5/26 - uzasadnienie Sąd Rejonowy w Y z 2026-02-01",
                                "III K 5/26", "Sąd Rejonowy w Y", "1 lutego 2026", "10 lutego 2026")
        if "/content/$N/UZAS1" in url:
            return content_html("UZASADNIENIE tresc wlasnego uzasadnienia")
        if "/details/$N/RULING1" in url:
            return details_html("RULING1", "III K 5/26 - wyrok Sąd Rejonowy w Y z 2026-02-01",
                                "III K 5/26", "Sąd Rejonowy w Y", "1 lutego 2026", "10 lutego 2026")
        if "/content/$N/RULING1" in url:
            return content_html("WYROK sentencja rozstrzygniecia")
        raise AssertionError(url)


ms5 = MsSource(CFG, OwnIsUzasHttp())
doc2 = ms5.document("UZAS1")
check("otwarcie samego uzasadnienia zwraca scalony dokument pod ID wyroku",
     doc2["doc_id"], "RULING1")
check("scalona treść zawiera sentencję wyroku",
     "sentencja" in (doc2.get("full_text") or "").lower(), True)
check("scalona treść zawiera uzasadnienie",
     "uzasadnienie" in (doc2.get("full_text") or "").lower(), True)

# ----------------------------------------------------------------------
print("\n" + "=" * 62)
if failures:
    print(f"NIEPOWODZENIA ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("Wszystko przeszło.")
