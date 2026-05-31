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
        # Vorsicht: bei Keycloak ist die login_url eine SSO-URL, Erfolg
        # bedeutet Weiterleitung WEG von dort. Wenn wir noch dort sind -> Fehler.
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
