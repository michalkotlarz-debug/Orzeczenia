# Orzecznik — wyszukiwarka orzeczeń na żywo

Nakładka na trzy publiczne portale orzecznictwa. **Archiwum zostaje tam, gdzie
jest.** Każde wyszukanie i każde otwarcie orzeczenia to zapytanie wysyłane
w tej samej chwili do serwisu źródłowego — my tylko parsujemy odpowiedź
i pokazujemy ją w jednym, spójnym interfejsie.

| Źródło | Adres | Co daje |
|---|---|---|
| Portal Orzeczeń Sądów Powszechnych | `orzeczenia.ms.gov.pl` | wyroki, postanowienia i uzasadnienia sądów rejonowych, okręgowych i apelacyjnych |
| Centralna Baza Orzeczeń Sądów Administracyjnych | `orzeczenia.nsa.gov.pl` | orzeczenia NSA i szesnastu WSA, sygnatury typu `I SA/Łd 269/26` |
| Baza orzeczeń KIO | `orzeczenia.uzp.gov.pl` | orzeczenia Krajowej Izby Odwoławczej, sygnatury typu `KIO 1919/16` |

Jedyne, co aplikacja u siebie zapisuje, to **lista nowo zauważonych sygnatur** —
po to, żeby dało się odpowiedzieć na pytanie „co przybyło od wczoraj". Treści
orzeczeń nadal nie kopiujemy.

## ⚠ robots.txt CBOSA — przeczytaj, zanim opublikujesz serwis

`orzeczenia.nsa.gov.pl/robots.txt` zabrania **wszystkim** robotom dwóch
ścieżek, których używa wyszukiwarka:

```
User-agent: *
Disallow: /cbo/find
Disallow: /cbo/search
```

Adresy pojedynczych orzeczeń (`/doc/…`) zakazem **nie** są objęte, więc
otwieranie i pobieranie treści jest poza sporem. Problem dotyczy samego
wyszukiwania.

Co z tym zrobiono w kodzie:

* `sources.nsa.ignore_robots: true` — wyszukiwanie **uruchamiane ręcznie przez
  człowieka** idzie do CBOSA mimo zakazu. Argument: to nie jest indeksowanie
  bazy, tylko przekazanie jednego zapytania jednego użytkownika. Ocena tego
  argumentu należy do Ciebie jako wydawcy serwisu — flagę można wyłączyć
  jedną linijką i wtedy CBOSA zostanie po prostu pominięta.
* `sources.nsa.poll: false` — **obserwator dla CBOSA jest wyłączony**, i to
  nie jest przypadek. Automat chodzący co dobę po `/cbo/search` to już
  bezspornie robot, którego robots.txt zakazuje. Portale MS i KIO takiego
  zakazu nie mają (MS w ogóle nie publikuje `robots.txt` — pod `/robots.txt`
  oddaje stronę HTML), więc tam obserwator działa.

CBOSA dodatkowo **odcina klientów pytających zbyt gęsto** — sprawdzone
w praktyce. Dlatego `http.delay_seconds` jest wspólny dla wszystkich hostów
i lepiej go nie skracać.

Oficjalnego API ani zrzutu danych NSA nie udostępnia (zarządzenie Prezesa NSA
o publikacji orzeczeń w systemach teleinformatycznych nie przewiduje takiej
ścieżki), a `sitemap.xml` pochodzi z 2009 r. i jest bezużyteczny.

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

To wszystko — wyszukiwarka jest gotowa od pierwszego uruchomienia, bez kroku
„zbuduj bazę".

Obserwator (nowe orzeczenia) uruchamia się osobno i jest opcjonalny:

```bash
python -m orzeczenia.cli obserwuj      # jeden przebieg
python -m orzeczenia.cli baza          # co już zebrał
```

### Pełne archiwum na dysk (`archiwizuj-ms`)

To już nie obserwator ani import nowości, tylko jednorazowe/wznawialne
pobranie **całego** dostępnego zbioru sądów powszechnych z `ncourt-api`
(dziś ok. 465 tys. pozycji) - jeden plik JSON na orzeczenie, prosto na dysk,
z pominięciem `Store`/SQLite:

```bash
python -m orzeczenia.cli archiwizuj-ms --out dane/archiwum/ms
```

Przy odstępie `http.delay_seconds` (domyślnie 1,2 s) i dwóch zapytaniach na
dokument do orzeczenia.ms.gov.pl, pełny przebieg to rzędu 1-2 tygodni
ciągłego działania - portal karze szybsze odpytywanie CAPTCHĄ. Bezpiecznie
przerwać (Ctrl+C) i uruchomić ponownie: już zapisane pliki są pomijane, więc
kolejne uruchomienie wznawia od miejsca przerwania. `--limit N` ogranicza
liczbę nowo pobranych dokumentów w jednym przebiegu, żeby robić to porcjami
(np. w cronie co noc). Część starszych pozycji portal oddaje bez treści
(„Błąd danych” na `/content/…` mimo że `/details/…` działa) - to liczone jest
jako błąd tej pozycji i przebieg leci dalej, nie przerywa się.

### Pełne archiwum wprost do bazy, automatycznie (`importuj-ms --full` + GitHub Actions)

To wariant, z którego korzysta wdrożona aplikacja: zamiast zapisywać na dysk
(`archiwizuj-ms` wyżej), `importuj-ms --full` pisze od razu do tej samej bazy
Postgres, z której czyta wyszukiwarka — każda zaimportowana pozycja jest
natychmiast przeszukiwalna na stronie.

```bash
python -m orzeczenia.cli importuj-ms --full --limit 150
```

Stan „co już mamy” trzyma się sam w bazie (`known_ids`) — nie ma żadnego
pliku/kursora do pilnowania, więc kolejne uruchomienia same wznawiają się
tam, gdzie skończyło poprzednie. Dzięki temu ten sam przebieg da się wołać
cyklicznie, niezależnie od tego, czy czyjś komputer jest włączony.

Właśnie to robi `.github/workflows/archiwum.yml`: uruchamia powyższą komendę
co 30 minut na infrastrukturze GitHuba (`workflow_dispatch` pozwala też
odpalić ręcznie z zakładki *Actions*). Jedyny wymagany krok ręczny: dodać
sekret repozytorium `DATABASE_URL` (Settings → Secrets and variables →
Actions → New repository secret) z tym samym connection stringiem co w
Vercelu. Przy ok. 150 nowych pozycjach na przebieg i typowym tempie
(limiter + okazjonalne blokady CAPTCHA) cały zbiór (dziś ok. 465 tys.)
zaimportuje się w tygodnie, bez udziału człowieka i bez zużywania tokenów.

W cronie, np. codziennie o 5:00:

```
0 5 * * *  cd /sciezka/do/orzeczenia && .venv/bin/python -m orzeczenia.cli obserwuj
```

Na Vercelu robi to harmonogram wpisany w `vercel.json`, który woła
`GET /api/obserwator/uruchom`. Endpoint jest chroniony tokenem — ustaw
`ORZECZNIK_POLL_TOKEN` (i ten sam ciąg jako `CRON_SECRET` w Vercelu).

W Dockerze: `docker build -t orzecznik . && docker run -p 8000:8000 orzecznik`

Na Vercelu: repozytorium jest już skonfigurowane (`app.py`, `vercel.json`,
`.python-version`) — wystarczy zaimportować projekt. Szczegóły i ostrzeżenia:
**[deploy/VERCEL.md](deploy/VERCEL.md)**.

### Backend i frontend to jedna aplikacja

`orzeczenia/web/app.py` jest jednocześnie backendem (odpytuje portale, parsuje,
wystawia JSON pod `/api/*`) i serwuje frontend (szablony Jinja2 + `static/`).
Nie ma osobnego serwera do uruchomienia — `serve` startuje całość.

Jeśli chcesz podpiąć własny frontend albo wystawić serwis publicznie, w katalogu
`deploy/` są gotowe konfiguracje dla Rendera, Fly.io i Railway/Heroku oraz
instrukcja: **[deploy/README.md](deploy/README.md)**. Dla frontendu z innej
domeny dopisz ją w `config.yaml` → `web.cors_origins`.

**Czy potrzebny jest Railway?** Do samego wyszukiwania nie — Vercel wystarczy
także dla serwisu publicznego. Do obserwatora potrzebna jest baza, która
przeżyje restart (katalog aplikacji na Vercelu jest tylko do odczytu) oraz
harmonogram; na planie Hobby ten drugi chodzi najwyżej raz na dobę. Pełne
porównanie trzech układów: **[deploy/railway.md](deploy/railway.md)**.

## Obserwator — schemat przenoszenia orzeczeń

Jeden przebieg to, dla każdego serwisu z `poll: true`: pobierz kilka
pierwszych stron najświeższych wyników i zapisz te sygnatury, których jeszcze
nie widzieliśmy. Zapisujemy metrykę i urywek, nigdy pełnej treści — ta jest
pobierana dopiero przy otwarciu orzeczenia.

| Kolumna | Skąd |
|---|---|
| `source`, `doc_id` | klucz naturalny: serwis + identyfikator w tym serwisie |
| `signature`, `doc_type`, `court`, `division` | z listy wyników portalu |
| `judgment_date`, `publication_date` | rozróżniane osobno; CBOSA i KIO nie prowadzą daty publikacji |
| `outcome`, `excerpt`, `panel`, `thematic` | tyle, ile podaje lista wyników |
| `source_url` | adres oryginału |
| `first_seen_at` | **kiedy TA aplikacja zobaczyła orzeczenie po raz pierwszy** |
| `last_seen_at` | ostatni przebieg, w którym pozycja jeszcze była |

Tabela `przebiegi` trzyma historię: kiedy, dla jakiego serwisu, ile obejrzano,
ile było nowych, czy się udało. Widać ją na `/nowe` i w `python -m orzeczenia.cli baza`.

Baza: domyślnie SQLite (`dane/orzecznik.sqlite3`). W chmurze ustaw
`DATABASE_URL` na Postgresa — `postgres://…`, `postgresql://…`
i `postgresql+psycopg://…` są rozpoznawane.

## Strony

| Adres | Co robi |
|---|---|
| `/` | duże pole wyszukiwania i „ostatnio opublikowane" pobierane na żywo |
| `/szukaj` | wyniki ze wszystkich portali, z zakładkami i licznikami per źródło |
| `/nowe` | co obserwator zauważył jako nowe + historia przebiegów |
| `/orzeczenie/{źródło}/{id}` | pełna treść orzeczenia z metryką |
| `/orzeczenie/{źródło}/{id}/pobierz.txt` | to samo jako plik tekstowy |
| `/ulubione` | zapisane sygnatury (tylko w Twojej przeglądarce) |
| `/eksport.csv` | bieżąca strona wyników jako CSV |
| `/api/szukaj`, `/api/orzeczenie/{źródło}/{id}` | to samo w JSON |
| `/api/nowe` | lista nowych orzeczeń w JSON |
| `/api/obserwator/uruchom` | jeden przebieg obserwatora (wymaga tokenu) |
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
  cli.py              komendy: serve, obserwuj, baza
  store.py            baza nowych orzeczeń (SQLite lub Postgres, bez ORM-a)
  obserwator.py       jeden przebieg: co nowego pojawiło się w portalach
  parse/common.py     daty, sygnatury, skład orzekający, podział sentencja/uzasadnienie
  sources/
    base.py           Query, Hit, SearchPage
    ms_gov.py         Portal Orzeczeń — budowa adresów i parsowanie
    nsa_cbosa.py      CBOSA — sesja POST /cbo/search + GET /cbo/find?p=N
    kio_uzp.py        baza KIO — budowa adresów i parsowanie
    registry.py       równoległe odpytanie źródeł i scalenie wyników
  web/
    app.py            trasy FastAPI i API JSON
    templates/        Jinja2
    static/           style.css i app.js (ulubione, filtry)
tests/
  fixtures/           prawdziwe fragmenty HTML z portali
  test_sources.py     adresy, parsery, scalanie, awarie źródeł, cache (MS + KIO)
  test_nsa.py         CBOSA: formularz, lista, dokument, baza obserwatora
  test_templates.py   renderowanie wszystkich stron
```

## Testy

```bash
python tests/test_sources.py
python tests/test_nsa.py
python tests/test_templates.py
```

Testy nie wykonują żadnych zapytań sieciowych — działają na zapisanych
fragmentach HTML z portali i na tymczasowej bazie SQLite. `test_templates.py` zapisuje też podgląd
wszystkich stron do `tests/preview/`. **Uwaga:** te podglądy to statyczne pliki
bez serwera pod spodem — odnośniki w nich nie działają. Żeby klikać, uruchom
`python -m orzeczenia.cli serve`.

## Gdy portal zmieni HTML

Parsery są odizolowane w `sources/ms_gov.py` i `sources/kio_uzp.py`. Zapisz nowy
fragment HTML do `tests/fixtures/`, popraw selektor i uruchom testy.

## Uwagi prawne

Orzeczenia są anonimizowane u źródła (inicjały, `(...)`) — serwis niczego nie
deanonimizuje i nie zmienia treści. Od Fazy 1 (własna baza + indeks) serwis
**przechowuje treść orzeczeń we własnej bazie** (Postgres/Neon), żeby
wyszukiwanie było szybsze i nie obciążało portali źródłowych przy każdym
zapytaniu — to wciąż ta sama, już zanonimizowana u źródła treść, tylko
skopiowana zamiast pytana za każdym razem na żywo. Serwis nieoficjalny;
źródłem prawa pozostaje treść oryginalna. Adres kontaktowy w `http.user_agent`
jest wysyłany do portali przy każdym zapytaniu — wpisz tam swój.
