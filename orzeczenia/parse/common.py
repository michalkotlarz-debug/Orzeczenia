"""Wspólne narzędzia parsujące: daty PL, sygnatury, nazwiska, typ orzeczenia."""
from __future__ import annotations

import re
import unicodedata
from datetime import date

PL_MONTHS = {
    "stycznia": 1, "styczeń": 1, "styczen": 1,
    "lutego": 2, "luty": 2,
    "marca": 3, "marzec": 3,
    "kwietnia": 4, "kwiecień": 4, "kwiecien": 4,
    "maja": 5, "maj": 5,
    "czerwca": 6, "czerwiec": 6,
    "lipca": 7, "lipiec": 7,
    "sierpnia": 8, "sierpień": 8, "sierpien": 8,
    "września": 9, "wrzesnia": 9, "wrzesień": 9, "wrzesien": 9,
    "października": 10, "pazdziernika": 10, "październik": 10, "pazdziernik": 10,
    "listopada": 11, "listopad": 11,
    "grudnia": 12, "grudzień": 12, "grudzien": 12,
}

_WS = re.compile(r"[\s ​]+")


def squash(text: str | None) -> str:
    return _WS.sub(" ", (text or "")).strip()


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")


# ----------------------------------------------------------------------
# HTML -> tekst z zachowaniem podziału na bloki
# ----------------------------------------------------------------------
BLOCK_TAGS = {
    "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table",
    "section", "article", "blockquote", "ul", "ol", "dl", "dt", "dd", "hr", "pre",
}


def html_text(node) -> str:
    """Zamienia węzeł BeautifulSoup na tekst: jeden wiersz na blok.

    Dwa problemy, które to rozwiązuje:
    1. Elementy INLINE (<span>, <strong>, <a>) muszą być sklejone BEZ separatora -
       inaczej treść z KIO (<p><span>Przewodniczący: </span><span>Jan Kowalski</span></p>)
       rozpada się na dwa wiersze i psuje rozpoznawanie składu orzekającego.
    2. Znaki nowej linii z formatowania źródła (wcięcia w HTML) NIE mogą łamać
       akapitu - inaczej "1. zmienia zaskarżony wyrok" rozpada się na dwie linie.
    """
    if node is None:
        return ""
    SEP = "\x00"
    for bad in node.find_all(["script", "style"]):
        bad.decompose()
    for tag in node.find_all(BLOCK_TAGS):
        tag.insert_before(SEP)
        tag.insert_after(SEP)
    raw = node.get_text("")
    raw = (raw.replace("\xa0", " ").replace("​", "")
              .replace("‑", "-").replace("\n", " ").replace("\r", " "))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.split(SEP)]
    return "\n".join(ln for ln in lines if ln).strip()


# ----------------------------------------------------------------------
# daty
# ----------------------------------------------------------------------
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b")
_PL = re.compile(r"\b(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})\b", re.IGNORECASE)


def parse_date(text: str | None) -> str | None:
    """Zwraca datę w formacie ISO (YYYY-MM-DD) albo None."""
    if not text:
        return None
    t = squash(text)
    if m := _ISO.search(t):
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m := _DMY.search(t):
        return _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if m := _PL.search(t):
        if month := PL_MONTHS.get(m.group(2).lower()):
            return _safe_date(int(m.group(3)), month, int(m.group(1)))
    return None


def _safe_date(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


# ----------------------------------------------------------------------
# sygnatury
# ----------------------------------------------------------------------
SIGNATURE_RE = re.compile(
    r"\b((?:KIO|[IVXLC]{1,6})\s+[A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]{0,6}\s*\d+\s*/\s*\d{2,4})\b")
KIO_SIG_RE = re.compile(r"\bKIO\s+\d+\s*/\s*\d{2,4}\b")


# Skróty sądów administracyjnych zapisywane w sygnaturach z małych liter:
# "I SA/Łd 269/26", "II SA/Wa 118/25". Ślepe .upper() by je zepsuło.
_SIG_COURT_CODE = re.compile(r"(?<=/)([A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż]{2,4})(?=\s)")


def normalize_signature(sig: str | None) -> str | None:
    """'  ii   c  123 / 20 ' -> 'II C 123/20'; 'i sa/łd 269/26' -> 'I SA/Łd 269/26'."""
    if not sig:
        return None
    s = squash(sig).replace("–", "-").replace("—", "-")
    s = re.sub(r"\s*/\s*", "/", s)
    s = re.sub(r"\s+", " ", s).strip(" .,;").upper()
    # przywróć zapis typu "/Łd", "/Wa", "/Kr" - pierwsza wielka, reszta mała
    return _SIG_COURT_CODE.sub(lambda m: m.group(1).capitalize(), s)


# ----------------------------------------------------------------------
# typ orzeczenia
# ----------------------------------------------------------------------
# Kolejność = priorytet. Portal potrafi podać kilka typów naraz
# ("zarządzenie, uzasadnienie") - wtedy wiodący jest ten najwyżej na liście,
# bo "uzasadnienie" jest najmniej konkretnym określeniem.
DOC_TYPE_PATTERNS: list[tuple[str, str]] = [
    ("wyrok", r"wyrok\w*"),
    ("postanowienie", r"postanowieni\w*"),
    ("nakaz zapłaty", r"nakaz\w*(?:\s+zaplaty)?"),
    ("zarządzenie", r"zarzadzeni\w*"),
    ("ugoda", r"ugod\w*"),
    ("opinia", r"opini\w*"),
    ("uzasadnienie", r"uzasadnieni\w*"),
]


def detect_doc_types(*candidates: str | None) -> list[str]:
    """Zwraca WSZYSTKIE rozpoznane typy, uporządkowane wg priorytetu."""
    found: list[str] = []
    for cand in candidates:
        if not cand:
            continue
        low = strip_accents(squash(cand).lower())
        for canon, rx in DOC_TYPE_PATTERNS:
            if canon in found:
                continue
            if re.search(rf"\b{rx}\b", low):
                found.append(canon)
        if found:
            break          # nie mieszaj typów z tytułu z tymi z treści
    return found


def detect_doc_type(*candidates: str | None) -> str | None:
    """Typ wiodący - pierwszy wg priorytetu."""
    types = detect_doc_types(*candidates)
    return types[0] if types else None


# ----------------------------------------------------------------------
# scalanie wyroku z uzasadnieniem opublikowanych jako dwa osobne dokumenty
# (patrz orzeczenia/obserwator.py, orzeczenia/sources/ms_gov.py - wspólne dla
# importu do własnej bazy i dla wyszukiwania na żywo)
# ----------------------------------------------------------------------
RULING_DOC_TYPES = {"wyrok", "postanowienie", "nakaz zapłaty", "zarządzenie", "ugoda"}


def is_uzasadnienie_pair(a_doc_type: str | None, b_doc_type: str | None) -> bool:
    """Portal MS czasem publikuje wyrok/postanowienie i jego uzasadnienie jako
    dwa osobne dokumenty (sprawdzone na żywo na sygnaturze „II K 971/25") -
    ta sama sygnatura/sąd/data orzeczenia/data publikacji, ale jeden ma typ
    "uzasadnienie", drugi właściwy typ rozstrzygnięcia."""
    return (a_doc_type == "uzasadnienie") != (b_doc_type == "uzasadnienie") and (
        a_doc_type in RULING_DOC_TYPES or b_doc_type in RULING_DOC_TYPES)


def combine_wyrok_uzasadnienie(wyrok: dict, uzas: dict) -> dict:
    """Łączy treść dwóch już zidentyfikowanych ról (`wyrok` = rozstrzygnięcie,
    zostaje jako kanoniczny rekord; `uzas` = uzasadnienie, zostaje wchłonięte)
    w jeden dokument. Oczekuje słowników w kształcie zwracanym przez
    `Source.document()` / odczytanych ze `Store` (te same nazwy pól)."""
    merged = dict(wyrok)

    own_uzas = wyrok.get("uzasadnienie") or ""
    uzas_text = uzas.get("uzasadnienie") or uzas.get("full_text") or ""
    if uzas_text and uzas_text not in own_uzas:
        merged["uzasadnienie"] = (own_uzas + "\n\n" + uzas_text).strip() if own_uzas else uzas_text

    own_full = wyrok.get("full_text") or ""
    parts = [own_full] if own_full else []
    if uzas.get("full_text") and uzas["full_text"] not in own_full:
        parts.append(uzas["full_text"])
    merged["full_text"] = "\n\n".join(parts) or None

    merged["thematic"] = list(dict.fromkeys(
        (wyrok.get("thematic") or []) + (uzas.get("thematic") or [])))
    seen_names = {(j.get("name") or "").lower() for j in (wyrok.get("judges") or [])}
    judges = list(wyrok.get("judges") or [])
    for j in uzas.get("judges") or []:
        name = (j.get("name") or "").lower()
        if name and name not in seen_names:
            seen_names.add(name)
            judges.append(j)
    merged["judges"] = judges
    merged["legal_basis"] = wyrok.get("legal_basis") or uzas.get("legal_basis")
    merged["importance"] = wyrok.get("importance") or uzas.get("importance")
    merged["doc_type_raw"] = wyrok.get("doc_type_raw") or uzas.get("doc_type_raw")
    merged.pop("excerpt", None)   # dociągnie się od nowa z pełnej (scalonej) treści
    return merged


# ----------------------------------------------------------------------
# osoby (sędziowie / arbitrzy)
# ----------------------------------------------------------------------
TITLE_NOISE = re.compile(
    r"\b(SSR|SSO|SSA|SSN|S\.?S\.?R\.?|sędzia|sedzia|sędziowie|sedziowie|"
    r"przewodniczący|przewodniczaca|przewodnicząca|przewodniczacy|"
    r"członek|czlonek|członkowie|czlonkowie|protokolant|protokolantka|"
    r"del(?:egowany|egowana)?|sądu|sadu|rejonowego|okręgowego|okregowego|apelacyjnego|"
    r"del\.|dr|hab\.|prof\.|mgr)\b\.?",
    re.IGNORECASE)

_NAME_TOKEN = r"(?:[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:-[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)?)"
# UWAGA: tylko spacja/tab między członami - imię i nazwisko nigdy nie łamie się
# na dwie linie, a lista składu ma jedną osobę w wierszu.
PERSON_RE = re.compile(rf"\b{_NAME_TOKEN}(?:[ \t]+{_NAME_TOKEN}){{1,2}}\b")


def clean_person(raw: str | None) -> str | None:
    if not raw:
        return None
    s = squash(raw)
    s = TITLE_NOISE.sub(" ", s)
    s = re.sub(r"[^\wĄĆĘŁŃÓŚŹŻąćęłńóśźż'\-\s.]", " ", s)
    s = re.sub(r"\.(?=\S)", ". ", s)
    s = squash(s).strip(" .,-")
    if not s or len(s) < 4:
        return None
    parts = [p for p in s.split() if p[:1].isupper()]
    if len(parts) < 2:
        return None
    return " ".join(parts[:4])


ROLE_ORDER = {"przewodniczący": 0, "sędzia": 1, "członek": 2, "protokolant": 9}


def sort_panel(panel: list[dict[str, str]]) -> list[dict[str, str]]:
    """Przewodniczący zawsze pierwszy, protokolant ostatni."""
    return sorted(panel, key=lambda p: (ROLE_ORDER.get(p.get("role", ""), 5),
                                        p.get("name", "")))


def normalize_person(name: str) -> str:
    return strip_accents(squash(name).lower())


SINGLE_ROLES = [
    ("przewodniczący", re.compile(
        r"przewodnicz[ąa]c[yaąe]{1,2}\s*(?:sk[łl]adowi|sk[łl]adu)?\s*[:\-–]?\s*([^\n;]{3,120})",
        re.IGNORECASE)),
    ("protokolant", re.compile(
        r"protokolant(?:ka)?\s*[:\-–]?\s*([^\n;]{3,120})", re.IGNORECASE)),
]

MULTI_ROLES = [
    ("członek", re.compile(r"cz[łl]onk(?:owie|ini|a)?\s*[:\-–]?\s*", re.IGNORECASE)),
    ("sędzia", re.compile(r"s[ęe]dzi(?:owie|a|ego)\s*[:\-–]\s*", re.IGNORECASE)),
]

# słowa, po których lista osób na pewno się kończy
_PANEL_STOP = re.compile(
    r"\b(po rozpoznaniu|protokolant|protokolantka|przy udziale|w sprawie|orzeka|"
    r"postanawia|wyrokuje|na posiedzeniu|na rozprawie|sprawy|z udzia[łl]em|"
    r"po przeprowadzeniu|dnia\b)", re.IGNORECASE)


def extract_panel(text: str | None, window: int = 3000) -> list[dict[str, str]]:
    """Wyciąga skład orzekający z początku treści orzeczenia.

    Obsługuje zapis jednoliniowy ("Przewodniczący: Jan Kowalski") i wieloliniowy
    ("Członkowie:\n Anna Nowak\n Piotr Zieliński").
    """
    if not text:
        return []
    head = text[:window]
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(name: str | None, role: str) -> None:
        if not name:
            return
        key = normalize_person(name)
        if key in seen:
            return
        seen.add(key)
        out.append({"name": name, "role": role})

    for role, rx in SINGLE_ROLES:
        if not (m := rx.search(head)):
            continue
        chunk = _PANEL_STOP.split(m.group(1))[0]
        if pm := PERSON_RE.search(chunk):
            add(clean_person(pm.group(0)), role)

    for role, rx in MULTI_ROLES:
        if not (m := rx.search(head)):
            continue
        chunk = _PANEL_STOP.split(head[m.end():m.end() + 320])[0]
        for pm in PERSON_RE.finditer(chunk):
            if len(out) >= 12:
                break
            add(clean_person(pm.group(0)), role)

    return out[:12]


# ----------------------------------------------------------------------
# podział sentencja / uzasadnienie
# ----------------------------------------------------------------------
# W tekstach z KIO uzasadnienie bywa rozstrzelone: "U z a s a d n i e n i e"
_UZAS_RE = re.compile(
    r"(?:^|\n|\s)((?:U\s*Z\s*A\s*S\s*A\s*D\s*N\s*I\s*E\s*N\s*I\s*E)|"
    r"(?:U\s*z\s*a\s*s\s*a\s*d\s*n\s*i\s*e\s*n\s*i\s*e))\b")


def split_sentencja_uzasadnienie(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    m = _UZAS_RE.search(text)
    if not m:
        return (text.strip() or None), None
    head = text[:m.start()].strip()
    tail = text[m.end():].strip()
    if len(head) < 40:          # dokument jest samym uzasadnieniem
        return None, text.strip()
    return (head or None), (tail or None)


# ----------------------------------------------------------------------
def court_level(court: str | None) -> str | None:
    if not court:
        return None
    c = strip_accents(court.lower())
    if "krajowa izba" in c or c.strip() == "kio":
        return "KIO"
    if "apelacyjny" in c:
        return "SA"
    if "okregowy" in c:
        return "SO"
    if "rejonowy" in c:
        return "SR"
    if "naczelny sad administracyjny" in c or c.strip() == "nsa":
        return "NSA"
    if "wojewodzki sad administracyjny" in c or c.startswith("wsa"):
        return "WSA"
    if "najwyzszy" in c:
        return "SN"
    return None
