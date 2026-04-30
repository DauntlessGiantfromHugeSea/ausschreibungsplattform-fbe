"""Branding-Assets: Logo + Favicon laden und cachen.

Das FBE-Logo wird beim Start der App einmal von fb-eng.de heruntergeladen
und unter backend/static/ abgelegt, damit der Browser es spaeter
ohne externe Anfrage ueber /static/fbe-logo.png ausliefern kann.

Wenn der Download fehlschlaegt (kein Internet, robots-Block, 5xx), bleibt
die App lauffaehig - Templates fallen dann auf das Text-Badge zurueck.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from .config import PROJECT_ROOT


log = logging.getLogger(__name__)

LOGO_URL = "https://fb-eng.de/wp-content/uploads/2024/10/FBE_green.png"
STATIC_DIR = PROJECT_ROOT / "backend" / "static"
LOGO_FILE = STATIC_DIR / "fbe-logo.png"
# Wir nutzen das gleiche PNG als Favicon. Browser akzeptieren PNG seit
# Jahren - eine separate ico-Datei ist nicht mehr noetig.
FAVICON_FILE = STATIC_DIR / "favicon.png"


def ensure_logo() -> bool:
    """Laedt das Logo bei Bedarf herunter. Liefert True wenn Datei vorhanden."""
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    if LOGO_FILE.exists() and LOGO_FILE.stat().st_size > 100:
        # Favicon-Spiegel auf jeden Fall sicherstellen.
        if not FAVICON_FILE.exists():
            try:
                FAVICON_FILE.write_bytes(LOGO_FILE.read_bytes())
            except OSError as exc:  # pragma: no cover
                log.warning("Konnte Favicon nicht spiegeln: %s", exc)
        return True

    try:
        with httpx.Client(timeout=15, follow_redirects=True,
                          headers={"User-Agent":
                                   "Mozilla/5.0 (compatible; FBE-Branding/1.0)"}) as client:
            resp = client.get(LOGO_URL)
        if resp.status_code != 200 or not resp.content:
            log.warning("Logo-Download fehlgeschlagen: HTTP %s", resp.status_code)
            return False
        LOGO_FILE.write_bytes(resp.content)
        FAVICON_FILE.write_bytes(resp.content)
        log.info("Logo heruntergeladen: %d Bytes -> %s", len(resp.content), LOGO_FILE)
        return True
    except Exception as exc:  # pragma: no cover - Netzwerk
        log.warning("Logo-Download fehlgeschlagen: %s", exc)
        return False


def has_logo() -> bool:
    return LOGO_FILE.exists() and LOGO_FILE.stat().st_size > 100
