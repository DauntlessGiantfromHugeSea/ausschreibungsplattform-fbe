"""FastAPI-App: Dashboard, REST, Export, Auth, Probe."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .auth import install_auth, verify_credentials
from .config import PROJECT_ROOT
from .database import get_db, init_db
from .export import to_csv, to_xlsx
from .models import Tender, TenderStatus
from .pipeline import _load_scraper
from .portal_config import enabled_portals, load_portals
from .run_state import load_run_state
from .scheduler import is_pipeline_running, next_run_time, run_pipeline_with_lock
from .search_terms import load_search_config
from . import yaml_store


log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="FBE Ausschreibungsplattform", version="0.2.0")
install_auth(app)


@app.on_event("startup")
def _startup():
    init_db()


# --- Helpers --------------------------------------------------------
STATUS_VALUES = [s.value for s in TenderStatus]
LEVEL_VALUES = ["high", "medium", "low"]

SORT_OPTIONS = {
    "score_desc": (Tender.relevance_score.desc(), Tender.created_at.desc()),
    "score_asc":  (Tender.relevance_score.asc(),),
    "deadline_asc":  (Tender.deadline.asc(), Tender.relevance_score.desc()),
    "deadline_desc": (Tender.deadline.desc(),),
    "created_desc":  (Tender.created_at.desc(),),
    "created_asc":   (Tender.created_at.asc(),),
    "title_asc":     (Tender.title.asc(),),
}


def _filtered_query(
    db: Session,
    portal: Optional[str] = None,
    region: Optional[str] = None,
    status: Optional[str] = None,
    level: Optional[str] = None,
    deadline_from: Optional[str] = None,
    deadline_to: Optional[str] = None,
    q: Optional[str] = None,
    score_min: Optional[float] = None,
    score_max: Optional[float] = None,
    quick: Optional[str] = None,
):
    query = db.query(Tender)
    if portal:
        query = query.filter(Tender.portal == portal)
    if region:
        query = query.filter(Tender.region == region)
    if status:
        query = query.filter(Tender.status == status)
    if level:
        query = query.filter(Tender.relevance_level == level)
    if score_min is not None:
        query = query.filter(Tender.relevance_score >= score_min)
    if score_max is not None:
        query = query.filter(Tender.relevance_score <= score_max)
    if deadline_from:
        try:
            query = query.filter(Tender.deadline >= datetime.fromisoformat(deadline_from))
        except ValueError:
            pass
    if deadline_to:
        try:
            query = query.filter(Tender.deadline <= datetime.fromisoformat(deadline_to))
        except ValueError:
            pass
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                Tender.title.ilike(like),
                Tender.description.ilike(like),
                Tender.contracting_authority.ilike(like),
                Tender.matched_terms.ilike(like),
            )
        )

    # Quick-Filter (chips) – ueberlagern oben, kombinierbar
    now = datetime.utcnow()
    if quick == "high":
        query = query.filter(Tender.relevance_level == "high")
    elif quick == "deadline-7":
        query = query.filter(Tender.deadline >= now, Tender.deadline <= now + timedelta(days=7))
    elif quick == "deadline-14":
        query = query.filter(Tender.deadline >= now, Tender.deadline <= now + timedelta(days=14))
    elif quick == "deadline-30":
        query = query.filter(Tender.deadline >= now, Tender.deadline <= now + timedelta(days=30))
    elif quick in STATUS_VALUES:
        query = query.filter(Tender.status == quick)

    return query


def _apply_sort(query, sort: Optional[str]):
    cols = SORT_OPTIONS.get(sort or "score_desc", SORT_OPTIONS["score_desc"])
    return query.order_by(*cols)


# --- Auth Routes ---------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: str = "/", error: Optional[str] = None):
    if request.session.get("user"):
        return RedirectResponse(next, status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": error})


@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    if verify_credentials(username, password):
        request.session["user"] = username
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"next": next, "error": "Benutzername oder Passwort falsch."},
        status_code=401,
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- HTML Routes ----------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    db: Session = Depends(get_db),
    portal: Optional[str] = None,
    region: Optional[str] = None,
    status: Optional[str] = None,
    level: Optional[str] = None,
    deadline_from: Optional[str] = None,
    deadline_to: Optional[str] = None,
    q: Optional[str] = None,
    score_min: Optional[float] = None,
    score_max: Optional[float] = None,
    quick: Optional[str] = None,
    sort: Optional[str] = "score_desc",
    flash: Optional[str] = None,
    error: Optional[str] = None,
):
    query = _filtered_query(
        db,
        portal=portal, region=region, status=status, level=level,
        deadline_from=deadline_from, deadline_to=deadline_to, q=q,
        score_min=score_min, score_max=score_max, quick=quick,
    )
    tenders = _apply_sort(query, sort).limit(500).all()

    portals_distinct = [r[0] for r in db.query(Tender.portal).distinct().all() if r[0]]
    regions_distinct = [r[0] for r in db.query(Tender.region).distinct().all() if r[0]]
    total_count = db.query(Tender).count()
    high_count = db.query(Tender).filter(Tender.relevance_level == "high").count()
    interesting_count = db.query(Tender).filter(Tender.status == TenderStatus.INTERESSANT.value).count()
    soon_count = (
        db.query(Tender)
        .filter(Tender.deadline >= datetime.utcnow())
        .filter(Tender.deadline <= datetime.utcnow() + timedelta(days=14))
        .count()
    )
    stats = {
        "total": total_count,
        "high": high_count,
        "interesting": interesting_count,
        "soon": soon_count,
    }

    last_run = load_run_state()
    next_run = next_run_time()

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "tenders": tenders,
            "total_count": total_count,
            "stats": stats,
            "portals": portals_distinct,
            "regions": regions_distinct,
            "statuses": STATUS_VALUES,
            "levels": LEVEL_VALUES,
            "filters": {
                "portal": portal or "",
                "region": region or "",
                "status": status or "",
                "level": level or "",
                "deadline_from": deadline_from or "",
                "deadline_to": deadline_to or "",
                "q": q or "",
                "score_min": score_min if score_min is not None else "",
                "score_max": score_max if score_max is not None else "",
                "quick": quick or "",
                "sort": sort or "score_desc",
            },
            "configured_portals": enabled_portals(),
            "flash": flash,
            "error": error,
            "last_run": last_run,
            "next_run": next_run,
            "is_running": is_pipeline_running(),
            "user": request.session.get("user"),
        },
    )


@app.get("/tender/{tender_id}", response_class=HTMLResponse)
def detail(tender_id: int, request: Request, db: Session = Depends(get_db)):
    tender = db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(404, "Ausschreibung nicht gefunden")
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"tender": tender, "statuses": STATUS_VALUES, "user": request.session.get("user")},
    )


@app.post("/tender/{tender_id}/status")
def set_status(
    tender_id: int,
    status: str = Form(...),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    tender = db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(404, "Ausschreibung nicht gefunden")
    if status not in STATUS_VALUES:
        raise HTTPException(400, f"Unbekannter Status '{status}'")
    tender.status = status
    if notes is not None:
        tender.notes = notes
    db.commit()
    return RedirectResponse(url=f"/tender/{tender_id}", status_code=303)


@app.post("/tender/{tender_id}/quick-status")
def quick_status(tender_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    tender = db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(404, "nicht gefunden")
    if status not in STATUS_VALUES:
        raise HTTPException(400, "Unbekannter Status")
    tender.status = status
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/run-search", response_class=HTMLResponse)
def run_search_now(background_tasks: BackgroundTasks, format: Optional[str] = None):
    log.info("Manueller Suchlauf via /run-search ausgeloest.")
    if format == "json":
        stats = run_pipeline_with_lock() or {"info": "Lauf laeuft bereits"}
        return JSONResponse(stats)
    if is_pipeline_running():
        return RedirectResponse(
            url="/?flash=Ein Suchlauf läuft bereits – bitte warten.",
            status_code=303,
        )
    background_tasks.add_task(run_pipeline_with_lock)
    return RedirectResponse(
        url="/?flash=Suche im Hintergrund gestartet. Tabelle aktualisiert sich nach Abschluss.",
        status_code=303,
    )


# --- Admin: Portal-Verwaltung -----------------------------------------
SCRAPER_CHOICES = ["bund", "ted", "rss_generic", "generic_html"]


def _portals_view_ctx(request: Request, flash: str | None = None, error: str | None = None) -> dict:
    return {
        "portals": load_portals(),
        "user": request.session.get("user"),
        "flash": flash,
        "error": error,
    }


@app.get("/admin/portals", response_class=HTMLResponse)
def admin_portals(request: Request, flash: Optional[str] = None, error: Optional[str] = None):
    return templates.TemplateResponse(
        request, "portals.html", _portals_view_ctx(request, flash, error),
    )


@app.post("/admin/portals/{name}/toggle")
def admin_portal_toggle(name: str):
    raw = yaml_store.read_portals_raw()
    portals = raw.get("portals", [])
    target = next((p for p in portals if p.get("name") == name), None)
    if not target:
        return RedirectResponse(url="/admin/portals?error=Portal nicht gefunden", status_code=303)
    target["enabled"] = not bool(target.get("enabled", True))
    yaml_store.write_portals(raw)
    state = "aktiviert" if target["enabled"] else "deaktiviert"
    return RedirectResponse(url=f"/admin/portals?flash={name} {state}.", status_code=303)


@app.post("/admin/portals/{name}/delete")
def admin_portal_delete(name: str):
    raw = yaml_store.read_portals_raw()
    portals = [p for p in raw.get("portals", []) if p.get("name") != name]
    if len(portals) == len(raw.get("portals", [])):
        return RedirectResponse(url="/admin/portals?error=Portal nicht gefunden", status_code=303)
    raw["portals"] = portals
    yaml_store.write_portals(raw)
    return RedirectResponse(url=f"/admin/portals?flash={name} geloescht.", status_code=303)


@app.get("/admin/portals/new", response_class=HTMLResponse)
def admin_portal_new(request: Request):
    return templates.TemplateResponse(
        request, "portal_edit.html",
        {
            "portal": None,
            "scraper_choices": SCRAPER_CHOICES,
            "user": request.session.get("user"),
            "is_new": True,
        },
    )


@app.get("/admin/portals/{name}/edit", response_class=HTMLResponse)
def admin_portal_edit(name: str, request: Request):
    raw = yaml_store.read_portals_raw()
    portal = next((p for p in raw.get("portals", []) if p.get("name") == name), None)
    if not portal:
        return RedirectResponse(url="/admin/portals?error=Portal nicht gefunden", status_code=303)
    return templates.TemplateResponse(
        request, "portal_edit.html",
        {
            "portal": portal,
            "scraper_choices": SCRAPER_CHOICES,
            "user": request.session.get("user"),
            "is_new": False,
            "config_yaml": yaml_store.yaml.safe_dump(portal.get("config", {}) or {}, allow_unicode=True, sort_keys=False) if portal.get("config") else "",
        },
    )


@app.post("/admin/portals/save")
def admin_portal_save(
    request: Request,
    original_name: str = Form(""),
    name: str = Form(...),
    scraper: str = Form(...),
    base_url: str = Form(""),
    strategy: str = Form("scrape"),
    enabled: Optional[str] = Form(None),
    notes: str = Form(""),
    config_yaml: str = Form(""),
):
    name = name.strip()
    if not name:
        return RedirectResponse(url="/admin/portals?error=Name ist erforderlich.", status_code=303)

    try:
        cfg_data = yaml_store.parse_yaml_string(config_yaml) if config_yaml.strip() else {}
    except Exception as exc:
        return RedirectResponse(
            url=f"/admin/portals?error=Config-YAML ungueltig: {str(exc)[:200]}",
            status_code=303,
        )

    raw = yaml_store.read_portals_raw()
    portals = raw.get("portals", [])

    new_entry = {
        "name": name,
        "enabled": enabled == "on",
        "scraper": scraper.strip(),
        "base_url": base_url.strip(),
        "strategy": strategy.strip() or "scrape",
        "notes": notes.strip(),
    }
    if cfg_data:
        new_entry["config"] = cfg_data

    if original_name:
        # Update bestehender Eintrag.
        for i, p in enumerate(portals):
            if p.get("name") == original_name:
                portals[i] = new_entry
                break
        else:
            portals.append(new_entry)
    else:
        if any(p.get("name") == name for p in portals):
            return RedirectResponse(
                url=f"/admin/portals?error=Portal mit Name '{name}' existiert bereits.",
                status_code=303,
            )
        portals.append(new_entry)

    raw["portals"] = portals
    yaml_store.write_portals(raw)
    return RedirectResponse(url=f"/admin/portals?flash={name} gespeichert.", status_code=303)


# --- Admin: Suchbegriffe ----------------------------------------------
@app.get("/admin/search-terms", response_class=HTMLResponse)
def admin_search_terms(request: Request, flash: Optional[str] = None, error: Optional[str] = None):
    raw = yaml_store.read_terms_raw()
    yaml_text = yaml_store.yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False, width=120)
    return templates.TemplateResponse(
        request, "search_terms.html",
        {
            "raw": raw,
            "yaml_text": yaml_text,
            "user": request.session.get("user"),
            "flash": flash,
            "error": error,
        },
    )


@app.post("/admin/search-terms")
def admin_search_terms_save(request: Request, yaml_text: str = Form(...)):
    try:
        data = yaml_store.parse_yaml_string(yaml_text)
    except Exception as exc:
        return RedirectResponse(
            url=f"/admin/search-terms?error=YAML ungueltig: {str(exc)[:200]}",
            status_code=303,
        )
    yaml_store.write_terms(data)
    return RedirectResponse(url="/admin/search-terms?flash=Suchbegriffe gespeichert.", status_code=303)


@app.get("/admin/probe", response_class=HTMLResponse)
def admin_probe_get(request: Request, portal: Optional[str] = None):
    return templates.TemplateResponse(
        request, "probe.html",
        {"portals": load_portals(), "selected": portal,
         "result": None, "user": request.session.get("user")},
    )


@app.post("/admin/probe", response_class=HTMLResponse)
def admin_probe_post(request: Request, portal: str = Form(...), term: str = Form("Flüssigboden")):
    chosen = next((p for p in load_portals() if p.name == portal), None)
    if not chosen:
        raise HTTPException(404, "Portal nicht gefunden")

    result = {"name": chosen.name, "term": term, "items": [], "error": None, "count": 0}
    try:
        ScraperCls = _load_scraper(chosen)
        with ScraperCls(base_url=chosen.base_url, name=chosen.name, config=chosen.config) as sc:
            items = sc.fetch([term])
        result["count"] = len(items)
        result["items"] = items[:5]
    except Exception as exc:
        log.exception("Probe %s fehlgeschlagen: %s", chosen.name, exc)
        result["error"] = f"{type(exc).__name__}: {exc}"

    return templates.TemplateResponse(
        request, "probe.html",
        {"portals": load_portals(), "selected": portal,
         "result": result, "term": term, "user": request.session.get("user")},
    )


# --- Export ---------------------------------------------------------
@app.get("/export/csv")
def export_csv(
    db: Session = Depends(get_db),
    portal: Optional[str] = None,
    region: Optional[str] = None,
    status: Optional[str] = None,
    level: Optional[str] = None,
    deadline_from: Optional[str] = None,
    deadline_to: Optional[str] = None,
    q: Optional[str] = None,
    score_min: Optional[float] = None,
    score_max: Optional[float] = None,
    quick: Optional[str] = None,
    sort: Optional[str] = "score_desc",
):
    query = _filtered_query(
        db, portal=portal, region=region, status=status, level=level,
        deadline_from=deadline_from, deadline_to=deadline_to, q=q,
        score_min=score_min, score_max=score_max, quick=quick,
    )
    tenders = _apply_sort(query, sort).all()
    return Response(
        content=to_csv(tenders),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="ausschreibungen.csv"'},
    )


@app.get("/export/xlsx")
def export_xlsx(
    db: Session = Depends(get_db),
    portal: Optional[str] = None,
    region: Optional[str] = None,
    status: Optional[str] = None,
    level: Optional[str] = None,
    deadline_from: Optional[str] = None,
    deadline_to: Optional[str] = None,
    q: Optional[str] = None,
    score_min: Optional[float] = None,
    score_max: Optional[float] = None,
    quick: Optional[str] = None,
    sort: Optional[str] = "score_desc",
):
    query = _filtered_query(
        db, portal=portal, region=region, status=status, level=level,
        deadline_from=deadline_from, deadline_to=deadline_to, q=q,
        score_min=score_min, score_max=score_max, quick=quick,
    )
    tenders = _apply_sort(query, sort).all()
    return Response(
        content=to_xlsx(tenders),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="ausschreibungen.xlsx"'},
    )


# --- JSON-API -------------------------------------------------------
@app.get("/api/tenders")
def api_list(
    db: Session = Depends(get_db),
    portal: Optional[str] = None,
    region: Optional[str] = None,
    status: Optional[str] = None,
    level: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(200, le=2000),
):
    query = _filtered_query(db, portal=portal, region=region, status=status, level=level, q=q)
    tenders = query.order_by(Tender.relevance_score.desc()).limit(limit).all()
    return [t.to_dict() for t in tenders]


@app.get("/api/health", response_class=PlainTextResponse)
def health():
    return "ok"
