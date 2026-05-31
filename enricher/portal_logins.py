"""Portal-Logins fuer den Enricher.

Konfiguration via ENV-Variable PORTAL_LOGINS_JSON (oder Datei
PORTAL_LOGINS_FILE). Beispiel-JSON:

  {
    "evergabe.de": {
      "login_url": "https://www.evergabe.de/login",
      "username_selector": "input[name='user[email]']",
      "password_selector": "input[name='user[password]']",
      "submit_selector": "button[type='submit']",
      "username": "max@example.com",
      "password": "geheim",
      "success_selector": "a[href*='logout']"
    }
  }

Jeder Host-Schluessel kann als Suffix in tender.url-Hostnamen vorkommen
(z.B. 'evergabe.de' matcht 'www.evergabe.de').

Strategie:
- Ein Browser-Context pro Lauf, gemeinsame Storage (Cookies/Session).
- Beim ersten Tender pro Domain wird login() einmalig ausgefuehrt.
- Erfolg wird ueber success_selector geprueft (optional).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse


log = logging.getLogger("enricher.login")


def load_config() -> dict[str, dict]:
    """Liest PORTAL_LOGINS_JSON oder die Datei. Beides optional."""
    raw = os.environ.get("PORTAL_LOGINS_JSON", "").strip()
    if not raw:
        path = os.environ.get("PORTAL_LOGINS_FILE", "").strip()
        if path and Path(path).is_file():
            try:
                raw = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("PORTAL_LOGINS_FILE konnte nicht gelesen werden: %s", exc)
                return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            log.warning("PORTAL_LOGINS_JSON ist kein Objekt, ignoriere.")
            return {}
        return data
    except json.JSONDecodeError as exc:
        log.warning("PORTAL_LOGINS_JSON ungueltig: %s", exc)
        return {}


def find_config_for(url: str, cfg: dict[str, dict]) -> tuple[str | None, dict | None]:
    """Findet die passende Login-Konfig fuer eine URL (Host-Suffix-Match)."""
    if not cfg:
        return None, None
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return None, None
    for key, conf in cfg.items():
        if not key or not isinstance(conf, dict):
            continue
        if host == key or host.endswith("." + key):
            return key, conf
    return None, None


def perform_login(page, conf: dict, timeout_ms: int = 20000) -> bool:
    """Fuehrt den Login-Flow aus. Liefert True bei (vermutlichem) Erfolg.

    Erwartet im conf:
      - login_url (required)
      - username_selector, password_selector, submit_selector (required)
      - username, password (required)
      - success_selector (optional - wird nach Submit erwartet)
    """
    req = ("login_url", "username_selector", "password_selector",
           "submit_selector", "username", "password")
    for k in req:
        if not conf.get(k):
            log.warning("Login-Config unvollstaendig - %s fehlt.", k)
            return False
    try:
        page.goto(conf["login_url"], wait_until="networkidle", timeout=timeout_ms)
    except Exception as exc:
        log.warning("Login-Page konnte nicht geladen werden: %s", exc)
        return False
    # Cookie-Banner best-effort wegklicken
    try:
        page.evaluate("""
            const sel = '[id*=cookie i], [class*=cookie i], [id*=consent i], [class*=consent i]';
            document.querySelectorAll(sel).forEach(e => e.remove());
        """)
    except Exception:
        pass
    try:
        page.fill(conf["username_selector"], conf["username"], timeout=5000)
        page.fill(conf["password_selector"], conf["password"], timeout=5000)
        page.click(conf["submit_selector"], timeout=5000)
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass
    except Exception as exc:
        log.warning("Login-Eingabe fehlgeschlagen: %s", exc)
        return False
    succ = conf.get("success_selector")
    if succ:
        try:
            page.wait_for_selector(succ, timeout=5000)
            return True
        except Exception:
            log.warning("Login-Erfolgs-Selektor '%s' nicht gefunden - Login-Flow gescheitert?", succ)
            return False
    return True
