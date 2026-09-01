"""Orzecznik - nakładka na publiczne portale orzecznictwa.

Wyszukiwanie i otwieranie dokumentów czyta najpierw z własnej bazy (zbieranej
przez obserwatora - patrz `orzeczenia/obserwator.py`), bo to szybsze i nie
obciąża portali źródłowych. Dopiero gdy czegoś tam jeszcze nie ma, dociągamy
na żywo (`registry.search()`/`registry.document()`) - użytkownik nigdy nie
dostaje pustej strony tylko dlatego, że baza jeszcze nie zdążyła czegoś zebrać.
"""
from __future__ import annotations

import csv
import io
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Query as Q, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import load_config
from ..format import date_pl, plural_pl
from ..http import RateLimited, SourceUnavailable
from ..sources import Query, Registry
from ..sources.base import Hit, SearchPage
from ..store import Store

log = logging.getLogger("orzecznik.web")

BASE_DIR = Path(__file__).parent
cfg = load_config()
registry = Registry(cfg)

_store: Any = None
_store_error: str = ""


def get_store():
    """Baza obserwatora tworzona przy pierwszym użyciu.

    Na Vercelu katalog aplikacji jest tylko do odczytu, więc bez DATABASE_URL
    ta funkcja zawiedzie - i dobrze: reszta serwisu (wyszukiwanie na żywo)
    ma działać także wtedy."""
    global _store, _store_error
    if _store is not None or _store_error:
        return _store
    if not cfg.store.enabled:
        _store_error = "baza obserwatora wyłączona w konfiguracji (store.enabled)"
        return None
    try:
        from ..store import Store
        _store = Store(cfg.store.url, cfg.store.keep_days)
    except Exception as exc:                        # brak zapisu / zły DATABASE_URL
        _store_error = f"nie udało się otworzyć bazy obserwatora: {exc}"
        log.warning(_store_error)
    return _store


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    registry.close()
    if _store is not None:
        _store.close()


app = FastAPI(title=cfg.web.site_name, docs_url="/api/docs",
              openapi_url="/api/openapi.json", lifespan=lifespan)
# Backend jest jednocześnie API: pozwalamy pytać go z innego frontendu
# (lista dozwolonych źródeł w config.yaml -> web.cors_origins).
if cfg.web.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.web.cors_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals.update(
    site_name=cfg.web.site_name,
    source_label=registry.labels,
    date_field_labels={"judgment": "data orzeczenia", "publication": "data publikacji"},
    sort_labels={"relevance": "trafność", "date_desc": "data orzeczenia ↓",
                 "date_asc": "data orzeczenia ↑", "pub_desc": "data publikacji ↓"},
    plural_pl=plural_pl,
)
templates.env.filters["datepl"] = date_pl


def _qs(params: dict[str, Any], **override: Any) -> str:
    from urllib.parse import urlencode
    merged = {k: v for k, v in params.items() if v not in (None, "")}
    for k, v in override.items():
        if v in (None, ""):
            # Pusta wartość override'u ma USUNĄĆ ten filtr (np. zakładka
            # "Wszystkie" czyści "source") - poprzednio taką wartość po
            # cichu pomijano, więc istniejący filtr nigdy nie znikał.
            merged.pop(k, None)
        else:
            merged[k] = v
    return urlencode(merged)


templates.env.filters["urlencode_page"] = lambda p, page: _qs(p, page=page)
templates.env.filters["urlencode_extra"] = lambda p, k, v: _qs(p, **{k: v, "page": 1})


def _query(**kw: str) -> Query:
    return Query(**{k: (v or "").strip() for k, v in kw.items()})


def _is_simple_phrase(q: Query) -> bool:
    """Baza umie dziś tylko proste wyszukiwanie pełnotekstowe po frazie -
    sygnatura/sędzia/podstawa prawna/zakres dat nadal wymagają pytania portalu
    na żywo (patrz Registry.search)."""
    return bool(q.phrase) and not any(
        (q.signature, q.judge, q.legal_basis, q.thematic, q.date_from, q.date_to))


PAGE_SIZES = (20, 50, 100)
DEFAULT_PAGE_SIZE = PAGE_SIZES[0]


def _clean_per_page(value: int) -> int:
    return value if value in PAGE_SIZES else DEFAULT_PAGE_SIZE


def _search(query: Query, page: int, source: str,
           per_page: int = DEFAULT_PAGE_SIZE) -> tuple[SearchPage, bool]:
    """(wynik, czy_z_wlasnej_bazy). Dla prostych fraz próbujemy najpierw bazy -
    szybciej i bez obciążania portalu; gdy nic tam nie ma (jeszcze niezaimportowane
    albo baza niedostępna), wracamy do dzisiejszego zachowania: pytamy na żywo."""
    per_page = _clean_per_page(per_page)
    store = get_store()

    if _is_simple_phrase(query) and query.sort == "relevance":
        if store is not None:
            rows, total = store.search_fulltext(
                query.phrase, source=source, limit=per_page, offset=(page - 1) * per_page)
            if rows:
                res = SearchPage(hits=[Store.to_hit(r) for r in rows],
                                 page=page, per_page=per_page)
                res.totals = ({source: total} if source
                              else store.count_fulltext_by_source(query.phrase))
                return res, True
        return registry.search(query, page=page, only=source, per_page=per_page), False

    # Zapytania z filtrami (sygnatura/sędzia/podstawa prawna/hasło/zakres dat) -
    # dotychczas szły WYŁĄCZNIE na żywo, więc awaria/blokada portalu oznaczała
    # zero wyników, mimo że własna baza mogła już mieć pasujące pozycje.
    # Czytamy więc najpierw z bazy i ZAWSZE dokładamy to, co ona ma, nawet gdy
    # portal później zawiedzie - baza nie znika z wyniku z powodu błędu na żywo.
    db_hits: list[Hit] = []
    db_totals: dict[str, int] = {}
    if store is not None and not query.is_empty():
        rows, total = store.search_advanced(
            phrase=query.phrase, source=source, signature=query.signature,
            judge=query.judge, legal_basis=query.legal_basis, thematic=query.thematic,
            date_field=query.date_field, date_from=query.date_from, date_to=query.date_to,
            sort=query.sort, limit=per_page, offset=(page - 1) * per_page)
        db_hits = [Store.to_hit(r) for r in rows]
        if db_hits:
            db_totals = ({source: total} if source else
                        store.count_advanced_by_source(
                            phrase=query.phrase, signature=query.signature, judge=query.judge,
                            legal_basis=query.legal_basis, thematic=query.thematic,
                            date_field=query.date_field, date_from=query.date_from,
                            date_to=query.date_to))

    if len(db_hits) >= per_page:
        # Strona w całości pokryta własną bazą - nie ma potrzeby pytać na żywo.
        return SearchPage(hits=db_hits[:per_page], totals=db_totals,
                          page=page, per_page=per_page), True

    res = registry.search(query, page=page, only=source, per_page=per_page)
    known = {(h.source, h.doc_id) for h in res.hits}
    live_count = len(res.hits)
    for h in db_hits:
        if (h.source, h.doc_id) not in known:
            res.hits.append(h)
            known.add((h.source, h.doc_id))
    res.hits = res.hits[:per_page]
    for k, v in db_totals.items():
        res.totals[k] = max(res.totals.get(k, 0), v)
    # "z naszego indeksu" tylko gdy na żywo faktycznie nic nowego nie doszło -
    # w pozostałych przypadkach etykieta "na żywo" jest bliższa prawdzie.
    return res, bool(db_hits) and live_count == 0


# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    store = get_store()
    since = (date.today() - timedelta(days=NOWE_LOOKBACK_DAYS)).isoformat()
    latest_rows = (store.latest(limit=8, since=since, date_field="publication")
                  if store else [])
    baza = store.count() if store else {}
    return templates.TemplateResponse(request, "home.html", {
        "latest_rows": latest_rows, "baza": baza, "baza_razem": sum(baza.values())})


@app.get("/szukaj", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = "", signature: str = "", judge: str = "", thematic: str = "",
    legal_basis: str = "", date_field: str = "judgment",
    date_from: str = "", date_to: str = "",
    sort: str = "relevance", source: str = "", page: int = Q(1, ge=1),
    per_page: int = Q(DEFAULT_PAGE_SIZE),
):
    query = _query(phrase=q, signature=signature, judge=judge, thematic=thematic,
                   legal_basis=legal_basis, date_field=date_field,
                   date_from=date_from, date_to=date_to, sort=sort)
    per_page = _clean_per_page(per_page)
    res, from_db = _search(query, page=page, source=source, per_page=per_page)
    params = {k: v for k, v in request.query_params.items() if k != "page" and v}
    return templates.TemplateResponse(request, "results.html", {
        "q": q, "res": res, "query": query, "page": page, "params": params,
        "source": source, "from_db": from_db, "per_page": per_page,
        "page_sizes": PAGE_SIZES})


@app.get("/orzeczenie/{source}/{doc_id}", response_class=HTMLResponse)
def document_page(request: Request, source: str, doc_id: str, q: str = ""):
    store = get_store()
    doc = store.get_document(source, doc_id) if store else None
    if doc is None:
        try:
            doc = registry.document(source, doc_id)
        except (RateLimited, SourceUnavailable, ValueError) as exc:
            return templates.TemplateResponse(request, "blad.html", {
                "tytul": "Nie udało się pobrać orzeczenia", "opis": str(exc)}, status_code=502)
    else:
        doc["source_label"] = registry.labels.get(source, source)
    return templates.TemplateResponse(request, "document.html", {"d": doc, "q": q})


@app.get("/orzeczenie/{source}/{doc_id}/pobierz.txt")
def download_document(source: str, doc_id: str):
    try:
        d = registry.document(source, doc_id)
    except (RateLimited, SourceUnavailable, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    lines = [
        f"Sygnatura:        {d.get('signature') or '—'}",
        f"Typ:              {d.get('doc_type') or '—'}",
        f"Data orzeczenia:  {date_pl(d.get('judgment_date'))}",
    ]
    if d.get("publication_date"):
        lines.append(f"Data publikacji:  {date_pl(d['publication_date'])}")
    lines += [
        f"Sąd / organ:      {d.get('court') or '—'}"
        + (f" — {d['division']}" if d.get("division") else ""),
        "Skład orzekający: " + (", ".join(f"{j['name']} ({j['role']})"
                                          for j in d.get("judges") or []) or "—"),
        "Hasła tematyczne: " + (", ".join(d.get("thematic") or []) or "—"),
        f"Źródło:           {d.get('source_url')}",
        "", "=" * 72, "",
    ]
    if d.get("sentencja"):
        lines += ["SENTENCJA", "", d["sentencja"], "", "=" * 72, ""]
    if d.get("uzasadnienie"):
        lines += ["UZASADNIENIE", "", d["uzasadnienie"]]
    if not d.get("sentencja") and not d.get("uzasadnienie"):
        lines.append(d.get("full_text") or "")

    name = (d.get("signature") or f"{source}-{doc_id}").replace("/", "-").replace(" ", "_")
    return StreamingResponse(
        iter(["\n".join(lines).encode("utf-8")]),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.txt"'})


NOWE_LOOKBACK_DAYS = 14


@app.get("/nowe", response_class=HTMLResponse)
def new_page(request: Request, source: str = "", limit: int = Q(50, ge=1, le=200)):
    """Orzeczenia opublikowane w ostatnich dwóch tygodniach (data publikacji -
    okno przesuwa się razem z dzisiejszą datą), zebrane przez obserwatora."""
    store = get_store()
    since = (date.today() - timedelta(days=NOWE_LOOKBACK_DAYS)).isoformat()
    rows = (store.latest(limit=limit, source=source, since=since, date_field="publication")
           if store else [])
    return templates.TemplateResponse(request, "nowe.html", {
        "rows": rows, "source": source,
        "blad": _store_error if store is None else ""})


@app.get("/ulubione", response_class=HTMLResponse)
def favourites_page(request: Request):
    """Lista jest budowana w przeglądarce z localStorage - serwer nic o niej nie wie."""
    return templates.TemplateResponse(request, "ulubione.html", {})


@app.get("/eksport.csv")
def export_csv(q: str = "", signature: str = "", judge: str = "", thematic: str = "",
               legal_basis: str = "", date_field: str = "judgment",
               date_from: str = "", date_to: str = "", sort: str = "relevance",
               source: str = "", page: int = Q(1, ge=1)):
    query = _query(phrase=q, signature=signature, judge=judge, thematic=thematic,
                   legal_basis=legal_basis, date_field=date_field,
                   date_from=date_from, date_to=date_to, sort=sort)
    res = registry.search(query, page=page, only=source)

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["sygnatura", "typ", "data_orzeczenia", "data_publikacji",
                "sad_organ", "zrodlo", "adres_oryginalu"])
    for h in res.hits:
        w.writerow([h.signature, h.doc_type, h.judgment_date, h.publication_date,
                    h.court, registry.labels.get(h.source, h.source), h.source_url])
    return StreamingResponse(
        iter([("﻿" + buf.getvalue()).encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="orzeczenia.csv"'})


# ----------------------------------------------------------------------
@app.get("/api/szukaj")
def api_search(q: str = "", signature: str = "", judge: str = "", thematic: str = "",
               legal_basis: str = "", date_field: str = "judgment",
               date_from: str = "", date_to: str = "", sort: str = "relevance",
               source: str = "", page: int = Q(1, ge=1),
               per_page: int = Q(DEFAULT_PAGE_SIZE)):
    query = _query(phrase=q, signature=signature, judge=judge, thematic=thematic,
                   legal_basis=legal_basis, date_field=date_field,
                   date_from=date_from, date_to=date_to, sort=sort)
    per_page = _clean_per_page(per_page)
    res, from_db = _search(query, page=page, source=source, per_page=per_page)
    return {"page": page, "per_page": per_page, "totals": res.totals, "total": res.total,
            "errors": res.errors, "notes": res.notes, "z_bazy": from_db,
            "results": [{**h.__dict__, "url": h.url} for h in res.hits]}


@app.get("/api/orzeczenie/{source}/{doc_id}")
def api_document(source: str, doc_id: str):
    try:
        return registry.document(source, doc_id)
    except (RateLimited, SourceUnavailable, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/nowe")
def api_new(source: str = "", since: str = "", limit: int = Q(50, ge=1, le=200)):
    """Orzeczenia, które obserwator zobaczył po raz pierwszy - najnowsze na górze."""
    store = get_store()
    if store is None:
        return JSONResponse({"error": _store_error}, status_code=503)
    return {"count": store.count(), "results": store.latest(limit=limit, source=source,
                                                            since=since)}


@app.get("/api/obserwator/uruchom")
def api_poll(x_poll_token: str = Header("", alias="X-Poll-Token"),
             authorization: str = Header("", alias="Authorization")):
    """Jeden przebieg obserwatora, wołany z zewnątrz (Vercel Cron, cron systemowy).

    Zabezpieczone tokenem - inaczej każdy mógłby kazać nam odpytywać portale.
    Vercel Cron wysyła nagłówek `Authorization: Bearer <CRON_SECRET>`."""
    token = cfg.poll.token
    if not token:
        return JSONResponse(
            {"error": "ustaw ORZECZNIK_POLL_TOKEN (albo poll.token), zanim włączysz cron"},
            status_code=503)
    given = x_poll_token or authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(given, token):
        return JSONResponse({"error": "zły token"}, status_code=401)
    if not cfg.poll.enabled:
        return JSONResponse({"error": "obserwator wyłączony (poll.enabled)"}, status_code=503)

    store = get_store()
    if store is None:
        return JSONResponse({"error": _store_error}, status_code=503)

    from ..obserwator import run_once
    results = run_once(cfg, registry=registry, store=store)
    return {"ok": all(r.status == "ok" for r in results),
            "przebiegi": [r.as_dict() for r in results],
            "nowych": sum(r.added for r in results)}


@app.get("/api/health")
def health():
    store = get_store()
    return {"ok": True, "sources": list(registry.sources),
            "cache": len(registry.http.cache),
            "baza": store.count() if store else None,
            "baza_blad": _store_error or None}
