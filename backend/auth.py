"""Sessionbasierte Authentifizierung mit DB-Usern + Env-Fallback.

Strategie:
  1. Lookup in der users-Tabelle (mit bcrypt).
  2. Wenn kein DB-User passt: Fallback auf ADMIN_USERNAME/ADMIN_PASSWORD
     aus der .env. Das gilt vor allem fuer den Erst-Login direkt nach
     der Installation - sobald ein Admin-User in der DB existiert,
     sollte das Env-Passwort weggenommen oder geaendert werden.

Rollen:
  admin   - alles (Portale, Suchbegriffe, User-Verwaltung, freie Suche)
  user    - sieht ausschliesslich Tender, die zu einem der ihm
            zugewiesenen SearchProfiles passen. Keine freie Suche,
            keine Keywords sichtbar.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Awaitable, Callable

import bcrypt
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .database import SessionLocal


log = logging.getLogger(__name__)

PUBLIC_PATHS = {"/login", "/forgot-password", "/set-password", "/reset-password", "/logout", "/api/health"}
PUBLIC_PREFIXES = ("/static", "/.well-known")
ADMIN_PREFIXES = ("/admin/",)


# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
def authenticate(username: str, password: str) -> dict | None:
    """Liefert dict mit username/role/id oder None."""
    from .models import User

    if not username or not password:
        return None

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if user and user.is_active and verify_password(password, user.password_hash):
            user.last_login_at = datetime.utcnow()
            db.commit()
            return {"username": user.username, "role": user.role, "id": user.id}
    finally:
        db.close()

    # Env-Fallback: nur wenn DB keinen Eintrag hat oder noch keine User
    # angelegt sind. Erlaubt First-Boot ohne DB-User.
    if (settings.admin_username
            and settings.admin_password
            and secrets.compare_digest(username, settings.admin_username)
            and secrets.compare_digest(password, settings.admin_password)):
        return {"username": username, "role": "admin", "id": 0}

    return None


def has_any_user() -> bool:
    """True wenn die users-Tabelle mindestens einen aktiven User hat."""
    from .models import User
    db = SessionLocal()
    try:
        return db.query(User.id).filter(User.is_active == True).first() is not None  # noqa: E712
    finally:
        db.close()


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("user"))


def current_role(request: Request) -> str:
    return request.session.get("role") or "user"


def require_admin(request: Request) -> bool:
    return current_role(request) == "admin"


def is_restricted(request: Request) -> bool:
    """True fuer eingeloggte Non-Admins (Rolle 'user')."""
    return is_authenticated(request) and current_role(request) != "admin"


# ---------------------------------------------------------------------------
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable],
    ):
        path = request.url.path
        is_public = path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)

        if not is_public and not is_authenticated(request):
            if path.startswith("/api/"):
                return JSONResponse({"error": "unauthenticated"}, status_code=401)
            return RedirectResponse(url=f"/login?next={path}", status_code=303)

        # Admin-Bereich: viewer wird auf Dashboard umgeleitet
        if any(path.startswith(p) for p in ADMIN_PREFIXES) and is_authenticated(request):
            if not require_admin(request):
                return RedirectResponse(
                    url="/?error=Nur Admins haben Zugriff auf diesen Bereich.",
                    status_code=303,
                )

        return await call_next(request)


def install_auth(app) -> None:
    secret = settings.session_secret or secrets.token_urlsafe(32)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="fbe_session",
        max_age=60 * 60 * 24 * 14,
        same_site="lax",
        https_only=settings.session_https_only,
    )


# Token-Helpers fuer Invite + Password-Reset
import secrets
from datetime import datetime, timedelta

def generate_token() -> str:
    return secrets.token_urlsafe(32)

def invite_expires_at():
    return datetime.utcnow() + timedelta(hours=24)

def reset_expires_at():
    return datetime.utcnow() + timedelta(hours=1)
