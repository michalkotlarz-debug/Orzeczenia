"""Orzecznik - wyszukiwarka orzeczeń zbieranych do własnej bazy.

Warstwa web czyta WYŁĄCZNIE z własnej bazy (zbieranej w tle przez obserwatora -
patrz `orzeczenia/obserwator.py`, jedyne miejsce, które i tak łączy się z
portalami źródłowymi). Jeśli czegoś jeszcze nie zaimportowano, użytkownik po
prostu tego nie zobaczy - strona nigdy nie odpytuje portali na żywo w reakcji
na odwiedziny."""
from __future__ import annotations

import csv
import io
import logging
import secrets
import threading
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
from ..sources import Query, Registry
from ..sources.base import SearchPage
from ..store import Store

log = logging.getLogger("orzecznik.web")

BASE_DIR = Path(__file__).parent
cfg = load_config()
registry = Registry(cfg)

_store: Any = None
_store_error: str = ""
# Chroni przed nakladajacymi sie przebiegami obserwatora - jeden przebieg
# (zwlaszcza z dogrywaniem archiwum, patrz obserwator.py:run_once) potrafi
# trwac dluzej niz odstep crona (co 30 min). Bez tego kolejne wywolania
# `/api/obserwator/uruchom` kolejkuja sie w watkach FastAPI i mnoza
# rownolegle zapytania do tego samego portalu zamiast czekac na swoja kolej.
_poll_lock = threading.Lock()


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
           per_page: int = DEFAULT_PAGE_SIZE) -> SearchPage:
    """Wyniki wyłącznie z własnej bazy - patrz nagłówek modułu. Bez żadnego
    kryterium (`query.is_empty()`) nie zwraca nic, tak jak portal źródłowy
    dawniej przy pustym zapytaniu."""
    per_page = _clean_per_page(per_page)
    store = get_store()
    if store is None or query.is_empty():
        return SearchPage(page=page, per_page=per_page)

    if _is_simple_phrase(query) and query.sort == "relevance":
        rows, total = store.search_fulltext(
            query.phrase, source=source, limit=per_page, offset=(page - 1) * per_page)
        res = SearchPage(hits=[Store.to_hit(r) for r in rows], page=page, per_page=per_page)
        res.totals = ({source: total} if source
                      else store.count_fulltext_by_source(query.phrase))
        return res

    rows, total = store.search_advanced(
        phrase=query.phrase, source=source, signature=query.signature,
        judge=query.judge, legal_basis=query.legal_basis, thematic=query.thematic,
        date_field=query.date_field, date_from=query.date_from, date_to=query.date_to,
        sort=query.sort, limit=per_page, offset=(page - 1) * per_page)
    res = SearchPage(hits=[Store.to_hit(r) for r in rows], page=page, per_page=per_page)
    res.totals = ({source: total} if source else
                  store.count_advanced_by_source(
                      phrase=query.phrase, signature=query.signature, judge=query.judge,
                      legal_basis=query.legal_basis, thematic=query.thematic,
                      date_field=query.date_field, date_from=query.date_from,
                      date_to=query.date_to))
    return res


# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request, date_field: str = "publication"):
    date_field = date_field if date_field in ("judgment", "publication") else "publication"
    store = get_store()
    since = (date.today() - timedelta(days=NOWE_LOOKBACK_DAYS)).isoformat()
    latest_rows = (store.latest(limit=8, since=since, date_field=date_field)
                  if store else [])
    baza = store.count() if store else {}
    return templates.TemplateResponse(request, "home.html", {
        "latest_rows": latest_rows, "date_field": date_field,
        "baza": baza, "baza_razem": sum(baza.values())})


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
    res = _search(query, page=page, source=source, per_page=per_page)
    params = {k: v for k, v in request.query_params.items() if k != "page" and v}
    return templates.TemplateResponse(request, "results.html", {
        "q": q, "res": res, "query": query, "page": page, "params": params,
        "source": source, "per_page": per_page, "page_sizes": PAGE_SIZES})


@app.get("/orzeczenie/{source}/{doc_id}", response_class=HTMLResponse)
def document_page(request: Request, source: str, doc_id: str, q: str = ""):
    store = get_store()
    doc = store.get_document(source, doc_id) if store else None
    if doc is None:
        return templates.TemplateResponse(request, "blad.html", {
            "tytul": "Nie znaleziono orzeczenia",
            "opis": "Tego orzeczenia nie ma (jeszcze) w naszej bazie."}, status_code=404)
    return templates.TemplateResponse(request, "document.html", {"d": doc, "q": q})


@app.get("/orzeczenie/{source}/{doc_id}/pobierz.txt")
def download_document(source: str, doc_id: str):
    store = get_store()
    d = store.get_document(source, doc_id) if store else None
    if d is None:
        return JSONResponse({"error": "orzeczenie nie zostało jeszcze zaimportowane"},
                            status_code=404)

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
def new_page(request: Request, source: str = "", date_field: str = "publication",
            limit: int = Q(50, ge=1, le=200)):
    """Orzeczenia z ostatnich dwóch tygodni (okno przesuwa się razem z
    dzisiejszą datą) wg wybranej daty - orzeczenia albo publikacji."""
    date_field = date_field if date_field in ("judgment", "publication") else "publication"
    store = get_store()
    since = (date.today() - timedelta(days=NOWE_LOOKBACK_DAYS)).isoformat()
    rows = (store.latest(limit=limit, source=source, since=since, date_field=date_field)
           if store else [])
    return templates.TemplateResponse(request, "nowe.html", {
        "rows": rows, "source": source, "date_field": date_field,
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
    res = _search(query, page=page, source=source, per_page=DEFAULT_PAGE_SIZE)

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["sygnatura", "typ", "data_orzeczenia", "data_publikacji", "sad_organ"])
    for h in res.hits:
        w.writerow([h.signature, h.doc_type, h.judgment_date, h.publication_date, h.court])
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
    res = _search(query, page=page, source=source, per_page=per_page)
    return {"page": page, "per_page": per_page, "totals": res.totals, "total": res.total,
            "results": [{**h.__dict__, "url": h.url} for h in res.hits]}


@app.get("/api/orzeczenie/{source}/{doc_id}")
def api_document(source: str, doc_id: str):
    store = get_store()
    doc = store.get_document(source, doc_id) if store else None
    if doc is None:
        return JSONResponse({"error": "orzeczenie nie zostało jeszcze zaimportowane"},
                            status_code=404)
    return doc


@app.get("/api/podpowiedzi")
def api_suggest(pole: str = "", q: str = ""):
    """Podpowiedzi do autouzupełniania filtrów (sędzia/hasło tematyczne/podstawa
    prawna) na podstawie wartości już obecnych w bazie."""
    store = get_store()
    if store is None or pole not in ("judge", "thematic", "legal_basis"):
        return {"results": []}
    return {"results": store.suggest(pole, q)}


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

    if not _poll_lock.acquire(blocking=False):
        return JSONResponse(
            {"error": "poprzedni przebieg obserwatora wciąż trwa - pomijam ten"},
            status_code=409)
    try:
        from ..obserwator import run_once
        results = run_once(cfg, registry=registry, store=store)
        return {"ok": all(r.status == "ok" for r in results),
                "przebiegi": [r.as_dict() for r in results],
                "nowych": sum(r.added for r in results)}
    finally:
        _poll_lock.release()


@app.get("/api/health")
def health():
    store = get_store()
    return {"ok": True, "sources": list(registry.sources),
            "cache": len(registry.http.cache),
            "baza": store.count() if store else None,
            "baza_blad": _store_error or None}
