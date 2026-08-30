# Railway — kiedy jest potrzebny, a kiedy nie

Krótka odpowiedź: **do samego wyszukiwania nie. Do obserwatora — zależy, jak
często ma chodzić.**

## Co robi która część aplikacji

| Część | Czego potrzebuje |
|---|---|
| Strona + wyszukiwanie na żywo | tylko funkcja HTTP — Vercel wystarczy |
| Otwieranie orzeczenia | tylko funkcja HTTP — Vercel wystarczy |
| Obserwator (nowe orzeczenia) | harmonogram **+ baza, która przeżyje restart** |

Wyszukiwanie nie ma stanu: zapytanie leci do portalu i wraca. Dlatego to,
że serwis stał się publiczny, samo w sobie **nie** wymusza Railway — Vercel
sam dokłada instancje przy ruchu.

Problem jest gdzie indziej: katalog aplikacji na Vercelu jest **tylko do
odczytu**, więc SQLite nie ma gdzie zamieszkać. A harmonogram na planie
Hobby chodzi **najwyżej raz na dobę** i z dokładnością do godziny.

## Trzy sensowne układy

### 1. Vercel + Neon (darmowo, przebieg raz dziennie)
* frontend, API i obserwator jako funkcje na Vercelu,
* baza: darmowy Postgres w Neonie (albo Supabase), adres w `DATABASE_URL`,
* harmonogram: `crons` w `vercel.json` (już wpisany, 5:00 UTC),
* sekret: `ORZECZNIK_POLL_TOKEN`, ten sam co `CRON_SECRET` w Vercelu.

Ograniczenia planu Hobby: jeden przebieg na dobę, funkcja żyje maks. 60 s
(u nas ustawione w `vercel.json`; twardy limit planu to 300 s).
Przy trzech serwisach i kilku stronach wyników mieści się to spokojnie.

### 2. Vercel Pro (20 USD/mies.) — jeśli chcesz sprawdzać co godzinę
To samo co wyżej, ale harmonogram może chodzić co minutę. Sensowne, jeśli
zależy Ci na tym, żeby nowe orzeczenie pojawiało się u Ciebie tego samego
dnia, a nie następnego rano.

### 3. Railway (~5 USD/mies.) — jeden dom dla wszystkiego
Kontener chodzi bez przerwy, więc:
* nie ma limitu czasu funkcji (dłuższe przebiegi, więcej stron naraz),
* Postgres i harmonogram są w tym samym miejscu co aplikacja,
* SQLite też zadziała, jeśli podepniesz wolumen.

Uruchomienie: `Dockerfile` z tego repozytorium, a obok usługa cron
z komendą `python -m orzeczenia obserwuj`.

## Co polecam na start

Zacznij od **wariantu 1**. Nic nie kosztuje, a jedyne, czego nie masz, to
częstsze niż dobowe sprawdzanie nowości — czego przy orzeczeniach
publikowanych partiami raczej nie odczujesz. Jeśli po miesiącu okaże się,
że przebieg nie mieści się w 60 s albo chcesz odświeżać co godzinę,
przeniesienie obserwatora na Railway to zmiana jednej komendy — kod jest
ten sam.
