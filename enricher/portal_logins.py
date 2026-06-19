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
- Storage-State (Cookies + localStorage) wird pro Host persistent
  auf Disk gespeichert. Jeder neue browser-Context laed ihn rein.
- perform_login() wird nur ausgefuehrt, wenn der Storage-State leer
  ist oder der success_selector beim Probe nicht greift.
- Damit ueberlebt der Login einzelne Crawls + Container-Restarts.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse


log = logging.getLogger("enricher.login")


SESSION_DIR = Path(os.environ.get("SESSION_DIR", "/data/enrich/sessions"))


def session_file_for(host: str) -> Path:
    """Pfad zur storage_state-Datei fuer einen Host. Slug aus Host."""
    slug = re.sub(r"[^a-z0-9._-]+", "_", (host or "").lower()).strip("_") or "default"
    return SESSION_DIR / f"{slug}.json"


def has_saved_session(host: str) -> bool:
    p = session_file_for(host)
    return p.exists() and p.stat().st_size > 50


def save_session(ctx, host: str) -> bool:
    """Speichert den aktuellen Browser-Context-State (Cookies + storage) als JSON."""
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        path = session_file_for(host)
        ctx.storage_state(path=str(path))
        return True
    except Exception as exc:  # pragma: no cover
        log.warning("save_session(%s) fehlgeschlagen: %s", host, exc)
        return False


def saved_state_path(host: str) -> str | None:
    """Liefert den Pfad als String, wenn vorhanden - sonst None."""
    p = session_file_for(host)
    return str(p) if p.exists() and p.stat().st_size > 50 else None


def clear_session(host: str) -> None:
    p = session_file_for(host)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


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


def is_session_alive(page, conf: dict, timeout_ms: int = 10000) -> bool:
    """Prueft per Goto auf success_selector ohne Login. True = wir sind
    noch eingeloggt, False = Re-Login noetig."""
    succ = conf.get("success_selector")
    if not succ:
        # Ohne success_selector koennen wir nicht zuverlaessig pruefen -
        # wir behaupten: alive (Login ueberspringen).
        return True
    try:
        page.goto(conf["login_url"], wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception:
        return False
    # Wenn der Logout-Link (oder welcher Selektor auch immer) schon hier
    # erscheint, sind wir eingeloggt geblieben.
    try:
        page.wait_for_selector(succ, timeout=3000)
        return True
    except Exception:
        return False


def perform_login(page, conf: dict, timeout_ms: int = 20000) -> tuple[bool, str]:
    """Fuehrt den Login-Flow aus. Liefert (ok, reason).

    reason ist ein Klartext-Fehlergrund bei Misserfolg, oder bei Erfolg
    eine kurze Info (z.B. die Zielseite nach Login).
    """
    req = ("login_url", "username_selector", "password_selector",
           "submit_selector", "username", "password")
    for k in req:
        if not conf.get(k):
            return False, f"Config unvollstaendig: '{k}' fehlt"

    try:
        page.goto(conf["login_url"], wait_until="networkidle", timeout=timeout_ms)
    except Exception as exc:
        return False, f"login_url '{conf['login_url']}' nicht ladbar: {exc}"

    # Cookie-Banner best-effort wegklicken
    try:
        page.evaluate("""
            const sel = '[id*=cookie i], [class*=cookie i], [id*=consent i], [class*=consent i]';
            document.querySelectorAll(sel).forEach(e => e.remove());
        """)
    except Exception:
        pass

    # Username eintippen
    try:
        page.fill(conf["username_selector"], conf["username"], timeout=5000)
    except Exception as exc:
        return False, f"username_selector '{conf['username_selector']}' nicht gefunden ({type(exc).__name__})"
    # Passwort eintippen
    try:
        page.fill(conf["password_selector"], conf["password"], timeout=5000)
    except Exception as exc:
        return False, f"password_selector '{conf['password_selector']}' nicht gefunden ({type(exc).__name__})"
    # Submit
    try:
        page.click(conf["submit_selector"], timeout=5000)
    except Exception as exc:
        return False, f"submit_selector '{conf['submit_selector']}' nicht klickbar ({type(exc).__name__})"

    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass

    final_url = ""
    try:
        final_url = page.url
    except Exception:
        pass

    # Heuristik: wenn die finale URL noch immer die login_url ist, ist der Login
    # vermutlich abgelehnt (falsche Credentials, weil Keycloak/etc. zurueck zum
    # Formular leitet).
    if final_url and conf["login_url"] in final_url:
        return False, f"Nach Submit noch auf Login-Seite ({final_url[:120]}) - Credentials/CSRF/Captcha?"

    succ = conf.get("success_selector")
    if succ:
        try:
            page.wait_for_selector(succ, timeout=5000)
            return True, f"ok (success_selector gefunden auf {final_url[:80]})"
        except Exception:
            return False, (
                f"success_selector '{succ}' nicht gefunden auf {final_url[:120]} "
                "- Login ggf. erfolgreich, aber Selektor stimmt nicht. "
                "Tipp: success_selector leer lassen."
            )
    return True, f"ok (kein success_selector, finale URL: {final_url[:80]})"
