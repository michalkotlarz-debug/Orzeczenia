# Orzecznik — wyszukiwarka orzeczeń na żywo

Nakładka na dwa publiczne portale orzecznictwa. **Nie ma własnej bazy danych
i niczego nie archiwizuje.** Każde wyszukanie i każde otwarcie orzeczenia to
zapytanie wysyłane w tej samej chwili do serwisu źródłowego — my tylko
parsujemy odpowiedź i pokazujemy ją w jednym, spójnym interfejsie.

| Źródło | Adres | Co daje |
|---|---|---|
| Portal Orzeczeń Sądów Powszechnych | `orzeczenia.ms.gov.pl` | wyroki, postanowienia i uzasadnienia sądów rejonowych, okręgowych i apelacyjnych |
| Baza orzeczeń KIO | `orzeczenia.uzp.gov.pl` | orzeczenia Krajowej Izby Odwoławczej, sygnatury typu `KIO 1919/16` |

## Co widać przy każdym orzeczeniu

Na liście wyników — dokładnie to, co podaje portal źródłowy: sygnatura, typ,
sąd lub organ, data orzeczenia i publikacji oraz **opis (urywek treści)**.
Po kliknięciu sygnatury otwiera się pełna strona orzeczenia:

* **sygnatura sprawy**, typ (wyrok / postanowienie / uzasadnienie / zarządzenie),
* **data orzeczenia**, publikacji i uprawomocnienia — o ile portal je podaje,
* **skład orzekający** z rolami (przewodniczący, sędzia, członek, protokolant),
* sąd i wydział, hasła tematyczne, podstawa prawna,
* dla KIO dodatkowo: zamawiający, tryb postępowania i sposób rozstrzygnięcia,
* **pełna sentencja i pełne uzasadnienie**, rozdzielone na osobne sekcje.

Do tego link do oryginału w portalu i pobranie całości jako plik tekstowy.

## Uruchomienie

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m orzeczenia.cli serve     # http://127.0.0.1:8000
```

To wszystko — nie ma kroku „zbuduj bazę" ani harmonogramu. Aplikacja jest
gotowa od pierwszego uruchomienia.

W Dockerze: `docker build -t orzecznik . && docker run -p 8000:8000 orzecznik`

### Backend i frontend to jedna aplikacja

`orzeczenia/web/app.py` jest jednocześnie backendem (odpytuje portale, parsuje,
wystawia JSON pod `/api/*`) i serwuje frontend (szablony Jinja2 + `static/`).
Nie ma osobnego serwera do uruchomienia — `serve` startuje całość.

Jeśli chcesz podpiąć własny frontend albo wystawić serwis publicznie, w katalogu
`deploy/` są gotowe konfiguracje dla Rendera, Fly.io i Railway/Heroku oraz
instrukcja: **[deploy/README.md](deploy/README.md)**. Dla frontendu z innej
domeny dopisz ją w `config.yaml` → `web.cors_origins`.

## Strony

| Adres | Co robi |
|---|---|
| `/` | duże pole wyszukiwania i „ostatnio opublikowane" pobierane na żywo |
| `/szukaj` | wyniki z obu portali, z zakładkami i licznikami per źródło |
| `/orzeczenie/{źródło}/{id}` | pełna treść orzeczenia z metryką |
| `/orzeczenie/{źródło}/{id}/pobierz.txt` | to samo jako plik tekstowy |
| `/ulubione` | zapisane sygnatury (tylko w Twojej przeglądarce) |
| `/eksport.csv` | bieżąca strona wyników jako CSV |
| `/api/szukaj`, `/api/orzeczenie/{źródło}/{id}` | to samo w JSON |
| `/api/docs` | interaktywna dokumentacja API |

## Kryteria wyszukiwania

Kryteria trafiają **wprost do wyszukiwarek portali** — nie filtrujemy niczego
po swojemu poza jednym wyjątkiem opisanym niżej.

| Kryterium | Sądy powszechne | KIO |
|---|---|---|
| fraza w treści | tak | tak |
| sygnatura | tak | tak |
| data orzeczenia (od–do) | tak | tak |
| sędzia / arbiter | tak | — |
| hasło tematyczne | tak | — |
| podstawa prawna | tak | — |
| data publikacji | tak (patrz niżej) | — |

Użycie kryterium, którego KIO nie zna, świadomie pomija to źródło — w wynikach
widać wtedy licznik `KIO: 0`, a nie zmyślone trafienia.

**Data publikacji** to jedyny filtr stosowany po naszej stronie: Portal Orzeczeń
potrafi filtrować wyłącznie po dacie orzeczenia, więc pobieramy wyniki
posortowane po dacie publikacji i odsiewamy je z pobranej strony.

### Zakładki źródeł

Domyślnie pytamy oba portale równolegle i przeplatamy wyniki. Zakładki
`Wszystkie / Sądy powszechne / KIO` zawężają zapytanie do jednego serwisu.
Przy sortowaniu po dacie scalona lista jest posortowana dokładnie; przy
sortowaniu po trafności każdy portal liczy ją po swojemu, więc wyniki
przeplatamy po równo.

## Ulubione

Zapisują się w `localStorage` przeglądarki — nie ma kont ani stanu na serwerze,
a lista nie opuszcza Twojego komputera. Trzymamy tylko sygnaturę, sąd, datę
i identyfikator; pełna treść jest pobierana ze źródła dopiero po kliknięciu.

## Grzeczność wobec portali i pamięć podręczna

* jedno zapytanie na host naraz, z odstępem ≥1,2 s (`http.delay_seconds`);
* pamięć podręczna **w RAM**: listy wyników 10 minut, treści orzeczeń 6 godzin.
  Znika przy restarcie — to nie jest baza, tylko ochrona portali przed
  powtarzaniem tego samego zapytania;
* jeśli jeden portal nie odpowie, strona pokazuje wyniki z drugiego i wypisuje,
  co się stało — zamiast wywalać się w całości.

### Limit zapytań w Portalu Orzeczeń

`orzeczenia.ms.gov.pl` **nie zwraca kodu 429**. Zamiast tego oddaje zwykłe
`200 OK` ze stroną CAPTCHA („Wykryliśmy zbyt dużą liczbę zapytań pochodzących
z tego adresu"). Każda odpowiedź jest sprawdzana funkcją `looks_blocked()`;
po wykryciu blokady klient trwale zwalnia (×2, do 20 s), a użytkownik dostaje
czytelny komunikat zamiast pustej listy.

### Dwie pułapki adresów, na które trzeba uważać

**Portal Orzeczeń nie używa kodowania procentowego w ścieżce.** Ma własne,
Tapestry'owe: znak zapisuje jako `$` + 4 cyfry szesnastkowe. Spacja to `$0020`,
ukośnik `$002f`. Sygnatura wysłana jako `II%20C%20438%2F25` kończy się
odpowiedzią **HTTP 400** — sprawdzone na żywo. Robi to `_seg()` w `ms_gov.py`.

**Serwis UZP oddaje listę wyników tylko żądaniom wyglądającym na nawigację.**
Bez nagłówków `Sec-Fetch-Dest: document` i `Sec-Fetch-Mode: navigate` wraca sam
formularz wyszukiwania, bez ani jednej pozycji i bez żadnego błędu. Nagłówki
ustawia `PoliteClient`.

## Struktura projektu

```
orzeczenia/
  config.py           wczytywanie config.yaml
  http.py             klient HTTP: odstępy, cache w RAM, wykrywanie CAPTCHY
  format.py           daty po polsku, odmiana liczebników
  cli.py              jedna komenda: serve
  parse/common.py     daty, sygnatury, skład orzekający, podział sentencja/uzasadnienie
  sources/
    base.py           Query, Hit, SearchPage
    ms_gov.py         Portal Orzeczeń — budowa adresów i parsowanie
    kio_uzp.py        baza KIO — budowa adresów i parsowanie
    registry.py       równoległe odpytanie obu źródeł i scalenie wyników
  web/
    app.py            trasy FastAPI i API JSON
    templates/        Jinja2
    static/           style.css i app.js (ulubione, filtry)
tests/
  fixtures/           prawdziwe fragmenty HTML z obu portali
  test_sources.py     adresy, parsery, scalanie, awarie źródeł, cache
  test_templates.py   renderowanie wszystkich stron
```

## Testy

```bash
python tests/test_sources.py
python tests/test_templates.py
```

Testy nie wykonują żadnych zapytań sieciowych — działają na zapisanych
fragmentach HTML z obu portali. `test_templates.py` zapisuje też podgląd
wszystkich stron do `tests/preview/`. **Uwaga:** te podglądy to statyczne pliki
bez serwera pod spodem — odnośniki w nich nie działają. Żeby klikać, uruchom
`python -m orzeczenia.cli serve`.

## Gdy portal zmieni HTML

Parsery są odizolowane w `sources/ms_gov.py` i `sources/kio_uzp.py`. Zapisz nowy
fragment HTML do `tests/fixtures/`, popraw selektor i uruchom testy.

## Uwagi prawne

Orzeczenia są anonimizowane u źródła (inicjały, `(...)`) — serwis niczego nie
deanonimizuje i nie zmienia treści. Nie przechowuje też orzeczeń: pokazuje to,
co w danej chwili udostępniają portale. Serwis nieoficjalny; źródłem prawa
pozostaje treść oryginalna. Adres kontaktowy w `http.user_agent` jest wysyłany
do portali przy każdym zapytaniu — wpisz tam swój.
