"""Orzecznik - nakładka na publiczne portale orzecznictwa.

Aplikacja nie przechowuje orzeczeń. Każde wyszukanie i każde otwarcie
dokumentu to zapytanie na żywo do serwisu źródłowego; my tylko parsujemy
odpowiedź i pokazujemy ją w spójnym interfejsie.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query as Q, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import load_config
from ..format import date_pl, plural_pl
from ..http import RateLimited, SourceUnavailable
from ..sources import Query, Registry

BASE_DIR = Path(__file__).parent
cfg = load_config()
registry = Registry(cfg)

app = FastAPI(title=cfg.web.site_name, docs_url="/api/docs", openapi_url="/api/openapi.json")
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
    merged.update({k: v for k, v in override.items() if v not in (None, "")})
    return urlencode(merged)


templates.env.filters["urlencode_page"] = lambda p, page: _qs(p, page=page)
templates.env.filters["urlencode_extra"] = lambda p, k, v: _qs(p, **{k: v, "page": 1})


@app.on_event("shutdown")
def _shutdown() -> None:
    registry.close()


def _query(**kw: str) -> Query:
    return Query(**{k: (v or "").strip() for k, v in kw.items()})


# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    try:
        latest = registry.latest(limit=8)
    except Exception:
        latest = None
    return templates.TemplateResponse(request, "home.html", {"latest": latest})


@app.get("/szukaj", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = "", signature: str = "", judge: str = "", thematic: str = "",
    legal_basis: str = "", date_field: str = "judgment",
    date_from: str = "", date_to: str = "",
    sort: str = "relevance", source: str = "", page: int = Q(1, ge=1),
):
    query = _query(phrase=q, signature=signature, judge=judge, thematic=thematic,
                   legal_basis=legal_basis, date_field=date_field,
                   date_from=date_from, date_to=date_to, sort=sort)
    res = registry.search(query, page=page, only=source)
    params = {k: v for k, v in request.query_params.items() if k != "page" and v}
    return templates.TemplateResponse(request, "results.html", {
        "q": q, "res": res, "query": query, "page": page, "params": params,
        "source": source})


@app.get("/orzeczenie/{source}/{doc_id}", response_class=HTMLResponse)
def document_page(request: Request, source: str, doc_id: str, q: str = ""):
    try:
        doc = registry.document(source, doc_id)
    except (RateLimited, SourceUnavailable, ValueError) as exc:
        return templates.TemplateResponse(request, "blad.html", {
            "tytul": "Nie udało się pobrać orzeczenia", "opis": str(exc)}, status_code=502)
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
               source: str = "", page: int = Q(1, ge=1)):
    query = _query(phrase=q, signature=signature, judge=judge, thematic=thematic,
                   legal_basis=legal_basis, date_field=date_field,
                   date_from=date_from, date_to=date_to, sort=sort)
    res = registry.search(query, page=page, only=source)
    return {"page": page, "totals": res.totals, "total": res.total, "errors": res.errors,
            "results": [{**h.__dict__, "url": h.url} for h in res.hits]}


@app.get("/api/orzeczenie/{source}/{doc_id}")
def api_document(source: str, doc_id: str):
    try:
        return registry.document(source, doc_id)
    except (RateLimited, SourceUnavailable, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/health")
def health():
    return {"ok": True, "sources": list(registry.sources), "cache": len(registry.http.cache)}
