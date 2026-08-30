# Wdrożenie na Vercelu

Vercel wykrywa FastAPI po `requirements.txt` i szuka zmiennej `app` w `app.py`
w katalogu głównym. Oba pliki są już w repozytorium — konfiguracja jest gotowa,
nie trzeba nic przestawiać.

| Plik | Po co |
|---|---|
| `app.py` | punkt wejścia — importuje `orzeczenia.web.app:app` |
| `vercel.json` | limit czasu funkcji (60 s) i wykluczenie testów z paczki |
| `.python-version` | Python 3.12 |
| `requirements.txt` | zależności |

## Kroki

1. Wrzuć repozytorium na GitHuba.
2. Vercel → **Add New → Project** → wskaż to repozytorium.
3. Framework Preset zostaw na **Other** (Vercel sam wykryje FastAPI). Nie ustawiaj
   żadnego Build Command ani Output Directory.
4. **Deploy.**
5. W **Project Settings → Functions → Region** ustaw **Frankfurt (fra1)**.
   Domyślny region to Waszyngton; portale stoją w Polsce, więc każde zapytanie
   robiłoby niepotrzebną podróż przez Atlantyk — dwa razy, bo my pytamy portal,
   a potem odsyłamy odpowiedź.

Po wdrożeniu sprawdź `https://twoj-projekt.vercel.app/api/health` — powinno
zwrócić `{"ok": true, "sources": ["ms","kio"], "cache": 0}`.

## Co się zmienia w porównaniu z uruchomieniem lokalnym

Na Vercelu aplikacja działa jako funkcja bezstanowa i to ma dwa realne skutki.

**Pamięć podręczna przestaje działać tak dobrze.** Cache siedzi w RAM procesu,
a Vercel tworzy i usuwa instancje w miarę ruchu. Ta sama wyszukiwarka odpytana
dwa razy trafi zwykle na dwie różne instancje i dwa razy pójdzie do portalu.

**Ogranicznik tempa nie obejmuje wszystkich instancji.** `RateLimiter` pilnuje
odstępu w obrębie jednego procesu. Gdy Vercel uruchomi dziesięć instancji na raz,
każda liczy swój własny odstęp — i do orzeczenia.ms.gov.pl idzie dziesięć zapytań
naraz. Portal odpowie wtedy stroną CAPTCHA, a aplikacja pokaże komunikat
o chwilowym ograniczeniu.

Dla Ciebie i kilku osób to nie problem. Jeśli serwis ma być publiczny i używany
przez wiele osób jednocześnie, jeden stale działający serwer (Railway, Render,
Fly.io) jest **technicznie lepszy** — bo ogranicznik i cache są wtedy wspólne
dla całego ruchu. Konfiguracje do tych trzech są w `deploy/`.

## Zanim wystawisz publicznie

Wpisz swój adres kontaktowy w `config.yaml` → `http.user_agent`, albo ustaw
zmienną środowiskową w Vercelu:

```
ORZECZNIK_USER_AGENT = Orzecznik/2.0 (kontakt: twoj@email.pl)
```

To jedyny sposób, w jaki administratorzy portali mogą się z Tobą skontaktować,
zanim zaczną blokować ruch.
