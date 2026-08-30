# Jak wgrać to na GitHuba i Vercela

## 1. GitHub

W katalogu z rozpakowanym projektem:

```bash
git init
git add .
git commit -m "Orzecznik: wyszukiwarka orzeczeń na żywo"
git branch -M main
git remote add origin https://github.com/TWOJA-NAZWA/TWOJE-REPO.git
git push -u origin main
```

Jeśli repozytorium już istnieje i ma jakąś zawartość, zamiast `git init`
skopiuj pliki do sklonowanego katalogu i zrób `git add . && git commit && git push`.

## 2. Vercel

1. Vercel → **Add New → Project** → wybierz to repozytorium.
2. Framework Preset: **Other**. Nie ustawiaj Build Command ani Output Directory —
   Vercel sam wykryje FastAPI po `requirements.txt` i punkt wejścia `app.py`.
3. **Deploy**.
4. **Settings → Functions → Region → Frankfurt (fra1)** — portale stoją w Polsce.

Sprawdzenie: `https://twoj-projekt.vercel.app/api/health`

Pełny opis wraz z ograniczeniami: [deploy/VERCEL.md](deploy/VERCEL.md)

## 3. Czy potrzebujesz Railway?

Nie. Vercel uruchomi ten backend bez pośredników. Railway (albo Render, albo
Fly.io) ma sens dopiero wtedy, gdy serwis ma obsługiwać wiele osób naraz —
powody opisane w `deploy/VERCEL.md`, gotowe konfiguracje w `deploy/`.
