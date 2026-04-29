"""FastAPI-App: Dashboard, REST, Export."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .config import PROJECT_ROOT
from .database import get_db, init_db
from .export import to_csv, to_xlsx
from .models import Tender, TenderStatus
from .pipeline import run_pipeline
from .portal_config import enabled_portals


log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="FBE Ausschreibungsplattform", version="0.1.0")


@app.on_event("startup")
def _startup():
    init_db()


# --- Helpers --------------------------------------------------------
STATUS_VALUES = [s.value for s in TenderStatus]
LEVEL_VALUES = ["high", "medium", "low"]


def _filtered_query(
    db: Session,
    portal: Optional[str] = None,
    region: Optional[str] = None,
    status: Optional[str] = None,
    level: Optional[str] = None,
    deadline_from: Optional[str] = None,
    deadline_to: Optional[str] = None,
    q: Optional[str] = None,
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
    return query


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
):
    query = _filtered_query(
        db, portal=portal, region=region, status=status, level=level,
        deadline_from=deadline_from, deadline_to=deadline_to, q=q,
    )
    tenders = query.order_by(Tender.relevance_score.desc(), Tender.created_at.desc()).limit(500).all()

    portals_distinct = [r[0] for r in db.query(Tender.portal).distinct().all() if r[0]]
    regions_distinct = [r[0] for r in db.query(Tender.region).distinct().all() if r[0]]

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "tenders": tenders,
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
            },
            "configured_portals": enabled_portals(),
        },
    )


@app.get("/tender/{tender_id}", response_class=HTMLResponse)
def detail(tender_id: int, request: Request, db: Session = Depends(get_db)):
    tender = db.query(Tender).get(tender_id)
    if not tender:
        raise HTTPException(404, "Ausschreibung nicht gefunden")
    return templates.TemplateResponse(
        "detail.html",
        {"request": request, "tender": tender, "statuses": STATUS_VALUES},
    )


@app.post("/tender/{tender_id}/status")
def set_status(
    tender_id: int,
    status: str = Form(...),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    tender = db.query(Tender).get(tender_id)
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
    tender = db.query(Tender).get(tender_id)
    if not tender:
        raise HTTPException(404, "nicht gefunden")
    if status not in STATUS_VALUES:
        raise HTTPException(400, "Unbekannter Status")
    tender.status = status
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/run-search")
def run_search_now():
    """Startet einen Pipeline-Lauf synchron (MVP). Liefert Statistik."""
    log.info("Manueller Suchlauf via /run-search ausgeloest.")
    stats = run_pipeline()
    return JSONResponse(stats)


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
):
    query = _filtered_query(
        db, portal=portal, region=region, status=status, level=level,
        deadline_from=deadline_from, deadline_to=deadline_to, q=q,
    )
    tenders = query.order_by(Tender.relevance_score.desc()).all()
    csv_data = to_csv(tenders)
    return Response(
        content=csv_data,
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
):
    query = _filtered_query(
        db, portal=portal, region=region, status=status, level=level,
        deadline_from=deadline_from, deadline_to=deadline_to, q=q,
    )
    tenders = query.order_by(Tender.relevance_score.desc()).all()
    xlsx = to_xlsx(tenders)
    return Response(
        content=xlsx,
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
