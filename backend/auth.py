"""Einfache, sessionbasierte Authentifizierung.

Single-Admin-Modell: Username + Passwort kommen aus .env
(`ADMIN_USERNAME`, `ADMIN_PASSWORD`). Session-Cookie via
Starlette `SessionMiddleware`. Wer nicht eingeloggt ist, wird auf
`/login` umgeleitet.
"""
from __future__ import annotations

import secrets
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .config import settings


PUBLIC_PATHS = {"/login", "/logout", "/api/health"}
PUBLIC_PREFIXES = ("/static",)


def verify_credentials(username: str, password: str) -> bool:
    user_ok = secrets.compare_digest(username or "", settings.admin_username)
    pass_ok = secrets.compare_digest(password or "", settings.admin_password)
    return user_ok and pass_ok


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("user"))


class AuthMiddleware(BaseHTTPMiddleware):
    """Schuetzt alle Routen ausser den oeffentlichen via Redirect."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable],
    ):
        path = request.url.path
        is_public = path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)
        if not is_public and not is_authenticated(request):
            # Bei API-Aufrufen 401, sonst Redirect zur Login-Seite.
            if path.startswith("/api/"):
                from fastapi.responses import JSONResponse
                return JSONResponse({"error": "unauthenticated"}, status_code=401)
            return RedirectResponse(url=f"/login?next={path}", status_code=303)
        return await call_next(request)


def install_auth(app) -> None:
    """SessionMiddleware + AuthMiddleware in der richtigen Reihenfolge."""
    secret = settings.session_secret or secrets.token_urlsafe(32)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="fbe_session",
        max_age=60 * 60 * 24 * 14,  # 14 Tage
        same_site="lax",
        https_only=False,  # auf True setzen, sobald HTTPS aktiv
    )
