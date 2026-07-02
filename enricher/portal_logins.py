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


# Fallback-Selektoren: greifen, wenn der konfigurierte Selektor nichts
# findet. Deckt die gaengigen Login-Formulare (Keycloak, Wicket,
# Rails/Devise, Cosinex, Healy-Hudson) ab.
FALLBACK_USERNAME_SELECTORS = [
    "input[type='email']",
    "input[name*='user' i][type='text']",
    "input[name*='mail' i]",
    "input[name*='login' i]",
    "input[id*='user' i]",
    "input[id*='mail' i]",
    "input[autocomplete='username']",
    "form input[type='text']:visible",
]
FALLBACK_PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[autocomplete='current-password']",
]
FALLBACK_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "form button",
    "button:has-text('Anmelden')",
    "button:has-text('Login')",
    "button:has-text('Einloggen')",
    "a:has-text('Anmelden')",
]
# 2FA-Code-Eingabefelder (TOTP): Auto-Erkennung nach dem ersten Submit.
FALLBACK_TOTP_SELECTORS = [
    "input[autocomplete='one-time-code']",
    "input[name*='otp' i]",
    "input[name*='totp' i]",
    "input[name*='code' i][type='text']",
    "input[name*='code' i][type='tel']",
    "input[name*='code' i][type='number']",
    "input[id*='otp' i]",
    "input[id*='token' i]",
]

# Buttons, die Cookie-Consent akzeptieren (Klick > DOM-Remove, weil viele
# Portale den Consent serverseitig speichern und sonst je Seite neu fragen).
COOKIE_ACCEPT_TEXTS = [
    "Alle akzeptieren", "Akzeptieren", "Alles akzeptieren", "Zustimmen",
    "Einverstanden", "Accept all", "Accept", "OK",
]


def _dismiss_cookie_banner(page) -> None:
    """Cookie-Banner erst per Klick akzeptieren, dann Reste entfernen."""
    for txt in COOKIE_ACCEPT_TEXTS:
        try:
            btn = page.locator(f"button:has-text('{txt}'), a:has-text('{txt}')").first
            if btn.count() and btn.is_visible():
                btn.click(timeout=1500, no_wait_after=True)
                page.wait_for_timeout(400)
                break
        except Exception:
            continue
    try:
        page.evaluate("""
            const sel = '[id*=cookie i], [class*=cookie i], [id*=consent i], [class*=consent i]';
            document.querySelectorAll(sel).forEach(e => e.remove());
        """)
    except Exception:
        pass


def _fill_first(page, configured: str, fallbacks: list[str], value: str,
                what: str) -> tuple[bool, str]:
    """Versucht erst den konfigurierten Selektor, dann die Fallbacks.
    Liefert (ok, benutzter_selektor_oder_fehler)."""
    candidates = [configured] + [s for s in fallbacks if s != configured]
    for sel in candidates:
        if not sel:
            continue
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.fill(value, timeout=4000)
            return True, sel
        except Exception:
            continue
    return False, f"{what}: keiner der Selektoren traf ({configured} + {len(fallbacks)} Fallbacks)"


def _click_first(page, configured: str, fallbacks: list[str]) -> tuple[bool, str]:
    candidates = [configured] + [s for s in fallbacks if s != configured]
    for sel in candidates:
        if not sel:
            continue
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.click(timeout=4000)
            return True, sel
        except Exception:
            continue
    return False, f"Submit: keiner der Selektoren klickbar ({configured} + {len(fallbacks)} Fallbacks)"


def perform_login(page, conf: dict, timeout_ms: int = 20000) -> tuple[bool, str]:
    """Fuehrt den Login-Flow aus - mit Fallback-Selektoren und einem
    automatischen Retry. Liefert (ok, reason)."""
    if not conf.get("login_url") or not conf.get("username") or not conf.get("password"):
        return False, "Config unvollstaendig: login_url/username/password fehlt"

    last_reason = ""
    for attempt in (1, 2):
        ok, reason = _perform_login_once(page, conf, timeout_ms)
        if ok:
            return True, reason if attempt == 1 else f"{reason} (2. Versuch)"
        last_reason = reason
        log.info("Login-Versuch %d fehlgeschlagen: %s", attempt, reason)
        try:
            page.wait_for_timeout(1500)
        except Exception:
            pass
    return False, last_reason


def _generate_totp(secret: str) -> str | None:
    """Generiert den aktuellen 6-stelligen TOTP-Code aus dem Base32-Secret."""
    try:
        import pyotp
    except ImportError:
        log.warning("pyotp nicht installiert - TOTP-Login nicht moeglich "
                    "(pip install pyotp + Image neu bauen)")
        return None
    try:
        clean = secret.strip().replace(" ", "").upper()
        return pyotp.TOTP(clean).now()
    except Exception as exc:
        log.warning("TOTP-Generierung fehlgeschlagen: %s", exc)
        return None


def _handle_totp_step(page, conf: dict, timeout_ms: int) -> tuple[bool | None, str]:
    """Erkennt und fuellt das 2FA-Code-Feld nach dem ersten Submit.

    Liefert:
      (True, info)   - Code eingegeben + abgeschickt
      (False, err)   - 2FA-Feld da, aber Code-Eingabe fehlgeschlagen
      (None, "")     - kein 2FA-Feld gefunden (Portal fragt nicht / schon vorbei)
    """
    configured = (conf.get("totp_selector") or "").strip()
    candidates = ([configured] if configured else []) + FALLBACK_TOTP_SELECTORS

    field = None
    used_sel = ""
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                field = loc
                used_sel = sel
                break
        except Exception:
            continue
    if field is None:
        return None, ""

    code = _generate_totp(conf["totp_secret"])
    if not code:
        return False, "2FA-Feld gefunden, aber TOTP-Code konnte nicht generiert werden (Secret pruefen / pyotp fehlt)"

    try:
        field.fill(code, timeout=4000)
    except Exception as exc:
        return False, f"2FA-Code-Eingabe fehlgeschlagen ({used_sel}): {type(exc).__name__}"

    # Absenden: erst konfigurierten Submit probieren, dann Fallbacks, dann Enter.
    ok, _ = _click_first(page, conf.get("submit_selector", ""), FALLBACK_SUBMIT_SELECTORS)
    if not ok:
        try:
            field.press("Enter")
        except Exception:
            return False, "2FA-Code eingegeben, aber kein Submit-Weg gefunden"

    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    try:
        page.wait_for_timeout(800)
    except Exception:
        pass
    log.info("2FA-Code eingegeben (Feld: %s)", used_sel)
    return True, f"2FA ok ({used_sel})"


def _perform_login_once(page, conf: dict, timeout_ms: int) -> tuple[bool, str]:
    try:
        page.goto(conf["login_url"], wait_until="networkidle", timeout=timeout_ms)
    except Exception as exc:
        return False, f"login_url '{conf['login_url']}' nicht ladbar: {exc}"

    _dismiss_cookie_banner(page)

    ok, info = _fill_first(page, conf.get("username_selector", ""),
                           FALLBACK_USERNAME_SELECTORS, conf["username"], "Username")
    if not ok:
        return False, info
    ok, info = _fill_first(page, conf.get("password_selector", ""),
                           FALLBACK_PASSWORD_SELECTORS, conf["password"], "Passwort")
    if not ok:
        return False, info
    ok, info = _click_first(page, conf.get("submit_selector", ""),
                            FALLBACK_SUBMIT_SELECTORS)
    if not ok:
        return False, info

    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    try:
        page.wait_for_timeout(800)
    except Exception:
        pass

    # 2FA-Schritt (TOTP via Authenticator-Secret)?
    if conf.get("totp_secret"):
        handled, totp_info = _handle_totp_step(page, conf, timeout_ms)
        if handled is False:
            return False, totp_info
        # handled is None = kein 2FA-Feld aufgetaucht -> normal weiter

    final_url = ""
    try:
        final_url = page.url
    except Exception:
        pass

    # Fehlermeldung auf der Seite? (Keycloak/Devise zeigen .alert/.error)
    try:
        err_el = page.locator(".alert-danger, .alert-error, .error-message, [class*='login-error' i]").first
        if err_el.count() and err_el.is_visible():
            err_txt = (err_el.inner_text() or "").strip()[:150]
            if err_txt:
                return False, f"Portal meldet: {err_txt}"
    except Exception:
        pass

    # Heuristik: finale URL noch die login_url -> vermutlich abgelehnt.
    if final_url and conf["login_url"].split("?")[0] in final_url and "logout" not in final_url:
        # Ausnahme: success_selector trifft trotzdem (SPA-Logins bleiben auf der URL)
        succ = conf.get("success_selector")
        if succ:
            try:
                page.wait_for_selector(succ, timeout=3000)
                return True, f"ok (success_selector auf Login-URL - SPA-Login, {final_url[:80]})"
            except Exception:
                pass
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
