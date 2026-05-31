"""FastAPI-App: Dashboard, REST, Export, Auth, Probe."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import json
from fastapi import BackgroundTasks, Body, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .auth import authenticate, hash_password, install_auth, is_restricted, require_admin
from . import branding
from .config import PROJECT_ROOT, settings
from .database import get_db, init_db
from .export import to_csv, to_xlsx
from .models import Comment, Tender, TenderStatus, SearchProfile, TenderEvent, User
from .pipeline import _load_scraper
from .portal_config import enabled_portals, load_portals
from .run_state import load_run_state
from .scheduler import force_release_lock, is_pipeline_running, next_run_time, run_pipeline_with_lock
from .search_terms import load_search_config
from . import yaml_store


log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["has_logo"] = branding.has_logo

app = FastAPI(title="FBE Ausschreibungsplattform", version="0.2.0")

# Static-Ordner muss existieren bevor StaticFiles mountet, sonst crasht
# der Service-Start mit RuntimeError ("Directory does not exist") - der
# Ordner wird im Repo per .gitkeep getrackt, aber wir sichern hier zusaetzlich
# fuer Worktrees ohne den Marker ab.
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
install_auth(app)


@app.on_event("startup")
def _startup():
    init_db()
    from . import migrations
    migrations.run_all()
    branding.ensure_logo()


# --- Helpers --------------------------------------------------------
STATUS_VALUES = [s.value for s in TenderStatus]
LEVEL_VALUES = ["high", "medium", "low"]
PER_PAGE_OPTIONS = [50, 100, 250]
PER_PAGE_ALL = 99999


def _normalize_per_page(value: int) -> int:
    """Keep dashboard page sizes bounded, with 99999 as the explicit all option."""
    if value == PER_PAGE_ALL:
        return PER_PAGE_ALL
    if value in PER_PAGE_OPTIONS:
        return value
    return 50


def _session_user(request: Request) -> dict | None:
    """Liefert den eingeloggten User als dict {username, role, id} oder None.

    Die Login-Route schreibt username/role/user_id getrennt in die Session;
    Templates und Comment-Routen erwarten ein dict - hier zentralisiert.
    """
    username = request.session.get("user")
    if not username:
        return None
    return {
        "username": username,
        "role": request.session.get("role") or "viewer",
        "id": request.session.get("user_id") or 0,
    }


def _portal_status_summary(last_run: dict | None) -> dict:
    """Zaehlt OK / leer / Fehler / nicht-gelaufen pro konfiguriertem Portal."""
    portals = list(load_portals())
    last_by_name: dict[str, dict] = {}
    if last_run and isinstance(last_run.get("portals"), list):
        for p in last_run["portals"]:
            if isinstance(p, dict) and p.get("name"):
                last_by_name[p["name"]] = p
    ok = empty = error = never = 0
    for cp in portals:
        if not cp.enabled:
            continue
        p = last_by_name.get(cp.name)
        if not p:
            never += 1
        elif (p.get("errors") or 0) > 0:
            error += 1
        elif (p.get("fetched") or 0) > 0:
            ok += 1
        else:
            empty += 1
    return {
        "total": ok + empty + error + never,
        "ok": ok, "empty": empty, "error": error, "never": never,
    }

SORT_OPTIONS = {
    "score_desc": (Tender.relevance_score.desc(), Tender.created_at.desc()),
    "score_asc":  (Tender.relevance_score.asc(),),
    "deadline_asc":  (Tender.deadline.asc(), Tender.relevance_score.desc()),
    "deadline_desc": (Tender.deadline.desc(),),
    "created_desc":  (Tender.created_at.desc(),),
    "created_asc":   (Tender.created_at.asc(),),
    "title_asc":     (Tender.title.asc(),),
    "region_asc":    (Tender.region.asc(), Tender.relevance_score.desc()),
    "region_desc":   (Tender.region.desc(), Tender.relevance_score.desc()),
}


def _safe_float(value, default=None):
    """Tolerantes Float-Parsing fuer Query-Parameter aus HTML-Forms:
    leerer String / None -> default, ungueltige Werte -> default."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
    include_expired: bool = False,
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

    # Default: nur LAUFENDE Ausschreibungen anzeigen (Frist heute oder spaeter,
    # oder gar keine Frist gesetzt). Mit include_expired=True wird der Filter
    # uebersprungen.
    now = datetime.utcnow()
    if not include_expired:
        query = query.filter(
            or_(Tender.deadline.is_(None), Tender.deadline >= now)
        )

    # Quick-Filter (chips) – ueberlagern oben, kombinierbar
    if quick == "high":
        query = query.filter(Tender.relevance_level == "high")
    elif quick == "deadline-7":
        query = query.filter(Tender.deadline >= now, Tender.deadline <= now + timedelta(days=7))
    elif quick == "deadline-14":
        query = query.filter(Tender.deadline >= now, Tender.deadline <= now + timedelta(days=14))
    elif quick == "deadline-30":
        query = query.filter(Tender.deadline >= now, Tender.deadline <= now + timedelta(days=30))
    elif quick == "expired":
        query = query.filter(Tender.deadline < now)
    elif quick in STATUS_VALUES:
        query = query.filter(Tender.status == quick)

    return query


def _apply_sort(query, sort: Optional[str]):
    cols = SORT_OPTIONS.get(sort or "score_desc", SORT_OPTIONS["score_desc"])
    return query.order_by(*cols)


# --- Profil-Enforcement fuer restricted-User ----------------------
def _profile_filter_expr(profile: SearchProfile):
    """SQLAlchemy-Filter, der EINEN Profil-Filter beschreibt.

    Mehrere keywords innerhalb eines Profils sind OR-verknuepft;
    keywords + portal/region/score sind AND-verknuepft.
    """
    from sqlalchemy import and_ as _and

    conds = []

    kws = profile.keyword_list()
    if kws:
        kw_or = []
        for kw in kws:
            like = f"%{kw}%"
            kw_or.append(or_(
                Tender.title.ilike(like),
                Tender.description.ilike(like),
                Tender.matched_terms.ilike(like),
                Tender.contracting_authority.ilike(like),
            ))
        conds.append(or_(*kw_or))
    elif profile.query:
        # Legacy-Profile ohne keywords-Liste: query-Feld als einziger Begriff
        like = f"%{profile.query}%"
        conds.append(or_(
            Tender.title.ilike(like),
            Tender.description.ilike(like),
            Tender.matched_terms.ilike(like),
            Tender.contracting_authority.ilike(like),
        ))

    if profile.portal:
        conds.append(Tender.portal == profile.portal)
    if profile.region:
        conds.append(Tender.region == profile.region)
    if profile.status:
        conds.append(Tender.status == profile.status)
    if profile.level:
        conds.append(Tender.relevance_level == profile.level)
    if profile.score_min is not None:
        conds.append(Tender.relevance_score >= profile.score_min)
    if profile.score_max is not None:
        conds.append(Tender.relevance_score <= profile.score_max)
    if profile.deadline_days:
        until = datetime.utcnow() + timedelta(days=profile.deadline_days)
        conds.append(Tender.deadline.isnot(None))
        conds.append(Tender.deadline <= until)

    if not conds:
        # Leeres Profil matched nichts - sonst koennte ein leeres Profil
        # einem User unbeabsichtigt ALLE Tender oeffnen.
        return Tender.id == -1
    return _and(*conds)


def _user_assigned_profiles(db: Session, request: Request) -> list[SearchProfile]:
    """Liefert die SearchProfiles, die dem eingeloggten User zugewiesen sind.

    Nutzt session.user_id; fuer den Env-Admin-Fallback (id=0) leer.
    """
    uid = request.session.get("user_id")
    if not uid:
        return []
    user = db.get(User, uid)
    if not user:
        return []
    return list(user.assigned_profiles)


def _enforce_restricted(
    db: Session,
    request: Request,
    base_query,
    selected_profile_id: Optional[int] = None,
) -> tuple[object, list[SearchProfile], Optional[SearchProfile]]:
    """Schraenkt base_query auf die zugewiesenen Profile des Restricted-Users ein.

    Liefert (query, alle_zugewiesenen_profile, gewaehltes_profil_oder_None).
    Wenn selected_profile_id gesetzt aber dem User nicht zugewiesen -> 403-aequivalent
    (query auf 'nichts'). Wenn keine Profile zugewiesen -> query auf 'nichts'.
    """
    profiles = _user_assigned_profiles(db, request)
    if not profiles:
        return base_query.filter(Tender.id == -1), [], None

    selected = None
    if selected_profile_id:
        for p in profiles:
            if p.id == selected_profile_id:
                selected = p
                break
        if not selected:
            return base_query.filter(Tender.id == -1), profiles, None

    if selected:
        return base_query.filter(_profile_filter_expr(selected)), profiles, selected
    # Kein konkretes Profil gewaehlt - Union ueber alle zugewiesenen
    return base_query.filter(or_(*[_profile_filter_expr(p) for p in profiles])), profiles, None


# --- Auth Routes ---------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: str = "/", error: Optional[str] = None, flash: Optional[str] = None):
    if request.session.get("user"):
        return RedirectResponse(next, status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": error, "flash": flash})


@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    user = authenticate(username, password)
    if user:
        request.session["user"] = user["username"]
        request.session["role"] = user["role"]
        request.session["user_id"] = user["id"]
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


def _base_url(request: Request) -> str:
    """Liefert externe Basis-URL fuer Mail-Links.

    Bevorzugt settings.public_base_url (falls gesetzt), sonst die in der
    Request enthaltene URL inkl. korrektem Scheme/Host hinter Reverse-Proxy.
    """
    base = (getattr(settings, "public_base_url", None) or "").rstrip("/")
    if base:
        return base
    fwd_proto = request.headers.get("x-forwarded-proto")
    fwd_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    scheme = fwd_proto or request.url.scheme
    host = fwd_host or request.url.netloc
    return f"{scheme}://{host}"


# --- Passwort vergessen / Reset / Invite ---------------------------------
@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_get(request: Request, flash: Optional[str] = None):
    return templates.TemplateResponse(
        request, "forgot_password.html", {"flash": flash, "user": None},
    )


@app.post("/forgot-password")
def forgot_password_post(
    request: Request,
    identifier: str = Form(...),
    db: Session = Depends(get_db),
):
    from .auth import generate_token, reset_expires_at
    from . import notify as notify_mod

    ident = (identifier or "").strip()
    # Aus Datenschutzgruenden immer die gleiche Erfolgsmeldung
    generic_flash = "Falls ein Konto existiert, wurde ein Reset-Link verschickt."

    if ident:
        user = (
            db.query(User)
            .filter(or_(User.username == ident, User.email == ident))
            .first()
        )
        if user and user.is_active and user.email:
            user.reset_token = generate_token()
            user.reset_token_expires_at = reset_expires_at()
            db.commit()
            link = f"{_base_url(request)}/set-password?token={user.reset_token}&reset=1"
            try:
                notify_mod.send_password_reset_mail(user.email, user.username, link)
            except Exception:
                log.exception("Passwort-Reset-Mail konnte nicht gesendet werden")

    return RedirectResponse(
        url=f"/forgot-password?flash={quote(generic_flash)}",
        status_code=303,
    )


@app.get("/set-password", response_class=HTMLResponse)
def set_password_get(
    request: Request,
    token: str = "",
    reset: Optional[str] = None,
    db: Session = Depends(get_db),
):
    is_reset = bool(reset)
    is_invite = not is_reset
    user = None
    if token:
        if is_reset:
            user = db.query(User).filter(User.reset_token == token).first()
            if user and user.reset_token_expires_at and user.reset_token_expires_at < datetime.utcnow():
                user = None
        else:
            user = db.query(User).filter(User.invite_token == token).first()
            if user and user.invite_token_expires_at and user.invite_token_expires_at < datetime.utcnow():
                user = None
    return templates.TemplateResponse(
        request, "set_password.html",
        {
            "token": token if user else "",
            "username": user.username if user else None,
            "is_reset": is_reset,
            "is_invite": is_invite,
            "user": None,
        },
    )


def _apply_new_password(
    request: Request,
    db: Session,
    token: str,
    password: str,
    password2: str,
    field: str,
    expires_field: str,
    is_reset: bool,
):
    if not token:
        return RedirectResponse(url="/login?error=Token fehlt.", status_code=303)
    if not password or len(password) < 6:
        return templates.TemplateResponse(
            request, "set_password.html",
            {"token": token, "is_reset": is_reset, "is_invite": not is_reset,
             "error": "Passwort zu kurz (min. 6 Zeichen).", "user": None},
        )
    if password != password2:
        return templates.TemplateResponse(
            request, "set_password.html",
            {"token": token, "is_reset": is_reset, "is_invite": not is_reset,
             "error": "Passwoerter stimmen nicht ueberein.", "user": None},
        )
    user = db.query(User).filter(getattr(User, field) == token).first()
    exp = getattr(user, expires_field, None) if user else None
    if not user or (exp and exp < datetime.utcnow()):
        return templates.TemplateResponse(
            request, "set_password.html",
            {"token": "", "is_reset": is_reset, "is_invite": not is_reset, "user": None},
        )
    user.password_hash = hash_password(password)
    user.is_active = True
    setattr(user, field, None)
    setattr(user, expires_field, None)
    db.commit()
    flash = "Passwort gesetzt - du kannst dich jetzt anmelden."
    return RedirectResponse(url=f"/login?flash={quote(flash)}", status_code=303)


@app.post("/set-password")
def set_password_post(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    db: Session = Depends(get_db),
):
    return _apply_new_password(
        request, db, token, password, password2,
        field="invite_token", expires_field="invite_token_expires_at",
        is_reset=False,
    )


@app.post("/reset-password")
def reset_password_post(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    db: Session = Depends(get_db),
):
    return _apply_new_password(
        request, db, token, password, password2,
        field="reset_token", expires_field="reset_token_expires_at",
        is_reset=True,
    )


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
    score_min: Optional[str] = "30",
    score_max: Optional[str] = None,
    quick: Optional[str] = None,
    sort: Optional[str] = "score_desc",
    page: int = 1,
    per_page: int = 50,
    include_expired: Optional[str] = None,
    profile: Optional[int] = None,
    flash: Optional[str] = None,
    error: Optional[str] = None,
):
    per_page = _normalize_per_page(per_page)
    expired_flag = bool(include_expired)
    restricted = is_restricted(request)

    if restricted:
        # Restricted-User: ALLE freien Filter-Params werden ignoriert.
        # Nur 'profile', 'status', 'sort', 'page', 'per_page' werden respektiert.
        portal = region = level = deadline_from = deadline_to = None
        q = None
        score_min = None
        score_max = None
        quick = None
        expired_flag = False
        query = _filtered_query(db, status=status, include_expired=False)
        query, user_profiles, selected_profile = _enforce_restricted(
            db, request, query, selected_profile_id=profile,
        )
    else:
        # Score-Werte tolerant parsen: leere Strings -> None, ungueltige -> None
        score_min = _safe_float(score_min, default=30.0)
        score_max = _safe_float(score_max, default=None)
        query = _filtered_query(
            db,
            portal=portal, region=region, status=status, level=level,
            deadline_from=deadline_from, deadline_to=deadline_to, q=q,
            score_min=score_min, score_max=score_max, quick=quick,
            include_expired=expired_flag,
        )
        user_profiles = []
        selected_profile = None

    total_results = query.count()
    total_pages = max(1, (total_results + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    tenders = _apply_sort(query, sort).offset(offset).limit(per_page).all()

    # Portal-Filter zeigt ALLE konfigurierten + alle in der DB vorhandenen
    # Portale - nicht nur die mit bisherigen Treffern. Sonst sieht der User
    # vor dem ersten Lauf nur ein einziges Portal im Dropdown.
    portals_in_db = {r[0] for r in db.query(Tender.portal).distinct().all() if r[0]}
    portals_configured = {p.name for p in load_portals() if p.name}
    portals_distinct = sorted(portals_in_db | portals_configured, key=str.lower)
    regions_distinct = [r[0] for r in db.query(Tender.region).distinct().all() if r[0]]
    # Counts pro Region (auf den AKTUELL gefilterten query, abzüglich region-Filter selbst)
    from sqlalchemy import func as _sqlfunc
    if restricted:
        # Restricted: Region-Counts wuerden Inhalte ausserhalb des Profils
        # zeigen koennen - bleibt leer, im UI sind die Filter ohnehin weg.
        region_counts = {}
        _q_status = _filtered_query(db, include_expired=False)
        _q_status, _, _ = _enforce_restricted(db, request, _q_status, selected_profile_id=profile)
        status_counts = dict(_q_status.with_entities(Tender.status, _sqlfunc.count(Tender.id)).group_by(Tender.status).all())
        status_counts = {k: v for k, v in status_counts.items() if k}
    else:
        _q_for_counts = _filtered_query(db, portal=portal, region=None, status=status, level=level, deadline_from=deadline_from, deadline_to=deadline_to, q=q, score_min=score_min, score_max=score_max, quick=quick)
        region_counts = dict(_q_for_counts.with_entities(Tender.region, _sqlfunc.count(Tender.id)).group_by(Tender.region).all())
        region_counts = {k: v for k, v in region_counts.items() if k}
        _q_for_status_counts = _filtered_query(db, portal=portal, region=region, status=None, level=level, deadline_from=deadline_from, deadline_to=deadline_to, q=q, score_min=score_min, score_max=score_max, quick=quick)
        status_counts = dict(_q_for_status_counts.with_entities(Tender.status, _sqlfunc.count(Tender.id)).group_by(Tender.status).all())
        status_counts = {k: v for k, v in status_counts.items() if k}
    if restricted:
        # Stats werden im UI fuer Restricted nicht angezeigt, aber wir
        # liefern konsistente Werte fuer das Profil-gefilterte Set.
        total_count = total_results
        high_q = _filtered_query(db, include_expired=False).filter(Tender.relevance_level == "high")
        high_q, _, _ = _enforce_restricted(db, request, high_q, selected_profile_id=profile)
        high_count = high_q.count()
        interesting_count = status_counts.get(TenderStatus.INTERESSANT.value, 0)
        soon_q = _filtered_query(db, include_expired=False).filter(
            Tender.deadline >= datetime.utcnow(),
            Tender.deadline <= datetime.utcnow() + timedelta(days=14),
        )
        soon_q, _, _ = _enforce_restricted(db, request, soon_q, selected_profile_id=profile)
        soon_count = soon_q.count()
    else:
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
    if restricted:
        # Restricted-User sehen nur ihre zugewiesenen Profile - nur name +
        # description, KEINE Filter-Felder (keywords, query etc.).
        profiles = user_profiles
    else:
        profiles = db.query(SearchProfile).order_by(SearchProfile.name).all()

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "tenders": tenders,
            "total_count": total_count,
            "stats": stats,
            "portals": portals_distinct,
            "regions": regions_distinct,
            "region_counts": region_counts,
            "status_counts": status_counts,
            "total_results": total_results,
            "total_pages": total_pages,
            "current_page": page,
            "per_page": per_page,
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
                "include_expired": "1" if expired_flag else "",
            },
            "configured_portals": enabled_portals(),
            "status_summary": _portal_status_summary(last_run),
            "flash": flash,
            "error": error,
            "last_run": last_run,
            "next_run": next_run,
            "is_running": is_pipeline_running(),
            "user": request.session.get("user"),
            "profiles": profiles,
            "restricted": restricted,
            "selected_profile": selected_profile,
        },
    )


@app.get("/tender/{tender_id}", response_class=HTMLResponse)
def detail(
    tender_id: int,
    request: Request,
    flash: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    tender = db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(404, "Ausschreibung nicht gefunden")

    # Restricted-User: nur sichtbar, wenn der Tender zu einem zugewiesenen
    # Profil passt. Sonst gleiche Antwort wie 404 (keine Existenz-Info leaken).
    if is_restricted(request):
        q = db.query(Tender).filter(Tender.id == tender_id)
        q, _, _ = _enforce_restricted(db, request, q)
        if q.first() is None:
            raise HTTPException(404, "Ausschreibung nicht gefunden")

    breakdown: list = []
    if tender.score_breakdown:
        try:
            import json as _json
            breakdown = _json.loads(tender.score_breakdown) or []
        except Exception:
            breakdown = []

    comments = (
        db.query(Comment)
        .filter(Comment.tender_id == tender_id)
        .order_by(Comment.created_at.asc())
        .all()
    )
    events = (
        db.query(TenderEvent)
        .filter(TenderEvent.tender_id == tender_id)
        .order_by(TenderEvent.created_at.desc())
        .all()
    )
    mail_ready = bool(settings.smtp_host and settings.smtp_from)
    from . import ai as ai_mod

    # Anreicherungs-Markdown aus dem Shared-Volume des Enricher-Containers.
    # Wenn die Datei existiert -> in der Detail-Seite anzeigen.
    enrich_markdown = None
    enrich_path = None
    try:
        from pathlib import Path as _Path
        p = _Path(settings.enrich_dir) / f"tender-{tender_id}.md"
        if p.is_file():
            enrich_markdown = p.read_text(encoding="utf-8", errors="replace")
            enrich_path = str(p)
    except Exception:  # pragma: no cover
        pass

    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "tender": tender,
            "statuses": STATUS_VALUES,
            "user": _session_user(request),
            "score_breakdown": breakdown,
            "comments": comments,
            "events": events,
            "mail_ready": mail_ready,
            "ai_configured": ai_mod.is_configured(),
            "ai_auto": settings.ai_auto_analyze,
            "enrich_markdown": enrich_markdown,
            "enrich_path": enrich_path,
            "flash": flash,
            "error": error,
        },
    )


@app.post("/tender/{tender_id}/comments")
def add_comment(
    tender_id: int,
    request: Request,
    body: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _session_user(request)
    if not user:
        raise HTTPException(401, "Nicht angemeldet")
    tender = db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(404, "Ausschreibung nicht gefunden")

    body = (body or "").strip()
    if not body:
        return RedirectResponse(url=f"/tender/{tender_id}", status_code=303)
    if len(body) > 5000:
        body = body[:5000]

    user_id = user["id"] if user["id"] > 0 else None
    db.add(Comment(
        tender_id=tender_id,
        user_id=user_id,
        username=user["username"][:80],
        body=body,
    ))
    db.commit()
    return RedirectResponse(
        url=f"/tender/{tender_id}#comments", status_code=303,
    )


@app.post("/tender/{tender_id}/comments/{comment_id}/delete")
def delete_comment(
    tender_id: int,
    comment_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _session_user(request)
    if not user:
        raise HTTPException(401, "Nicht angemeldet")
    comment = db.get(Comment, comment_id)
    if not comment or comment.tender_id != tender_id:
        raise HTTPException(404, "Kommentar nicht gefunden")
    is_admin = user["role"] == "admin"
    is_author = user["username"] == comment.username
    if not (is_admin or is_author):
        raise HTTPException(403, "Nur eigene Kommentare oder Admin")
    db.delete(comment)
    db.commit()
    return RedirectResponse(
        url=f"/tender/{tender_id}#comments", status_code=303,
    )


def _log_event(db: Session, tender_id: int, user: dict | None, event_type: str, detail: str):
    db.add(TenderEvent(
        tender_id=tender_id,
        user_id=(user["id"] if user and user.get("id", 0) > 0 else None),
        username=(user["username"] if user else "system")[:80],
        event_type=event_type,
        detail=detail,
    ))


@app.post("/tender/{tender_id}/status")
def set_status(
    tender_id: int,
    request: Request,
    status: str = Form(...),
    notes: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    tender = db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(404, "Ausschreibung nicht gefunden")
    if status not in STATUS_VALUES:
        raise HTTPException(400, f"Unbekannter Status '{status}'")
    old = tender.status
    tender.status = status
    if notes is not None:
        tender.notes = notes
    if old != status:
        _log_event(db, tender_id, _session_user(request), "status",
                   f"{old or 'neu'} → {status}")
    db.commit()
    return RedirectResponse(url=f"/tender/{tender_id}", status_code=303)


@app.post("/tender/{tender_id}/send-mail")
def tender_send_mail(
    tender_id: int,
    request: Request,
    to: str = Form(...),
    cc: str = Form(""),
    subject: str = Form(""),
    custom_message: str = Form(""),
    db: Session = Depends(get_db),
):
    """Versendet die Ausschreibung als Mail mit FBE-Branding-Layout."""
    user = _session_user(request)
    if not user:
        raise HTTPException(401, "Nicht angemeldet")
    tender = db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(404, "Ausschreibung nicht gefunden")

    def _split_emails(s: str) -> list[str]:
        # Akzeptiert komma- oder semikolon-getrennt, plus Whitespace.
        if not s:
            return []
        parts = re.split(r"[,;\s]+", s.strip())
        return [p for p in parts if "@" in p and "." in p.split("@")[-1]]

    to_list = _split_emails(to)
    cc_list = _split_emails(cc)
    if not to_list:
        return RedirectResponse(
            url="/tender/{}?error=Mindestens+eine+gueltige+Empfaenger-Adresse+noetig".format(tender_id),
            status_code=303,
        )

    from . import notify as notify_mod
    if not (settings.smtp_host and settings.smtp_from):
        return RedirectResponse(
            url="/tender/{}?error=SMTP+nicht+konfiguriert".format(tender_id),
            status_code=303,
        )

    subj = (subject or "").strip() or "Ausschreibung: {}".format(tender.title or "")

    # Reply-To setzt das HTML-Template auf company_email; eine User-Signatur
    # bauen wir aus username + company defaults.
    sig = "Mit freundlichen Grüßen\n{}".format(user["username"])

    ok, err = notify_mod.send_tender_mail(
        tender=tender,
        to=to_list,
        cc=cc_list,
        subject=subj,
        custom_message=custom_message or "",
        sender_signature=sig,
    )
    if ok:
        detail = "An: {}".format(", ".join(to_list))
        if cc_list:
            detail += "  ·  CC: {}".format(", ".join(cc_list))
        if subj:
            detail += "  ·  Betreff: {}".format(subj[:200])
        _log_event(db, tender_id, user, "mail_sent", detail)
        db.commit()
        msg = "Mail an {} versendet.".format(", ".join(to_list[:3]))
        return RedirectResponse(
            url="/tender/{}?flash=".format(tender_id) + quote(msg, safe=""),
            status_code=303,
        )
    err_msg = err or "Unbekannter Fehler - Logs pruefen"
    return RedirectResponse(
        url="/tender/{}?error=".format(tender_id) + quote(err_msg, safe=""),
        status_code=303,
    )


@app.post("/tender/{tender_id}/quick-status")
def quick_status(
    tender_id: int,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    tender = db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(404, "nicht gefunden")
    if status not in STATUS_VALUES:
        raise HTTPException(400, "Unbekannter Status")
    old = tender.status
    tender.status = status
    if old != status:
        _log_event(db, tender_id, _session_user(request), "status",
                   f"{old or 'neu'} → {status}")
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
SCRAPER_CHOICES = ["bund", "ted", "rss_generic", "generic_html", "crawl_html", "nextjs", "playwright_html"]


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
    ignore_robots: Optional[str] = Form(None),
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

    # ignore_robots-Checkbox aus dem Form-Feld in die portal-config einmischen
    # (statt im YAML-Block separat pflegen zu muessen).
    if not isinstance(cfg_data, dict):
        cfg_data = {}
    if ignore_robots == "on":
        cfg_data["ignore_robots"] = True
    else:
        cfg_data.pop("ignore_robots", None)

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
    diagnostic = None
    try:
        ScraperCls = _load_scraper(chosen)
        with ScraperCls(base_url=chosen.base_url, name=chosen.name, config=chosen.config) as sc:
            # Diagnose-Fetch der Listing-URL (zeigt Status, HTML-Groesse,
            # Container, Selector-Treffer im rohen HTML)
            diagnostic = _probe_diagnostic(sc, chosen)
            items = sc.fetch([term])
        result["count"] = len(items)
        result["items"] = items[:5]
    except Exception as exc:
        log.exception("Probe %s fehlgeschlagen: %s", chosen.name, exc)
        result["error"] = f"{type(exc).__name__}: {exc}"

    return templates.TemplateResponse(
        request, "probe.html",
        {"portals": load_portals(), "selected": portal,
         "result": result, "term": term,
         "diagnostic": diagnostic,
         "user": request.session.get("user")},
    )


@app.get("/admin/status", response_class=HTMLResponse)
def admin_status(request: Request):
    last_run = load_run_state()
    return templates.TemplateResponse(
        request, "status.html",
        {
            "configured_portals": enabled_portals(),
            "last_run": last_run,
            "summary": _portal_status_summary(last_run),
            "is_running": is_pipeline_running(),
            "user": _session_user(request),
        },
    )


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(
    request: Request,
    flash: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from . import db_backup
    counts = {
        "tenders": db.query(Tender).count(),
        "comments": db.query(Comment).count(),
        "profiles": db.query(SearchProfile).count(),
    }
    backups = db_backup.list_backups()
    db_path = db_backup._db_file()
    from . import notify as notify_mod
    health = {
        "is_running": is_pipeline_running(),
        "db_size_kb": (db_path.stat().st_size // 1024) if db_path else 0,
        "db_path": str(db_path) if db_path else "(non-SQLite)",
        "backups_count": len(backups),
        "last_backup": backups[0] if backups else None,
    }
    mail = {
        "configured": notify_mod.is_configured(),
        "to": settings.notify_email,
        "from": settings.smtp_from,
        "host": "{}:{}".format(settings.smtp_host, settings.smtp_port) if settings.smtp_host else "",
        "summary_time": "{:02d}:{:02d}".format(settings.summary_hour, settings.summary_minute),
    }
    return templates.TemplateResponse(
        request, "settings.html",
        {
            "user": _session_user(request),
            "counts": counts,
            "is_running": is_pipeline_running(),
            "health": health,
            "mail": mail,
            "backups": backups[:5],
            "flash": flash,
            "error": error,
        },
    )


@app.post("/admin/release-lock")
def admin_release_lock(request: Request):
    """Notfallfunktion: Pipeline-Lock zwangsweise freigeben."""
    user = _session_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Nur Admin")
    was_held = force_release_lock()
    msg = "Lock freigegeben." if was_held else "Kein aktiver Lock - nichts zu tun."
    return RedirectResponse(
        url="/admin/settings?flash=" + msg.replace(" ", "+"),
        status_code=303,
    )


@app.post("/admin/test-mail")
def admin_test_mail(request: Request):
    """Sendet eine kurze Test-Mail an NOTIFY_EMAIL um die SMTP-Konfig
    zu pruefen."""
    user = _session_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Nur Admin")
    from . import notify
    if not notify.is_configured():
        return RedirectResponse(
            url="/admin/settings?error=SMTP+nicht+konfiguriert+(NOTIFY_EMAIL+/+SMTP_HOST+leer)",
            status_code=303,
        )
    ok, err = notify.send_test_mail()
    if ok:
        msg = "Test-Mail an {} versendet.".format(settings.notify_email)
        return RedirectResponse(
            url="/admin/settings?flash=" + quote(msg, safe=""),
            status_code=303,
        )
    return RedirectResponse(
        url="/admin/settings?error=" + quote(err or "Unbekannter Fehler", safe=""),
        status_code=303,
    )


@app.get("/admin/broadcast", response_class=HTMLResponse)
def admin_broadcast_get(
    request: Request,
    flash: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    user = _session_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Nur Admin")
    recipients = (
        db.query(User)
        .filter(User.is_active == True)  # noqa: E712
        .filter(User.email.isnot(None))
        .filter(User.email != "")
        .order_by(User.username)
        .all()
    )
    mail_ready = bool(settings.smtp_host and settings.smtp_from)
    return templates.TemplateResponse(
        request, "broadcast.html",
        {
            "user": user,
            "recipients": recipients,
            "mail_ready": mail_ready,
            "flash": flash,
            "error": error,
        },
    )


@app.post("/admin/broadcast")
def admin_broadcast_post(
    request: Request,
    subject: str = Form(...),
    body: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _session_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Nur Admin")

    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject or not body:
        return RedirectResponse(
            url="/admin/broadcast?error=Betreff+und+Text+sind+Pflicht.",
            status_code=303,
        )
    if not (settings.smtp_host and settings.smtp_from):
        return RedirectResponse(
            url="/admin/broadcast?error=SMTP+nicht+konfiguriert.",
            status_code=303,
        )

    recipients = (
        db.query(User)
        .filter(User.is_active == True)  # noqa: E712
        .filter(User.email.isnot(None))
        .filter(User.email != "")
        .all()
    )
    addresses = [u.email.strip() for u in recipients if u.email and u.email.strip()]
    if not addresses:
        return RedirectResponse(
            url="/admin/broadcast?error=Keine+Empfaenger+mit+E-Mail+gefunden.",
            status_code=303,
        )

    from . import notify as notify_mod
    import html as _html

    # Body: Plain-Text-Zeilen in HTML. Aufzaehlungen (Zeile beginnt mit
    # "- ", "* " oder "• ") werden zu sauberen <ul>-Listen, alles andere
    # bleibt mit weicher Trennung im <p>-Block.
    def _body_to_html(text: str) -> str:
        blocks = []
        current_list: list[str] | None = None
        current_para: list[str] = []

        def flush_para():
            if current_para:
                blocks.append(
                    "<p style=\"margin:0 0 14px 0;font-size:15px;line-height:1.6;"
                    "color:#27272a;\">{}</p>".format("<br>".join(current_para))
                )
                current_para.clear()

        def flush_list():
            nonlocal current_list
            if current_list:
                items = "".join(
                    "<li style=\"margin:0 0 6px 0;\">{}</li>".format(i)
                    for i in current_list
                )
                blocks.append(
                    "<ul style=\"margin:0 0 16px 0;padding-left:20px;font-size:15px;"
                    "line-height:1.6;color:#27272a;\">{}</ul>".format(items)
                )
                current_list = None

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.lstrip()
            if stripped.startswith(("- ", "* ", "• ")):
                flush_para()
                if current_list is None:
                    current_list = []
                current_list.append(_html.escape(stripped[2:].lstrip()))
            elif not line.strip():
                flush_para()
                flush_list()
            else:
                flush_list()
                current_para.append(_html.escape(line))
        flush_para()
        flush_list()
        return "".join(blocks)

    company_name = _html.escape(settings.company_name or "FBE Ausschreibungsplattform")
    logo_url = settings.company_logo_url or ""
    web = settings.company_web or ""
    contact_email = settings.company_email or settings.smtp_from or ""

    logo_html = (
        '<img src="{}" alt="{}" style="height:36px;display:block;">'.format(
            _html.escape(logo_url), company_name)
        if logo_url else
        '<div style="font-weight:700;color:#3f5a30;font-size:18px;">{}</div>'.format(company_name)
    )

    footer_links = []
    if web:
        footer_links.append('<a href="{0}" style="color:#7eb064;text-decoration:none;">{0}</a>'.format(_html.escape(web)))
    if contact_email:
        footer_links.append('<a href="mailto:{0}" style="color:#7eb064;text-decoration:none;">{0}</a>'.format(_html.escape(contact_email)))
    footer_sep = " &nbsp;·&nbsp; ".join(footer_links)

    body_html = (
        '<!doctype html><html><body style="margin:0;padding:24px 12px;background:#f4f4f5;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Inter,system-ui,sans-serif;'
        'color:#27272a;">'
          '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
          'style="max-width:640px;margin:0 auto;background:#ffffff;border-collapse:collapse;'
          'border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.06);">'
            # Header
            '<tr><td style="padding:22px 28px;background:#ffffff;border-bottom:1px solid #ececef;">'
              '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;">'
                '<tr>'
                  '<td style="vertical-align:middle;">{logo}</td>'
                  '<td style="vertical-align:middle;text-align:right;font-size:11px;color:#a1a1aa;'
                  'text-transform:uppercase;letter-spacing:0.8px;">Systemnachricht</td>'
                '</tr>'
              '</table>'
            '</td></tr>'
            # Badge
            '<tr><td style="padding:24px 28px 0 28px;">'
              '<span style="display:inline-block;padding:4px 10px;background:#eaf3df;color:#3f5a30;'
              'border-radius:999px;font-size:11px;font-weight:600;letter-spacing:0.3px;text-transform:uppercase;">'
              'Plattform-Update</span>'
            '</td></tr>'
            # Subject as headline
            '<tr><td style="padding:10px 28px 0 28px;">'
              '<h1 style="margin:0;font-size:22px;font-weight:600;line-height:1.3;color:#1f2937;">'
              '{subject}</h1>'
            '</td></tr>'
            # Body
            '<tr><td style="padding:18px 28px 8px 28px;">{body_blocks}</td></tr>'
            # Disclaimer
            '<tr><td style="padding:6px 28px 22px 28px;">'
              '<div style="font-size:13px;color:#71717a;background:#fafafa;border:1px solid #ececef;'
              'border-radius:8px;padding:12px 14px;">'
              'Dies ist eine automatisch erzeugte Nachricht der {company} – bitte nicht darauf antworten. '
              'Bei Fragen wende dich an den Administrator der Plattform.'
              '</div>'
            '</td></tr>'
            # Footer
            '<tr><td style="padding:14px 28px 22px 28px;background:#fafafa;border-top:1px solid #ececef;'
            'text-align:center;color:#a1a1aa;font-size:12px;line-height:1.6;">'
              '<div style="color:#71717a;">{company}</div>'
              '{footer_links}'
            '</td></tr>'
          '</table>'
        '</body></html>'
    ).format(
        logo=logo_html,
        subject=_html.escape(subject),
        body_blocks=_body_to_html(body),
        company=company_name,
        footer_links=("<div style=\"margin-top:4px;\">{}</div>".format(footer_sep) if footer_sep else ""),
    )

    plain_lines = [
        f"[{settings.company_name or 'FBE Ausschreibungsplattform'}] Systemnachricht",
        "=" * 60,
        "",
        subject,
        "",
        body,
        "",
        "-" * 60,
        "Dies ist eine automatisch erzeugte Nachricht. Bitte nicht antworten.",
    ]
    plain = "\n".join(plain_lines)

    ok, err = notify_mod._send_to(
        to=[],  # Empfaenger in BCC, damit Adressen nicht gegenseitig sichtbar sind
        bcc=addresses,
        subject=f"[FBE] {subject}",
        plain_body=plain,
        html_body=body_html,
    )
    if not ok:
        return RedirectResponse(
            url=f"/admin/broadcast?error=Versand+fehlgeschlagen:+{quote(err or '-')}",
            status_code=303,
        )
    msg = f"Broadcast an {len(addresses)} Empfaenger versendet."
    return RedirectResponse(
        url=f"/admin/broadcast?flash={quote(msg)}",
        status_code=303,
    )


@app.post("/admin/send-summary")
def admin_send_summary(request: Request, days: int = Form(1)):
    """Sendet die Tageszusammenfassung sofort, unabhaengig vom Cron."""
    user = _session_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Nur Admin")
    from . import notify
    if not notify.is_configured():
        return RedirectResponse(
            url="/admin/settings?error=SMTP+nicht+konfiguriert",
            status_code=303,
        )
    days = max(1, min(int(days or 1), 30))
    ok = notify.send_daily_summary(days=days)
    if ok:
        msg = "Zusammenfassung der letzten {} Tage versendet.".format(days)
    else:
        msg = ("Keine neuen Treffer in den letzten {} Tagen oder Versand "
               "fehlgeschlagen.".format(days))
    return RedirectResponse(
        url="/admin/settings?flash=" + msg.replace(" ", "+"),
        status_code=303,
    )


@app.post("/admin/backup-now")
def admin_backup_now(request: Request):
    """Sofort ein DB-Backup erzeugen."""
    user = _session_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Nur Admin")
    from . import db_backup
    out = db_backup.maybe_backup(force=True)
    if out:
        msg = "Backup erstellt: {} ({} KB)".format(out.name, out.stat().st_size // 1024)
    else:
        msg = "Backup nicht erstellt - keine SQLite-DB konfiguriert?"
    return RedirectResponse(
        url="/admin/settings?flash=" + msg.replace(" ", "+"),
        status_code=303,
    )


@app.post("/admin/reset-tenders")
def admin_reset_tenders(
    request: Request,
    background_tasks: BackgroundTasks,
    confirm: str = Form(""),
    rescrape: str = Form(""),
    db: Session = Depends(get_db),
):
    """Loescht alle Tender + Kommentare + last-run-State. Optional sofort
    neu suchen."""
    user = _session_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Nur Admin")
    if confirm != "RESET":
        return RedirectResponse(
            url="/admin/settings?error=Reset+nicht+bestaetigt+(Feld+leer)",
            status_code=303,
        )

    # Auto-Backup vor dem zerstoerenden Reset.
    try:
        from . import db_backup
        backup = db_backup.maybe_backup(force=True)
        if backup:
            log.info("Pre-Reset-Backup angelegt: %s", backup.name)
    except Exception as exc:  # pragma: no cover
        log.warning("Pre-Reset-Backup fehlgeschlagen: %s", exc)

    deleted_comments = db.query(Comment).delete(synchronize_session=False)
    deleted_tenders = db.query(Tender).delete(synchronize_session=False)
    db.commit()

    # last_run.json loeschen, damit das Status-Panel sauber startet.
    try:
        from .run_state import _PATH as run_state_path
        if run_state_path.exists():
            run_state_path.unlink()
    except Exception as exc:  # pragma: no cover
        log.warning("last_run.json konnte nicht geloescht werden: %s", exc)

    msg = (
        "Reset: {} Tender + {} Kommentare geloescht."
        .format(deleted_tenders, deleted_comments)
    )

    if rescrape == "yes":
        # Immer queueen - run_pipeline_with_lock kuemmert sich um Doppellaeufe
        # via internem Lock. Nicht hier vorab gaten, sonst denkt der User
        # 'der Reset hat keine Suche getriggert' weil zufaellig schon ein
        # Auto-Run lief.
        background_tasks.add_task(run_pipeline_with_lock)
        msg += " Suchlauf gestartet."

    return RedirectResponse(
        url="/admin/settings?flash=" + msg.replace(" ", "+"),
        status_code=303,
    )


@app.post("/admin/run-search")
def admin_run_search(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Triggert sofort einen Suchlauf - separat von Reset, damit der User
    auch ohne Loeschen einen frischen Lauf anstossen kann."""
    user = _session_user(request)
    if not user:
        raise HTTPException(401, "Nicht angemeldet")
    background_tasks.add_task(run_pipeline_with_lock)
    return RedirectResponse(
        url="/admin/settings?flash=Suchlauf+gestartet+-+Status+aktualisiert+sich+in+ca.+30+Sekunden.",
        status_code=303,
    )


@app.get("/admin/probe-all", response_class=HTMLResponse)
def admin_probe_all_get(request: Request):
    return templates.TemplateResponse(
        request, "probe_all.html",
        {
            "portals": load_portals(),
            "results": None,
            "user": request.session.get("user"),
        },
    )


@app.post("/admin/probe-all", response_class=HTMLResponse)
def admin_probe_all_post(request: Request, term: str = Form("Tiefbau")):
    """Testet jedes konfigurierte Portal sequentiell mit demselben Suchbegriff."""
    results = []
    for portal in load_portals():
        if not portal.enabled:
            results.append({
                "name": portal.name, "scraper": portal.scraper,
                "enabled": False, "count": 0, "error": None,
                "elapsed_ms": 0, "skipped": True,
            })
            continue

        entry = {
            "name": portal.name, "scraper": portal.scraper,
            "enabled": True, "count": 0, "error": None,
            "elapsed_ms": 0, "skipped": False,
            "first_titles": [],
        }
        t0 = datetime.utcnow()
        try:
            ScraperCls = _load_scraper(portal)
            with ScraperCls(base_url=portal.base_url, name=portal.name, config=portal.config) as sc:
                items = sc.fetch([term])
            entry["count"] = len(items)
            entry["first_titles"] = [it.title[:80] for it in items[:3]]
        except Exception as exc:
            log.exception("Probe-All %s fehlgeschlagen: %s", portal.name, exc)
            entry["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        entry["elapsed_ms"] = int((datetime.utcnow() - t0).total_seconds() * 1000)
        results.append(entry)

    summary = {
        "total": len([r for r in results if not r.get("skipped")]),
        "ok": len([r for r in results if r["count"] > 0]),
        "empty": len([r for r in results if not r.get("skipped") and r["count"] == 0 and not r["error"]]),
        "error": len([r for r in results if r["error"]]),
    }
    return templates.TemplateResponse(
        request, "probe_all.html",
        {
            "portals": load_portals(),
            "results": results,
            "summary": summary,
            "term": term,
            "user": request.session.get("user"),
        },
    )


def _probe_diagnostic(scraper, portal_cfg) -> dict:
    """Holt rohe Listing-Antwort + analysiert Struktur fuer das UI."""
    cfg = portal_cfg.config or {}
    candidates: list[str] = []
    if cfg.get("listing_paths"):
        candidates.extend(cfg["listing_paths"])
    elif cfg.get("search_path"):
        candidates.append(cfg["search_path"].replace("{term}", "test"))

    if not candidates:
        return {"reason": "Keine listing_paths / search_path konfiguriert."}

    from urllib.parse import urljoin
    from bs4 import BeautifulSoup

    out = {"requests": []}
    for path in candidates[:2]:  # max 2 Requests
        url = urljoin(portal_cfg.base_url + "/", path.lstrip("/"))
        entry = {"url": url, "status": None, "final_url": None,
                 "html_length": 0, "container_found": False,
                 "container_children": 0, "result_selector_hits": 0,
                 "looks_like_spa": False, "html_excerpt": "",
                 "error": None}
        try:
            resp = scraper.get(url)
            entry["status"] = resp.status_code
            entry["final_url"] = str(resp.url)
            html = resp.text
            entry["html_length"] = len(html)
            entry["html_excerpt"] = html[:2500]
            soup = BeautifulSoup(html, "lxml")

            # Container-Heuristik
            for cid in ("results-list-container", "results", "tender-list", "search-results"):
                node = soup.find(id=cid)
                if node:
                    entry["container_found"] = True
                    entry["container_id"] = cid
                    entry["container_children"] = len([c for c in node.children if getattr(c, "name", None)])
                    break

            # Result-Selector pruefen
            sel = cfg.get("result_selector")
            if sel:
                try:
                    entry["result_selector_hits"] = len(soup.select(sel))
                except Exception as exc:
                    entry["result_selector_error"] = str(exc)

            # SPA-Heuristik: leerer Body, viele <script>, zentrale Mount-Points
            scripts = soup.find_all("script")
            mount_points = soup.find_all(id=lambda v: v in ("app", "root", "__next"))
            visible_text_len = len(soup.get_text(strip=True))
            entry["scripts"] = len(scripts)
            entry["visible_text_length"] = visible_text_len
            if (entry["html_length"] > 5000 and visible_text_len < 1500) or mount_points:
                entry["looks_like_spa"] = True

        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        out["requests"].append(entry)

    return out


# --- Export ---------------------------------------------------------
def _export_query(db, request, **filters):
    """Baut die Filter-Query fuer Exporte. Restricted-User: serverseitig
    auf zugewiesene Profile begrenzen, freie Filter aus der URL ignorieren."""
    if is_restricted(request):
        q = _filtered_query(db, status=filters.get("status"), include_expired=False)
        q, _, _ = _enforce_restricted(db, request, q,
                                      selected_profile_id=filters.get("profile"))
        return q
    return _filtered_query(
        db,
        portal=filters.get("portal"), region=filters.get("region"),
        status=filters.get("status"), level=filters.get("level"),
        deadline_from=filters.get("deadline_from"),
        deadline_to=filters.get("deadline_to"), q=filters.get("q"),
        score_min=_safe_float(filters.get("score_min")),
        score_max=_safe_float(filters.get("score_max")),
        quick=filters.get("quick"),
        include_expired=bool(filters.get("include_expired")),
    )


@app.get("/export/csv")
def export_csv(
    request: Request,
    db: Session = Depends(get_db),
    portal: Optional[str] = None,
    region: Optional[str] = None,
    status: Optional[str] = None,
    level: Optional[str] = None,
    deadline_from: Optional[str] = None,
    deadline_to: Optional[str] = None,
    q: Optional[str] = None,
    score_min: Optional[str] = None,
    score_max: Optional[str] = None,
    quick: Optional[str] = None,
    sort: Optional[str] = "score_desc",
    include_expired: Optional[str] = None,
    profile: Optional[int] = None,
):
    query = _export_query(
        db, request, portal=portal, region=region, status=status, level=level,
        deadline_from=deadline_from, deadline_to=deadline_to, q=q,
        score_min=score_min, score_max=score_max, quick=quick,
        include_expired=include_expired, profile=profile,
    )
    tenders = _apply_sort(query, sort).all()
    return Response(
        content=to_csv(tenders),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="ausschreibungen.csv"'},
    )


@app.get("/export/xlsx")
def export_xlsx(
    request: Request,
    db: Session = Depends(get_db),
    portal: Optional[str] = None,
    region: Optional[str] = None,
    status: Optional[str] = None,
    level: Optional[str] = None,
    deadline_from: Optional[str] = None,
    deadline_to: Optional[str] = None,
    q: Optional[str] = None,
    score_min: Optional[str] = None,
    score_max: Optional[str] = None,
    quick: Optional[str] = None,
    sort: Optional[str] = "score_desc",
    include_expired: Optional[str] = None,
    profile: Optional[int] = None,
):
    query = _export_query(
        db, request, portal=portal, region=region, status=status, level=level,
        deadline_from=deadline_from, deadline_to=deadline_to, q=q,
        score_min=score_min, score_max=score_max, quick=quick,
        include_expired=include_expired, profile=profile,
    )
    tenders = _apply_sort(query, sort).all()
    return Response(
        content=to_xlsx(tenders),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="ausschreibungen.xlsx"'},
    )


# --- Admin: User-Verwaltung ------------------------------------------
ROLES = ["admin", "viewer", "user"]


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(
    request: Request,
    db: Session = Depends(get_db),
    flash: Optional[str] = None,
    error: Optional[str] = None,
):
    users = db.query(User).order_by(User.username).all()
    return templates.TemplateResponse(
        request, "users.html",
        {
            "users": users,
            "user": request.session.get("user"),
            "current_user_id": request.session.get("user_id"),
            "flash": flash,
            "error": error,
        },
    )


@app.get("/admin/users/new", response_class=HTMLResponse)
def admin_user_new(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "user_edit.html",
        {
            "edited": None, "is_new": True, "roles": ROLES,
            "user": request.session.get("user"),
            "all_profiles": db.query(SearchProfile).order_by(SearchProfile.name).all(),
            "assigned_profile_ids": set(),
        },
    )


@app.get("/admin/users/{user_id}/edit", response_class=HTMLResponse)
def admin_user_edit(user_id: int, request: Request, db: Session = Depends(get_db)):
    edited = db.get(User, user_id)
    if not edited:
        return RedirectResponse(url="/admin/users?error=User nicht gefunden", status_code=303)
    return templates.TemplateResponse(
        request, "user_edit.html",
        {
            "edited": edited, "is_new": False, "roles": ROLES,
            "user": request.session.get("user"),
            "all_profiles": db.query(SearchProfile).order_by(SearchProfile.name).all(),
            "assigned_profile_ids": {p.id for p in edited.assigned_profiles},
        },
    )


def _apply_profile_assignments(db: Session, user: User, profile_ids_csv: str) -> int:
    """Setzt die SearchProfile-Zuweisungen eines Users aus einer CSV-Liste."""
    ids = [int(x) for x in profile_ids_csv.split(",") if x.strip().isdigit()]
    if ids:
        profs = db.query(SearchProfile).filter(SearchProfile.id.in_(ids)).all()
    else:
        profs = []
    user.assigned_profiles = profs
    return len(profs)


@app.post("/admin/users/save")
def admin_user_save(
    request: Request,
    user_id: str = Form(""),
    username: str = Form(...),
    email: str = Form(""),
    password: str = Form(""),
    role: str = Form("user"),
    is_active: Optional[str] = Form(None),
    send_invite: Optional[str] = Form(None),
    profile_ids: str = Form(""),
    profile_assign_present: str = Form(""),  # Sentinel: '1' wenn die Checkbox-UI im Form war
    db: Session = Depends(get_db),
):
    username = username.strip()
    if not username:
        return RedirectResponse(url="/admin/users?error=Username ist erforderlich.", status_code=303)
    if role not in ROLES:
        role = "user"

    pid = int(user_id) if user_id and user_id.isdigit() else None
    edited = db.get(User, pid) if pid else None

    if edited:
        # Update bestehender User
        if edited.username != username:
            # Username-Eindeutigkeit
            if db.query(User).filter(User.username == username, User.id != edited.id).first():
                return RedirectResponse(
                    url=f"/admin/users?error=Username '{username}' ist vergeben.",
                    status_code=303,
                )
            edited.username = username
        edited.role = role
        edited.is_active = is_active == "on"
        edited.email = email.strip() or None
        if password.strip():
            edited.password_hash = hash_password(password)
        # Nur dann Zuweisungen ueberschreiben, wenn der Block tatsaechlich im
        # Form vorhanden war. Verhindert versehentliches Leeren beim Save.
        if profile_assign_present == "1":
            n_profiles = _apply_profile_assignments(db, edited, profile_ids)
            msg = f"{username} aktualisiert ({n_profiles} Profile zugewiesen)."
        else:
            msg = f"{username} aktualisiert."
        db.commit()
        return RedirectResponse(url=f"/admin/users?flash={msg}", status_code=303)

    if db.query(User).filter(User.username == username).first():
        return RedirectResponse(url=f"/admin/users?error=Username '{username}' ist vergeben.", status_code=303)
    if not email.strip():
        return RedirectResponse(
            url="/admin/users?error=E-Mail ist erforderlich (User setzt Passwort selbst via Einladungs-Link).",
            status_code=303,
        )
    from backend.auth import generate_token as _gt, invite_expires_at as _exp
    new_user = User(
        username=username,
        email=email.strip(),
        password_hash="",
        role=role,
        is_active=False,
        invite_token=_gt(),
        invite_token_expires_at=_exp(),
    )
    db.add(new_user); db.flush()
    if profile_assign_present == "1":
        _apply_profile_assignments(db, new_user, profile_ids)
    db.commit()
    link = f"{_base_url(request)}/set-password?token={new_user.invite_token}"
    from backend import notify as notify_mod
    ok, err = notify_mod.send_invite_mail(new_user.email, new_user.username, link)
    if not ok:
        return RedirectResponse(
            url=f"/admin/users?flash={username} angelegt, Mailversand fehlgeschlagen: {err or '-'}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/users?flash={username} angelegt + Einladung verschickt.",
        status_code=303,
    )


@app.post("/admin/users/{user_id}/delete")
def admin_user_delete(user_id: int, request: Request, db: Session = Depends(get_db)):
    if request.session.get("user_id") == user_id:
        return RedirectResponse(
            url="/admin/users?error=Du kannst dich nicht selbst loeschen.",
            status_code=303,
        )
    edited = db.get(User, user_id)
    if not edited:
        return RedirectResponse(url="/admin/users?error=User nicht gefunden", status_code=303)
    name = edited.username
    db.delete(edited)
    db.commit()
    return RedirectResponse(url=f"/admin/users?flash={name} geloescht.", status_code=303)


# --- Suchprofile -----------------------------------------------------
# Verwaltungs-Routen liegen unter /admin/profiles/* und sind damit
# automatisch durch das AuthMiddleware-Admin-Gate geschuetzt.
@app.get("/admin/profiles", response_class=HTMLResponse)
def profiles_list(
    request: Request,
    db: Session = Depends(get_db),
    flash: Optional[str] = None,
    error: Optional[str] = None,
):
    items = db.query(SearchProfile).order_by(SearchProfile.name).all()
    # Pro Profil: Treffer-Count und User-Count vorberechnen, damit der Admin
    # auf einen Blick sieht, ob Keywords/Zuweisungen greifen.
    profile_info = []
    for p in items:
        try:
            match_count = db.query(Tender).filter(_profile_filter_expr(p)).count()
        except Exception:  # pragma: no cover - defensiv
            match_count = -1
        profile_info.append({
            "profile": p,
            "keywords": p.keyword_list(),
            "match_count": match_count,
            "user_count": len(p.assigned_users),
            "user_names": [u.username for u in p.assigned_users],
        })
    return templates.TemplateResponse(
        request, "profiles.html",
        {
            "profile_info": profile_info,
            "user": request.session.get("user"),
            "flash": flash,
            "error": error,
        },
    )


def _profile_edit_ctx(db: Session, profile: Optional[SearchProfile], is_new: bool, request: Request):
    return {
        "profile": profile,
        "is_new": is_new,
        "portals": [r[0] for r in db.query(Tender.portal).distinct().all() if r[0]],
        "regions": [r[0] for r in db.query(Tender.region).distinct().all() if r[0]],
        "statuses": STATUS_VALUES,
        "levels": LEVEL_VALUES,
        "all_users": db.query(User).order_by(User.username).all(),
        "assigned_user_ids": {u.id for u in (profile.assigned_users if profile else [])},
        "keywords_text": "\n".join(profile.keyword_list()) if profile else "",
        "user": request.session.get("user"),
    }


@app.get("/admin/profiles/new", response_class=HTMLResponse)
def profile_new(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request, "profile_edit.html",
        _profile_edit_ctx(db, None, is_new=True, request=request),
    )


@app.get("/admin/profiles/{profile_id}/edit", response_class=HTMLResponse)
def profile_edit(profile_id: int, request: Request, db: Session = Depends(get_db)):
    profile = db.get(SearchProfile, profile_id)
    if not profile:
        raise HTTPException(404, "Profil nicht gefunden")
    return templates.TemplateResponse(
        request, "profile_edit.html",
        _profile_edit_ctx(db, profile, is_new=False, request=request),
    )


def _to_int(s: str | None) -> int | None:
    if s is None or s == "":
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _parse_keywords_textarea(raw: str) -> list[str]:
    """Trennt Eingabe an Newline/Komma/Semikolon, dedupliziert, ohne Leerzeichen."""
    if not raw:
        return []
    parts = raw.replace(";", "\n").replace(",", "\n").splitlines()
    out, seen = [], set()
    for p in parts:
        p = p.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


@app.post("/admin/profiles/save")
def profile_save(
    request: Request,
    profile_id: str = Form(""),
    name: str = Form(...),
    description: str = Form(""),
    keywords: str = Form(""),
    query: str = Form(""),
    portal: str = Form(""),
    region: str = Form(""),
    status: str = Form(""),
    level: str = Form(""),
    score_min: str = Form(""),
    score_max: str = Form(""),
    deadline_days: str = Form(""),
    sort: str = Form("score_desc"),
    notify: Optional[str] = Form(None),
    user_ids: str = Form(""),
    user_assign_present: str = Form(""),  # Sentinel: '1' wenn die Checkbox-UI im Form war
    db: Session = Depends(get_db),
):
    name = name.strip()
    if not name:
        return RedirectResponse(url="/admin/profiles?error=Name ist erforderlich.", status_code=303)

    pid = _to_int(profile_id)
    profile = db.get(SearchProfile, pid) if pid else None
    if not profile:
        if db.query(SearchProfile).filter(SearchProfile.name == name).first():
            return RedirectResponse(
                url=f"/admin/profiles?error=Profil mit Name '{name}' existiert bereits.",
                status_code=303,
            )
        profile = SearchProfile(name=name)
        db.add(profile)

    profile.name = name
    profile.description = description.strip() or None
    kw_list = _parse_keywords_textarea(keywords)
    import json as _json
    profile.keywords = _json.dumps(kw_list, ensure_ascii=False) if kw_list else None
    profile.query = query.strip() or None
    profile.portal = portal.strip() or None
    profile.region = region.strip() or None
    profile.status = status.strip() or None
    profile.level = level.strip() or None
    profile.score_min = _to_int(score_min)
    profile.score_max = _to_int(score_max)
    profile.deadline_days = _to_int(deadline_days)
    profile.sort = sort.strip() or "score_desc"
    profile.notify = 1 if notify == "on" else 0

    # Neue Profile muessen erst ein commit/flush sehen, damit profile.id
    # existiert, bevor wir die M2M-Beziehung setzen. db.flush() reicht.
    db.flush()

    # User-Zuweisung nur uebernehmen, wenn die Checkbox-Sektion im Form war
    # (sonst wuerde das blosse Speichern der Profil-Daten alle Zuweisungen
    # ueberschreiben).
    if user_assign_present == "1":
        ids = [int(x) for x in user_ids.split(",") if x.strip().isdigit()]
        if ids:
            users = db.query(User).filter(User.id.in_(ids), User.role != "admin").all()
        else:
            users = []
        profile.assigned_users = users
        flash_msg = f"{name} gespeichert ({len(users)} User zugewiesen)."
    else:
        flash_msg = f"{name} gespeichert."

    db.commit()
    return RedirectResponse(url=f"/admin/profiles?flash={flash_msg}", status_code=303)


@app.get("/admin/profiles/{profile_id}/preview")
def profile_preview(profile_id: int, db: Session = Depends(get_db)):
    """Liefert JSON: wie viele Tender matched dieses Profil + erste 10 Titel.

    Zum Debuggen direkt im Profil-Edit-UI. Admin-only via Middleware.
    """
    profile = db.get(SearchProfile, profile_id)
    if not profile:
        return JSONResponse({"error": "Profil nicht gefunden"}, status_code=404)
    expr = _profile_filter_expr(profile)
    total = db.query(Tender).count()
    matches = (
        db.query(Tender)
        .filter(expr)
        .order_by(Tender.relevance_score.desc(), Tender.created_at.desc())
        .limit(10)
        .all()
    )
    count = db.query(Tender).filter(expr).count()
    return JSONResponse({
        "profile": profile.name,
        "keywords": profile.keyword_list(),
        "extra_query": profile.query,
        "portal_filter": profile.portal,
        "region_filter": profile.region,
        "score_min": profile.score_min,
        "score_max": profile.score_max,
        "match_count": count,
        "db_total": total,
        "examples": [
            {"id": t.id, "title": t.title, "portal": t.portal,
             "score": t.relevance_score, "region": t.region}
            for t in matches
        ],
    })


@app.post("/admin/profiles/{profile_id}/assign-users")
def profile_assign_users(
    profile_id: int,
    user_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    profile = db.get(SearchProfile, profile_id)
    if not profile:
        return RedirectResponse(url="/admin/profiles?error=Profil nicht gefunden", status_code=303)
    ids = [int(x) for x in user_ids.split(",") if x.strip().isdigit()]
    users = db.query(User).filter(User.id.in_(ids)).all() if ids else []
    profile.assigned_users = users
    db.commit()
    return RedirectResponse(url=f"/admin/profiles?flash={profile.name}: {len(users)} User zugewiesen.", status_code=303)


@app.post("/admin/profiles/{profile_id}/delete")
def profile_delete(profile_id: int, db: Session = Depends(get_db)):
    profile = db.get(SearchProfile, profile_id)
    if not profile:
        return RedirectResponse(url="/admin/profiles?error=Profil nicht gefunden", status_code=303)
    name = profile.name
    db.delete(profile)
    db.commit()
    return RedirectResponse(url=f"/admin/profiles?flash={name} geloescht.", status_code=303)


@app.get("/profiles/{profile_id}/apply")
def profile_apply(profile_id: int, request: Request, db: Session = Depends(get_db)):
    """Wendet ein Profil an. Restricted-User: nur zugewiesene Profile,
    Redirect auf /?profile=N (keine Keywords in der URL).
    Admin: Legacy-Verhalten - voller Query-String."""
    profile = db.get(SearchProfile, profile_id)
    if not profile:
        raise HTTPException(404, "Profil nicht gefunden")

    if is_restricted(request):
        assigned = _user_assigned_profiles(db, request)
        if profile not in assigned:
            raise HTTPException(404, "Profil nicht gefunden")
        return RedirectResponse(url=f"/?profile={profile.id}", status_code=303)

    qs = profile.to_query_string()
    return RedirectResponse(url=f"/?{qs}", status_code=303)


# --- JSON-API -------------------------------------------------------
@app.get("/api/tenders")
def api_list(
    request: Request,
    db: Session = Depends(get_db),
    portal: Optional[str] = None,
    region: Optional[str] = None,
    status: Optional[str] = None,
    level: Optional[str] = None,
    q: Optional[str] = None,
    profile: Optional[int] = None,
    limit: int = Query(200, le=2000),
):
    if is_restricted(request):
        query = _filtered_query(db, status=status, include_expired=False)
        query, _, _ = _enforce_restricted(db, request, query, selected_profile_id=profile)
    else:
        query = _filtered_query(db, portal=portal, region=region, status=status, level=level, q=q)
    tenders = query.order_by(Tender.relevance_score.desc()).limit(limit).all()
    return [t.to_dict() for t in tenders]


@app.get("/api/health", response_class=PlainTextResponse)
def health():
    return "ok"


NOTIFY_FREQ_VALUES = ("off", "daily", "weekly")


# --- KI ------------------------------------------------------------------

@app.get("/admin/ai-knowledge", response_class=HTMLResponse)
def admin_ai_knowledge_get(
    request: Request,
    flash: Optional[str] = None,
    error: Optional[str] = None,
):
    user = _session_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Nur Admin")
    from . import ai as ai_mod
    return templates.TemplateResponse(
        request, "ai_knowledge.html",
        {
            "user": user,
            "knowledge": ai_mod.read_knowledge(),
            "configured": ai_mod.is_configured(),
            "model": settings.openai_model,
            "flash": flash,
            "error": error,
        },
    )


@app.post("/admin/ai-knowledge")
def admin_ai_knowledge_post(
    request: Request,
    knowledge: str = Form(...),
):
    user = _session_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Nur Admin")
    from . import ai as ai_mod
    ai_mod.write_knowledge(knowledge)
    return RedirectResponse(url="/admin/ai-knowledge?flash=Gespeichert.", status_code=303)


@app.post("/tender/{tender_id}/ai-analyze")
def tender_ai_analyze(
    tender_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Triggert die KI-Analyse (Neu oder Re-Run). Speichert das Ergebnis
    in tender.ai_analysis und liefert es als JSON zurueck."""
    tender = db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(404, "Ausschreibung nicht gefunden")
    from . import ai as ai_mod
    if not ai_mod.is_configured():
        return JSONResponse({"error": "OPENAI_API_KEY nicht gesetzt"}, status_code=400)
    result = ai_mod.analyze_tender(tender)
    if "error" in result:
        return JSONResponse(result, status_code=502)
    tender.ai_analysis = json.dumps(result, ensure_ascii=False)
    tender.ai_analyzed_at = datetime.utcnow()
    db.commit()
    return JSONResponse({
        "analysis": result,
        "analyzed_at": tender.ai_analyzed_at.isoformat(),
    })


@app.post("/api/ai/chat")
def ai_chat(
    request: Request,
    payload: dict = Body(...),
):
    user = _session_user(request)
    if not user:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    from . import ai as ai_mod
    if not ai_mod.is_configured():
        return JSONResponse({"error": "OPENAI_API_KEY nicht gesetzt"}, status_code=400)
    msgs = payload.get("messages") or []
    if not isinstance(msgs, list) or not msgs:
        return JSONResponse({"error": "messages fehlt"}, status_code=400)
    try:
        text = ai_mod.chat(msgs)
    except Exception as exc:
        log.exception("AI-Chat fehlgeschlagen")
        return JSONResponse({"error": str(exc)[:200]}, status_code=502)
    return JSONResponse({"reply": text})


@app.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request):
    user = _session_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/assistant", status_code=303)
    from . import ai as ai_mod
    return templates.TemplateResponse(
        request, "assistant.html",
        {"user": user, "configured": ai_mod.is_configured(),
         "model": settings.openai_model},
    )


@app.get("/me/notifications", response_class=HTMLResponse)
def me_notifications_get(
    request: Request,
    flash: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    sess_user = _session_user(request)
    if not sess_user:
        return RedirectResponse(url="/login?next=/me/notifications", status_code=303)
    db_user = db.query(User).filter(User.username == sess_user["username"]).first()
    mail_ready = bool(settings.smtp_host and settings.smtp_from)
    return templates.TemplateResponse(
        request, "me_notifications.html",
        {
            "user": sess_user,
            "me": db_user,
            "mail_ready": mail_ready,
            "flash": flash,
            "error": error,
        },
    )


@app.post("/me/notifications")
def me_notifications_post(
    request: Request,
    frequency: str = Form("off"),
    min_score: int = Form(60),
    email: str = Form(""),
    db: Session = Depends(get_db),
):
    sess_user = _session_user(request)
    if not sess_user:
        raise HTTPException(401, "Nicht angemeldet")
    db_user = db.query(User).filter(User.username == sess_user["username"]).first()
    if not db_user:
        return RedirectResponse(url="/me/notifications?error=Konto+nicht+gefunden", status_code=303)
    if frequency not in NOTIFY_FREQ_VALUES:
        frequency = "off"
    min_score = max(0, min(int(min_score or 0), 100))
    email = (email or "").strip()
    if frequency != "off" and not email:
        return RedirectResponse(
            url="/me/notifications?error=Fuer+Benachrichtigungen+ist+eine+E-Mail+erforderlich",
            status_code=303,
        )
    db_user.notify_frequency = frequency
    db_user.notify_min_score = min_score
    if email:
        db_user.email = email
    db.commit()
    return RedirectResponse(url="/me/notifications?flash=Einstellungen+gespeichert.", status_code=303)


@app.post("/me/notifications/test")
def me_notifications_test(
    request: Request,
    db: Session = Depends(get_db),
):
    sess_user = _session_user(request)
    if not sess_user:
        raise HTTPException(401, "Nicht angemeldet")
    db_user = db.query(User).filter(User.username == sess_user["username"]).first()
    if not db_user or not db_user.email:
        return RedirectResponse(
            url="/me/notifications?error=Keine+E-Mail+hinterlegt",
            status_code=303,
        )
    if not (settings.smtp_host and settings.smtp_from):
        return RedirectResponse(
            url="/me/notifications?error=SMTP+nicht+konfiguriert",
            status_code=303,
        )
    from . import notify as notify_mod
    days = 7 if (db_user.notify_frequency == "weekly") else 1
    ok, err, count = notify_mod.send_user_digest(
        to_email=db_user.email,
        username=db_user.username,
        days=days,
        min_score=db_user.notify_min_score or 60,
    )
    if not ok:
        return RedirectResponse(
            url=f"/me/notifications?error=Versand+fehlgeschlagen:+{quote(err or '-')}",
            status_code=303,
        )
    if count == 0:
        msg = (f"Keine Treffer mit Score >= {db_user.notify_min_score} in den letzten "
               f"{days} Tag(en) - es waere also keine Mail rausgegangen.")
    else:
        msg = f"Test-Mail an {db_user.email} versendet ({count} Treffer)."
    return RedirectResponse(
        url=f"/me/notifications?flash={quote(msg)}",
        status_code=303,
    )


@app.get("/hilfe", response_class=HTMLResponse)
def hilfe(request: Request):
    return templates.TemplateResponse(request, "hilfe.html", {"user": _session_user(request)})



# --- Internal-API fuer den Enricher-Container -------------------------
# Auth: X-Internal-Token via Middleware. Kein Session-Login noetig.

@app.get("/api/internal/tenders-to-enrich")
def internal_tenders_to_enrich(
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Liefert eine Liste von Tendern, die noch nicht angereichert wurden.

    Kriterium: tender.ai_analysis ist NULL/leer. Sortiert nach Score
    absteigend, damit der Enricher zuerst die relevanten Treffer angeht.
    """
    from sqlalchemy import or_ as _or, func as _func
    items = (
        db.query(Tender)
        .filter(_or(Tender.ai_analysis.is_(None), _func.length(Tender.ai_analysis) == 0))
        .filter(Tender.url.isnot(None))
        .order_by(Tender.relevance_score.desc(), Tender.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {"id": t.id, "url": t.url, "title": t.title,
         "portal": t.portal, "score": t.relevance_score}
        for t in items
    ]


@app.post("/api/internal/tenders/{tender_id}/enriched")
def internal_tender_enriched(
    tender_id: int,
    payload: dict,
    db: Session = Depends(get_db),
):
    """Speichert das Anreicherungs-Ergebnis des Enrichers.

    Erwartet JSON-Body:
      { "markdown": "<voller Markdown-Text>",
        "summary":  "<optional, Kurztext fuer ai_analysis-Spalte>",
        "ok": true | false,
        "error": "<optional Fehlertext>" }

    Wenn ok=false, wird nur ai_analysis = 'enrich-error: <error>' gesetzt,
    damit das gleiche Tender nicht endlos im Poll-Backlog auftaucht.
    """
    t = db.get(Tender, tender_id)
    if not t:
        return JSONResponse({"error": "tender not found"}, status_code=404)

    ok = bool(payload.get("ok", True))
    markdown = (payload.get("markdown") or "").strip()
    summary = (payload.get("summary") or "").strip()
    error = (payload.get("error") or "").strip()

    if not ok:
        t.ai_analysis = f"enrich-error: {error[:300] or 'unknown'}"
        t.ai_analyzed_at = datetime.utcnow()
        db.commit()
        return {"ok": False, "stored": "error-marker"}

    if summary:
        t.ai_analysis = summary[:8000]
    elif markdown:
        # Wenn kein dezidierter summary mitkommt, nimm die ersten 8000 Zeichen
        # des Markdown - so dass das alte AI-Analysis-Feld eine Vorschau hat.
        t.ai_analysis = markdown[:8000]
    t.ai_analyzed_at = datetime.utcnow()

    # Markdown ins Shared-Volume schreiben (Datei pro Tender).
    written_path = None
    if markdown:
        try:
            from pathlib import Path as _Path
            d = _Path(settings.enrich_dir)
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"tender-{tender_id}.md"
            f.write_text(markdown, encoding="utf-8")
            written_path = str(f)
        except Exception as exc:  # pragma: no cover
            log.warning("Konnte Enrich-Markdown nicht schreiben: %s", exc)

    db.commit()
    return {"ok": True, "stored": "db+file" if written_path else "db", "path": written_path}


# --- Admin: KI-Anreicherungs-Dateien browsen + bearbeiten -------------
def _enrich_path_for(tender_id: int):
    """Liefert (Path, exists). Verhindert Path-Traversal durch int-only ID."""
    from pathlib import Path as _Path
    return _Path(settings.enrich_dir) / f"tender-{int(tender_id)}.md"


@app.get("/admin/enrichment", response_class=HTMLResponse)
def admin_enrichment_list(
    request: Request,
    db: Session = Depends(get_db),
    flash: Optional[str] = None,
    error: Optional[str] = None,
):
    """Listet alle .md-Dateien im Enrich-Verzeichnis + verknuepft sie mit
    dem Tender. Admin-only (Middleware)."""
    from pathlib import Path as _Path
    base = _Path(settings.enrich_dir)
    items = []
    if base.is_dir():
        for f in sorted(base.glob("tender-*.md")):
            try:
                tid = int(f.stem.split("-", 1)[1])
            except (ValueError, IndexError):
                continue
            t = db.get(Tender, tid)
            st = f.stat()
            items.append({
                "tender_id": tid,
                "title": t.title if t else "(geloescht)",
                "url": t.url if t else None,
                "portal": t.portal if t else None,
                "size_kb": round(st.st_size / 1024, 1),
                "modified": datetime.utcfromtimestamp(st.st_mtime),
            })
    # Sortierung: zuletzt geschrieben zuerst
    items.sort(key=lambda x: x["modified"], reverse=True)
    return templates.TemplateResponse(
        request, "enrichment_list.html",
        {
            "items": items,
            "enrich_dir": settings.enrich_dir,
            "user": request.session.get("user"),
            "flash": flash, "error": error,
        },
    )


@app.get("/admin/enrichment/{tender_id}", response_class=HTMLResponse)
def admin_enrichment_edit(
    tender_id: int,
    request: Request,
    db: Session = Depends(get_db),
    flash: Optional[str] = None,
    error: Optional[str] = None,
):
    p = _enrich_path_for(tender_id)
    if not p.is_file():
        return RedirectResponse(
            url="/admin/enrichment?error=Datei nicht gefunden", status_code=303)
    t = db.get(Tender, tender_id)
    return templates.TemplateResponse(
        request, "enrichment_edit.html",
        {
            "tender_id": tender_id,
            "tender": t,
            "content": p.read_text(encoding="utf-8", errors="replace"),
            "path": str(p),
            "user": request.session.get("user"),
            "flash": flash, "error": error,
        },
    )


@app.post("/admin/enrichment/{tender_id}/save")
def admin_enrichment_save(
    tender_id: int,
    content: str = Form(...),
    db: Session = Depends(get_db),
):
    p = _enrich_path_for(tender_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        # Plus: die ai_analysis-Spalte in der DB synchron halten (Vorschau).
        t = db.get(Tender, tender_id)
        if t:
            t.ai_analysis = content[:8000]
            t.ai_analyzed_at = datetime.utcnow()
            db.commit()
    except Exception as exc:  # pragma: no cover
        return RedirectResponse(
            url=f"/admin/enrichment/{tender_id}?error=Speichern fehlgeschlagen: {exc}",
            status_code=303)
    return RedirectResponse(
        url=f"/admin/enrichment/{tender_id}?flash=Gespeichert.", status_code=303)


@app.post("/admin/enrichment/{tender_id}/delete")
def admin_enrichment_delete(tender_id: int):
    p = _enrich_path_for(tender_id)
    if p.is_file():
        try:
            p.unlink()
        except Exception:  # pragma: no cover
            pass
    return RedirectResponse(url="/admin/enrichment?flash=Geloescht.", status_code=303)


@app.post("/admin/enrichment/{tender_id}/regenerate")
def admin_enrichment_regenerate(tender_id: int, db: Session = Depends(get_db)):
    """Markiert das Tender so, dass der Enricher es beim naechsten Poll neu
    bearbeitet. Setzt einfach ai_analysis zurueck."""
    t = db.get(Tender, tender_id)
    if not t:
        return RedirectResponse(url="/admin/enrichment?error=Tender nicht gefunden", status_code=303)
    t.ai_analysis = None
    t.ai_analyzed_at = None
    db.commit()
    return RedirectResponse(
        url="/admin/enrichment?flash=Tender %s wird beim naechsten Enricher-Poll neu bearbeitet." % tender_id,
        status_code=303)


# --- Notes-Editor (Obsidian-artige MD-Verwaltung) -------------------
from . import notes as _notes


@app.get("/admin/notes", response_class=HTMLResponse)
def admin_notes(
    request: Request,
    root: Optional[str] = None,
    file: Optional[str] = None,
    flash: Optional[str] = None,
    error: Optional[str] = None,
):
    roots = _notes.get_roots()
    root_key = root if root in roots else "knowledge"
    files_per_root = {k: _notes.list_files(k) for k in roots}
    current_content = ""
    current_name = ""
    current_html = ""
    if file:
        content = _notes.read_file(root_key, file)
        if content is not None:
            current_content = content
            current_name = file
            current_html = _notes.render_markdown(content)
    return templates.TemplateResponse(
        request, "notes.html",
        {
            "roots": roots,
            "root_key": root_key,
            "files_per_root": files_per_root,
            "current_name": current_name,
            "current_content": current_content,
            "current_html": current_html,
            "user": request.session.get("user"),
            "flash": flash, "error": error,
        },
    )


@app.post("/admin/notes/save")
def admin_notes_save(
    root: str = Form(...),
    name: str = Form(...),
    content: str = Form(...),
):
    name = name.strip()
    if not name.endswith(".md"):
        name = name + ".md"
    ok = _notes.write_file(root, name, content)
    if not ok:
        return RedirectResponse(
            url=f"/admin/notes?root={root}&error=Speichern fehlgeschlagen (ungueltiger Dateiname/Pfad?)",
            status_code=303)
    return RedirectResponse(
        url=f"/admin/notes?root={root}&file={name}&flash=Gespeichert.",
        status_code=303)


@app.post("/admin/notes/new")
def admin_notes_new(
    root: str = Form(...),
    name: str = Form(...),
):
    name = name.strip()
    if not name:
        return RedirectResponse(url=f"/admin/notes?root={root}&error=Name fehlt", status_code=303)
    if not name.endswith(".md"):
        name = name + ".md"
    # Default-Inhalt mit Frontmatter-Stub
    default = f"---\ntitle: {name[:-3]}\ntags: []\n---\n\n# {name[:-3]}\n\n"
    if _notes.read_file(root, name) is not None:
        return RedirectResponse(
            url=f"/admin/notes?root={root}&file={name}&error=Existiert bereits",
            status_code=303)
    if not _notes.write_file(root, name, default):
        return RedirectResponse(
            url=f"/admin/notes?root={root}&error=Anlegen fehlgeschlagen", status_code=303)
    return RedirectResponse(
        url=f"/admin/notes?root={root}&file={name}&flash=Angelegt.", status_code=303)


@app.post("/admin/notes/delete")
def admin_notes_delete(root: str = Form(...), name: str = Form(...)):
    _notes.delete_file(root, name)
    return RedirectResponse(url=f"/admin/notes?root={root}&flash=Geloescht.", status_code=303)


@app.get("/admin/notes/search")
def admin_notes_search(q: str = Query("", max_length=200)):
    return _notes.search(q)


# --- Reset Error-Marker (kein sqlite3-CLI noetig) ---------------------
@app.post("/admin/enrichment/reset-errors")
def admin_enrichment_reset_errors(db: Session = Depends(get_db)):
    from sqlalchemy import text as _text
    n = db.execute(
        _text("UPDATE tenders SET ai_analysis = NULL, ai_analyzed_at = NULL "
              "WHERE ai_analysis LIKE 'enrich-error:%'")
    ).rowcount or 0
    db.commit()
    return RedirectResponse(
        url=f"/admin/enrichment?flash={n} Error-Marker zurueckgesetzt - werden beim naechsten Poll neu probiert.",
        status_code=303)


# --- Wissensbasis fuer den Enricher (vom enricher abgerufen) ---------
@app.get("/api/internal/knowledge")
def internal_knowledge():
    """Liefert alle Markdown-Dateien aus knowledge_dir als JSON-Liste.

    Vom Enricher genutzt, um den LLM-System-Prompt mit Kontext anzureichern.
    """
    out = []
    from pathlib import Path as _Path
    d = _Path(settings.knowledge_dir)
    if d.is_dir():
        for f in sorted(d.glob("*.md")):
            try:
                out.append({"name": f.name, "content": f.read_text(encoding="utf-8", errors="replace")})
            except OSError:
                continue
    return out
