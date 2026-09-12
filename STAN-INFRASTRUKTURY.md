# Stan infrastruktury — jedno źródło prawdy

Ten plik odpowiada na pytanie „co dziś realnie działa, gdzie i jak" — bez
tego trzeba było rekonstruować stan z trzech osobnych rozmów. Aktualizuj go
przy każdej zmianie infrastruktury (nie kodu — kod dokumentuje się sam
w commitach; ten plik dokumentuje **wdrożenie**).

Ostatnia aktualizacja: 2026-09-12.

## Produkcja

- **Domena:** `portalorzeczen.pl` i `www.portalorzeczen.pl` → VPS (nie Vercel).
- **VPS:** home.pl, IP `87.106.31.76`, Ubuntu, użytkownik `deploy` (SSH: klucz
  `~/.ssh/orzeczenia_vps`, hasło root zostało zmienione po tym jak trafiło na
  czat — nie jest już aktualne).
- **Aplikacja:** kontener Docker `orzecznik` (`--network host`,
  `--restart unless-stopped`), za Nginx (reverse proxy + SSL Let's Encrypt,
  auto-odnawianie). Użytkownik w kontenerze: non-root (`orzecznik`, UID 1000).
- **Baza danych:** PostgreSQL 18.6 **lokalnie na VPS** (`127.0.0.1`, poza
  kontenerem, tylko lokalny dostęp — **nie jest osiągalna z zewnątrz, w tym
  z GitHub Actions**). Baza `orzecznik`. Rozszerzenie `unaccent` zainstalowane.
- **Wdrażanie kodu:** `deploy/vps-deploy.sh` — pakuje pliki wprost z lokalnego
  dysku (nie z gita!) i wysyła przez `scp`/`ssh`, buduje obraz, restartuje
  kontener. **Commit do gita i wdrożenie to dwie osobne czynności** — commit
  sam z siebie niczego nie wdraża.

## Co zniknęło (świadomie, nie przez pomyłkę)

- **Projekt Vercel „orzeczenia" — skasowany** (2026-09-03). `orzeczenia.vercel.app`
  zwraca teraz 404 i tak ma zostać.
- **Baza Neon Postgres — skasowana** razem z integracją Vercel↔Neon. Dane
  zostały wcześniej zmigrowane 1:1 na VPS (`pg_dump`/`pg_restore`, zweryfikowane).
- Powód całej migracji: chęć posiadania własnego, kontrolowanego serwera
  zamiast dwóch niezależnie rosnących baz (Vercel/Neon + docelowy VPS).

## Harmonogramy — prawdziwy crontab na VPS (`crontab -l` jako `deploy`)

```
*/15 * * * *  run_obserwator.sh       # orzeczenia MS/KIO: nowosci + fallback archiwum
17 6  * * *  run_akty_obserwuj.sh    # akty prawne: przyrost dzienny (nowe/zmienione)
13 *  * * *  check_disk.sh           # co godzine: alert mailowy gdy <5GB wolnego miejsca
*/20 * * * *  check_import.sh         # alert mailowy, gdy pobieranie danych stoi
@reboot      run_akty_wstecz_loop.sh # akty prawne: ciagla petla cofania (batch=10)
```

Cofanie aktow w archiwum nie chodzi juz z crona co 30 minut, tylko jako ciagla
petla startowana przy `@reboot` (`while true; curl .../api/akty/wstecz?batch=10;
sleep 3`). Paczka zeszla z 300 na 10 po awarii opisanej nizej.

Wszystkie wołają lokalny endpoint aplikacji (`curl http://127.0.0.1:8000/api/...`)
z tokenem z `.env` (`ORZECZNIK_POLL_TOKEN`), logują do
`/home/deploy/orzeczenia/*.log`. **Nie zależą od żadnej sesji Claude ani
otwartego komputera** — to prawdziwy `cron`, przetrwa restart serwera.

Oba endpointy obserwatorów (`/api/obserwator/uruchom`, `/api/akty/wstecz`,
`/api/akty/obserwuj`) mają nieblokującą blokadę (`threading.Lock`) chroniącą
przed nakładającymi się przebiegami — nakładające się wywołanie dostaje
`HTTP 409` zamiast czekać w kolejce.

## Pamięć — po awarii importu z 11–12.09.2026

Import parsuje PDF-y **w tym samym procesie co serwer WWW**, więc jego pamięć
jest pamięcią całej aplikacji. 11.09 wieczorem import trafił na M.P. 2021 poz. 414
(13 MB, 311 stron, 775 czcionek): parser zjadał ponad 2,8 GB przy 3,8 GB RAM-u
serwera, kernel ubijał procesy w całym systemie, kontener wstawał i brał ten sam
akt od nowa. Przez dobę nie wszedł do bazy ani jeden nowy dokument — ani akt,
ani orzeczenie — i nic o tym nie powiadamiało.

Co z tego zostało na stałe:

- **Kontener ma limit pamięci** `-m 2000m` (`deploy/vps-deploy.sh`). Przepełnienie
  ubija wtedy tylko jego, a `--restart unless-stopped` go podnosi; wcześniej
  globalny OOM zabierał ze sobą bazę i zadania wsadowe. Limit dobrany tak, by
  przebieg importu się mieścił — przy 1500m ginął w połowie i do bazy nie
  trafiało nic.
- **PDF czytany strona po stronie** (`orzeczenia/sources/sejm_eli.py`), zamiast
  całego dokumentu naraz: szczyt 127 MB zamiast 2860 MB na tym samym pliku.
- **Progi rozmiaru** jako druga linia obrony: powyżej 8 MB bez wykrywania tabel,
  powyżej 20 MB akt zapisywany bez treści zamiast blokowania kolejki.
- **`check_import.sh` co 20 minut** (kopia w `deploy/`) — alert mailem na
  michal.kotlarz@gmail.com, gdy kontener restartuje się w kółko, import nie
  zwraca odpowiedzi, nic nie przybywa (akty >24 h, orzeczenia >72 h, bo sądy nie
  publikują w weekendy) albo serwis nie odpowiada. Jeden alert na problem, drugi
  mail po powrocie do normy.

Alert dyskowy wysyła mail na **michal.kotlarz@gmail.com** przez Gmail SMTP
(`msmtp`, hasło aplikacji w `~/.msmtprc`, uprawnienia 600) — tylko raz na
przekroczenie progu, resetuje się gdy miejsce wraca powyżej 5GB.

## GitHub Actions — stan po migracji na VPS

- **`archiwum.yml` — WYŁĄCZONY** (`gh workflow disable`, 2026-09-04). Wołał
  starą bazę Neon (`DATABASE_URL` w sekretach), która już nie istnieje —
  gdyby ktoś go z powrotem włączył, będzie tylko generował błędy co 30 min.
  Ten sam efekt (dogrywanie archiwum) robi teraz `run_akty_wstecz`-owy
  odpowiednik dla orzeczeń: fallback w `orzeczenia/obserwator.py:run_once()`.
- **`admin-cli.yml`, `scal-duplikaty.yml` — ręczne (`workflow_dispatch`),
  nieaktualne.** Też celują w sekret `DATABASE_URL` (Neon), który już nie
  istnieje, a nawet gdyby sekret zaktualizować na VPS-owego Postgresa, baza
  jest dostępna tylko na `127.0.0.1` — GitHub Actions i tak by się nie
  dodzwonił. Jeśli potrzebna jednorazowa operacja administracyjna na
  produkcyjnej bazie: **SSH na VPS i `sudo docker exec orzecznik python -m
  orzeczenia.cli ...`** (tak jak w tej sesji przy naprawie filtrów i imporcie
  testowej paczki), nie przez GitHub Actions.
- **`testy.yml`** — jedyny wciąż w pełni aktualny, bez zależności od bazy
  produkcyjnej (uruchamia lokalny zestaw testów na fixture'ach/SQLite).

## Dwa niezależne moduły danych (osobne tabele, osobne pipeline'y)

| | Orzeczenia (MS/KIO) | Akty prawne (Sejm ELI API) |
|---|---|---|
| Tabela | `orzeczenia` | `akty_prawne` |
| Import nowości | `run_obserwator.sh` (15 min) | `run_akty_obserwuj.sh` (dziennie 6:17) |
| Import archiwum | wbudowany fallback w `run_once()` | `run_akty_wstecz_loop.sh` (ciągła pętla, batch 10/dziennik) |
| Zakładka web | `/szukaj`, `/nowe` | `/akty`, `/akt/{publisher}/{rok}/{poz}` |
| Zakres | Sądy powszechne + KIO | Dziennik Ustaw + Monitor Polski |
| Treść | pełny tekst z portalu | tekst wyciągnięty z PDF/HTML (**oryginalne PDF-y NIE są przechowywane**) |

## Znane, jeszcze nie rozwiązane sprawy

- **Import KIO konsekwentnie pokazuje `seen: 0, added: 0`** w każdym przebiegu
  obserwatora — zgłoszone, jeszcze nie zdiagnozowane.
- **`README.md` opisuje architekturę sprzed tej migracji** (live-proxy do
  portali na żywo, wdrożenie na Vercel/Railway/Render, brak wzmianki o VPS
  i o module aktów prawnych) — do generalnego odświeżenia, ten plik
  (`STAN-INFRASTRUKTURY.md`) jest na razie jedynym aktualnym opisem
  wdrożenia.
