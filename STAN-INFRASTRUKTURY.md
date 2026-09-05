# Stan infrastruktury — jedno źródło prawdy

Ten plik odpowiada na pytanie „co dziś realnie działa, gdzie i jak" — bez
tego trzeba było rekonstruować stan z trzech osobnych rozmów. Aktualizuj go
przy każdej zmianie infrastruktury (nie kodu — kod dokumentuje się sam
w commitach; ten plik dokumentuje **wdrożenie**).

Ostatnia aktualizacja: 2026-09-05.

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
*/30 * * * *  run_obserwator.sh       # orzeczenia MS/KIO: nowosci + fallback archiwum
*/30 * * * *  run_akty_wstecz.sh      # akty prawne: paczka cofania w archiwum (batch=300/dziennik)
17 6  * * *  run_akty_obserwuj.sh    # akty prawne: przyrost dzienny (nowe/zmienione)
13 *  * * *  check_disk.sh           # co godzine: alert mailowy gdy <5GB wolnego miejsca
```

Wszystkie wołają lokalny endpoint aplikacji (`curl http://127.0.0.1:8000/api/...`)
z tokenem z `.env` (`ORZECZNIK_POLL_TOKEN`), logują do
`/home/deploy/orzeczenia/*.log`. **Nie zależą od żadnej sesji Claude ani
otwartego komputera** — to prawdziwy `cron`, przetrwa restart serwera.

Oba endpointy obserwatorów (`/api/obserwator/uruchom`, `/api/akty/wstecz`,
`/api/akty/obserwuj`) mają nieblokującą blokadę (`threading.Lock`) chroniącą
przed nakładającymi się przebiegami — nakładające się wywołanie dostaje
`HTTP 409` zamiast czekać w kolejce.

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
| Import nowości | `run_obserwator.sh` (30 min) | `run_akty_obserwuj.sh` (dziennie 6:17) |
| Import archiwum | wbudowany fallback w `run_once()` | `run_akty_wstecz.sh` (30 min, batch 300/dziennik) |
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
