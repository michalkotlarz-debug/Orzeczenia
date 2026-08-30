# Wdrożenie backendu

Backend to zwykła aplikacja ASGI (`orzeczenia.web.app:app`) — działa wszędzie,
gdzie da się uruchomić uvicorna. Nie ma bazy danych ani wolumenów do podpięcia:
cały stan to pamięć podręczna w RAM, która znika przy restarcie.

## Lokalnie

```bash
pip install -r requirements.txt
python -m orzeczenia.cli serve          # http://127.0.0.1:8000
```

## Docker

```bash
docker build -t orzecznik .
docker run -p 8000:8000 orzecznik
```

## Publiczny adres

| Gdzie | Jak |
|---|---|
| **Render** | wrzuć repo na GitHub → New → Blueprint → `deploy/render.yaml` |
| **Fly.io** | `fly launch --copy-config --config deploy/fly.toml` |
| **Railway / Heroku** | skopiuj `deploy/Procfile` do katalogu głównego |
| **Własny serwer** | `uvicorn orzeczenia.web.app:app --host 0.0.0.0 --port 8000` za nginx-em |

Po wdrożeniu sprawdź `GET /api/health` — powinno zwrócić
`{"ok": true, "sources": ["ms", "kio"], "cache": 0}`.

## Zanim wystawisz to publicznie — dwie rzeczy

**Wpisz swój adres kontaktowy** w `config.yaml` → `http.user_agent`. Leci on do
portali przy każdym zapytaniu; to jedyny sposób, w jaki mogą się z Tobą
skontaktować, gdy ruch zacznie im przeszkadzać.

**Pamiętaj o limicie Portalu Orzeczeń.** Każde wyszukanie użytkownika to
zapytanie do orzeczenia.ms.gov.pl. Przy jednym użytkowniku odstęp 1,2 s jest
bezpieczny; przy kilkunastu naraz portal pokaże CAPTCHA i aplikacja zacznie
zwracać komunikat o chwilowym ograniczeniu. Jeśli serwis ma obsłużyć więcej
osób, podnieś `http.delay_seconds` i wydłuż `cache.listing_ttl_seconds`.

## API dla własnego frontendu

Backend wystawia JSON pod `/api/szukaj` i `/api/orzeczenie/{źródło}/{id}`.
Żeby wołać go z innej domeny, dopisz ją w `config.yaml`:

```yaml
web:
  cors_origins:
    - "https://moj-frontend.example.com"
```

```bash
curl 'https://twoj-adres/api/szukaj?q=wadium&page=1'
curl 'https://twoj-adres/api/orzeczenie/kio/35751'
```
