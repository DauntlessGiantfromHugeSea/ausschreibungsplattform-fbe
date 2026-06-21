"""FBE Enricher Bot.

Loop:
  1. Hole nicht-angereicherte Tender von fbe-tender (Internal-API).
  2. Rendere jede tender.url mit Chromium (Playwright).
  3. Lass OpenAI strukturierte Felder extrahieren.
  4. Schreibe Markdown-Datei + POSTe Zusammenfassung zurueck an fbe-tender.

Konfiguration via Environment / .env:
  FBE_TENDER_URL       Base-URL der Plattform, z.B. http://localhost:8000
  ENRICHER_TOKEN       Shared-Secret, identisch zur fbe-tender-Konfig
  OPENAI_API_KEY       OpenAI-Key
  OPENAI_MODEL         Default 'gpt-4o-mini'
  ENRICH_DIR           Zielverzeichnis fuer .md (default /data/enrich)
  POLL_INTERVAL_S      Sekunden zwischen Poll-Runs (default 600)
  BATCH_LIMIT          Tender pro Poll (default 5)
  PAGE_TIMEOUT_MS      Playwright-Timeout pro Seite (default 30000)
"""
from __future__ import annotations

import io
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright

try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except ImportError:  # pragma: no cover
    pdf_extract_text = None

from portal_logins import (
    load_config as _load_login_cfg,
    find_config_for as _find_login_cfg,
    perform_login as _perform_login,
    saved_state_path as _saved_state_path,
    save_session as _save_session,
    is_session_alive as _is_session_alive,
    clear_session as _clear_session,
)

# Fallback: ENV-basierte Logins (nur wenn die Plattform keine liefert).
_ENV_LOGINS = _load_login_cfg()
# In-Memory-Set: pro Container-Lifetime, fuer welche Domains schon eingeloggt wurde.
_LOGGED_IN: set[str] = set()
# Cache der Logins aus der Plattform-DB - alle 60s neu geholt.
_REMOTE_LOGINS: dict = {}
_REMOTE_LOGINS_LAST_FETCH: float = 0.0


def get_portal_logins() -> dict:
    """Liefert das aktuelle Login-Set: Plattform-DB hat Vorrang vor ENV."""
    global _REMOTE_LOGINS, _REMOTE_LOGINS_LAST_FETCH
    now = time.time()
    if now - _REMOTE_LOGINS_LAST_FETCH > 60:
        try:
            r = http.get("/api/internal/portal-logins")
            r.raise_for_status()
            items = r.json()
            _REMOTE_LOGINS = {it["host"]: it for it in items if it.get("host")}
            _REMOTE_LOGINS_LAST_FETCH = now
        except Exception as exc:
            log.info("Konnte Portal-Logins-API nicht abrufen: %s (nutze ENV-Fallback)", exc)
    if _REMOTE_LOGINS:
        return _REMOTE_LOGINS
    return _ENV_LOGINS


def report_login_result(login_id: int | None, ok: bool, error: str = "") -> None:
    """Meldet einen Login-Versuch an die Plattform zurueck (nur fuer DB-Logins)."""
    if not login_id:
        return
    try:
        http.post(
            f"/api/internal/portal-logins/{login_id}/result",
            json={"ok": ok, "error": error},
            timeout=5,
        )
    except Exception:
        pass


def run_pending_login_tests() -> int:
    """Holt explizit angeforderte Login-Tests und fuehrt sie aus.

    Pro Test: Playwright-Browser starten, perform_login() ausfuehren,
    Ergebnis an die Plattform melden. Cookies/Context werden NICHT
    zwischen Tests behalten - jeder Test bekommt frischen Browser,
    damit das Resultat ehrlich ist.
    """
    try:
        r = http.get("/api/internal/portal-logins-pending-test", timeout=10)
        r.raise_for_status()
        items = r.json()
    except Exception as exc:
        log.debug("pending-login-test fetch fehlgeschlagen: %s", exc)
        return 0
    if not items:
        return 0
    log.info("%d Login-Test(s) angefordert.", len(items))
    with sync_playwright() as pw:
        for conf in items:
            lid = conf.get("id")
            host = conf.get("host", "?")
            log.info("Login-Test fuer %s ...", host)
            # Login-Test = bewusst ohne gespeicherte Session, damit der Test
            # ehrlich von vorne pruefte, dass Credentials + Selektoren stimmen.
            _clear_session(host)
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                ctx = browser.new_context(
                    user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
                    locale="de-DE",
                )
                page = ctx.new_page()
                page.set_default_timeout(PAGE_TIMEOUT_MS)
                try:
                    ok, reason = _perform_login(page, conf, timeout_ms=PAGE_TIMEOUT_MS)
                except Exception as exc:
                    log.warning("Login-Test %s Exception: %s", host, exc)
                    report_login_result(lid, False, f"exception: {exc}")
                    continue
                if ok:
                    # Erfolgreicher Test -> Session auf Disk fuer
                    # spaetere Crawls (kein erneuter Login noetig).
                    _save_session(ctx, host)
                    log.info("Login-Test %s OK (%s) - Session gespeichert", host, reason)
                    report_login_result(lid, True, reason)
                else:
                    log.warning("Login-Test %s FAIL: %s", host, reason)
                    report_login_result(lid, False, reason)
            finally:
                browser.close()
    return len(items)


load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("enricher")

FBE_URL = os.environ.get("FBE_TENDER_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("ENRICHER_TOKEN", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
ENRICH_DIR = Path(os.environ.get("ENRICH_DIR", "/data/enrich"))
POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_S", "600"))
BATCH_LIMIT = int(os.environ.get("BATCH_LIMIT", "5"))
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "30000"))
# Deep-Crawl-Tuning
MAX_SUBPAGES = int(os.environ.get("MAX_SUBPAGES", "4"))
MAX_PDFS = int(os.environ.get("MAX_PDFS", "25"))
MAX_ATTACHMENT_MB = int(os.environ.get("MAX_ATTACHMENT_MB", "30"))
ATTACHMENT_EXTENSIONS = tuple(
    s.strip().lower()
    for s in os.environ.get(
        "ATTACHMENT_EXTENSIONS",
        ".pdf,.docx,.doc,.xlsx,.xls,.zip,.txt,.rtf,.odt,.ods,.csv"
    ).split(",")
    if s.strip()
)
MAX_TEXT_PER_SOURCE = int(os.environ.get("MAX_TEXT_PER_SOURCE", "6000"))
MAX_TOTAL_TEXT = int(os.environ.get("MAX_TOTAL_TEXT", "22000"))

# Linktext-Pattern, die wir als 'mehr Details' interpretieren (case-insensitive).
DETAIL_LINK_PATTERNS = [
    r"mehr.*(anzeigen|info|details?)?",
    r"details?",
    r"vergabeunterlagen?",
    r"leistungsverzeichnis",
    r"leistungsbeschreibung",
    r"bekanntmachung(stext)?",
    r"ausschreibungsunterlagen",
    r"kontakt",
    r"auftraggeber",
    r"objekt(beschreibung)?",
    r"weiterlesen",
    r"\bvollständig",
    r"aufklappen",
    r"einsehen",
]
DETAIL_LINK_RE = re.compile("|".join(DETAIL_LINK_PATTERNS), re.IGNORECASE)
# Expander-Buttons: wir klicken alles, was diesen Pattern enthaelt.
EXPAND_BUTTON_PATTERNS = ["mehr", "anzeigen", "aufklappen", "weiterlesen", "details", "akzeptieren"]
# Buttons, die ein Dokument-/Anhang-Panel oeffnen (Klick noetig, bevor Links sichtbar sind).
REVEAL_DOCS_PATTERNS = [
    "ausschreibungsunterlagen einsehen",
    "ausschreibungsunterlagen",
    "vergabeunterlagen einsehen",
    "vergabeunterlagen",
    "unterlagen einsehen",
    "unterlagen anzeigen",
    "dokumente anzeigen",
    "dokumente einsehen",
    "anlagen anzeigen",
    "downloads anzeigen",
]

if not TOKEN:
    log.error("ENRICHER_TOKEN ist nicht gesetzt - Abbruch.")
    sys.exit(2)
if not OPENAI_KEY:
    log.error("OPENAI_API_KEY ist nicht gesetzt - Abbruch.")
    sys.exit(2)

ENRICH_DIR.mkdir(parents=True, exist_ok=True)

oa = OpenAI(api_key=OPENAI_KEY)
http = httpx.Client(
    base_url=FBE_URL,
    headers={"X-Internal-Token": TOKEN},
    timeout=30.0,
)


PROMPT = """Du bist ein Spezialist fuer oeffentliche Bauausschreibungen (Tiefbau, Erdarbeiten, Leitungsbau).
Aus dem unten gesammelten Material (Hauptseite, Unterseiten, ggf. PDF-Anhaenge) extrahiere strukturiert die wichtigsten Informationen.
Die Quellen sind durch [MAIN]/[SUB]/[PDF]-Tags getrennt. Nutze ALLE Quellen, nicht nur die erste.

Ausgabe-Format: Markdown, exakt diese Abschnitte (auch wenn ein Feld leer bleibt):

# {title}

**Quelle:** {url}
**Stand:** {now}

## Eckdaten
- **Auftraggeber:** ...
- **Erfuellungsort / Ort:** ...
- **Submission-Frist:** ...
- **Geschaetzter Auftragswert:** ...
- **Verfahrensart:** ... (oeffentlich, beschraenkt, freihaendig, etc.)
- **CPV-Codes:** ...
- **Kontakt (Vergabestelle):** Name / Telefon / Mail wenn im Material zu finden

## Leistungsbeschreibung
6-12 Saetze, was ausgeschrieben ist - was, wo, Umfang, technische Anforderungen.
Konkrete Mengen / Stueckzahlen / Flaechen / Massnahmen explizit aufgreifen.

## Gewerke & Schluesselbegriffe
Bullet-Liste aller fachlich relevanten Begriffe (Tiefbau, Verfuellung, Spundwand, ZFSV, Fluessigboden, Leitungsbau, etc.) - aber nur die, die im Material wirklich vorkommen.

## Submission-Hinweise
Wie wird abgegeben? Welche Unterlagen werden gefordert? Welche Plattform?

## Bewertung fuer FBE
Kurze Einschaetzung (max. 3 Saetze): warum ist das fuer Fluessigboden Engineering relevant - oder warum nicht? Welche Hebel sieht man (ZFSV, Verfuellung, Leitungsgraeben)?

## Verwendete Quellen
Bullet-Liste der URLs/PDF-Pfade, die du tatsaechlich genutzt hast.

Wenn das gesamte Material offensichtlich nur Login-/Cookie-Wall ist und nichts Inhaltliches enthaelt:
"## Hinweis\nDie Detailseite ist nicht oeffentlich zugaenglich (Login erforderlich)."
"""


def fetch_tenders_to_enrich() -> list[dict]:
    try:
        r = http.get("/api/internal/tenders-to-enrich", params={"limit": BATCH_LIMIT})
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("Konnte Tender-Liste nicht holen: %s", exc)
        return []


def fetch_knowledge() -> str:
    """Holt alle Knowledge-MD-Dateien von fbe-tender und kombiniert sie."""
    try:
        r = http.get("/api/internal/knowledge")
        r.raise_for_status()
        files = r.json()
    except Exception as exc:
        log.info("Knowledge-Endpoint nicht erreichbar: %s", exc)
        return ""
    if not files:
        return ""
    parts = []
    for f in files:
        name = f.get("name", "")
        content = (f.get("content") or "").strip()
        if not content:
            continue
        parts.append(f"### Notiz: {name}\n{content}")
    if not parts:
        return ""
    return ("\n\n--- WISSENSBASIS (vom Admin gepflegte Notizen, beachte diese bei der Bewertung) ---\n\n"
            + "\n\n".join(parts))


def _kill_overlays(page) -> None:
    """Cookie-Banner / Consent-Overlays best-effort entfernen."""
    try:
        page.evaluate("""
            const sel = '[id*=cookie i], [class*=cookie i], [id*=consent i], [class*=consent i], [id*=overlay i], [class*=overlay i]';
            document.querySelectorAll(sel).forEach(e => e.remove());
        """)
    except Exception:
        pass


def _auto_expand(page) -> int:
    """Klickt alle Buttons/Toggles, deren Beschriftung auf 'Mehr/Details' deutet.

    Liefert die Anzahl der ausgefuehrten Klicks. Best-effort, ohne Exception.
    """
    clicked = 0
    for pat in EXPAND_BUTTON_PATTERNS:
        try:
            # Buttons + Links + Summary-Elemente
            locator = page.locator(
                f"button:has-text('{pat}'), summary:has-text('{pat}'), a:has-text('{pat}')",
                has_text=re.compile(pat, re.IGNORECASE),
            )
            n = min(locator.count(), 10)
            for i in range(n):
                try:
                    locator.nth(i).click(timeout=2000, no_wait_after=True)
                    clicked += 1
                except Exception:
                    continue
        except Exception:
            continue
    if clicked:
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
    return clicked


def _page_text(page, limit: int = MAX_TEXT_PER_SOURCE) -> str:
    """Liefert den sichtbaren Text der aktuellen Seite (HTML -> Text)."""
    try:
        html = page.content()
    except Exception:
        return ""
    return _html_to_text(html, limit)


def _html_to_text(html: str, limit: int) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit] + "\n[... gekuerzt ...]"
    return text


def _portal_specific_subpages(base_url: str) -> list[str]:
    """Liefert deterministisch konstruierbare Sub-URLs pro Portal, wo
    Vergabeunterlagen liegen. Spart uns das Klicken durch Reveal-Buttons.

    evergabe-online.de:
      tenderdetails.html?id=NNN  ->  tenderdocuments.html?id=NNN&cookieCheck
    """
    out: list[str] = []
    try:
        parsed = urlparse(base_url)
    except Exception:
        return out
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    query = parsed.query or ""
    if "evergabe-online.de" in host and "tenderdetails.html" in path:
        # ID extrahieren und auf tenderdocuments.html mappen
        m = re.search(r"[?&]id=(\d+)", query)
        if m:
            tid = m.group(1)
            out.append(f"https://www.evergabe-online.de/tenderdocuments.html?id={tid}&cookieCheck")
    return out


def _find_detail_subpages(page, base_url: str, max_n: int) -> list[str]:
    """Sammelt Sub-URLs der gleichen Domain, deren Linktext auf 'Details'
    hindeutet. Max max_n eindeutige URLs.

    Schliesst explizit URLs aus, die nach Datei-Download aussehen
    (Wicket-AJAX 'downloadLink', /download/, /files/, Endung .pdf/.docx etc.)
    - solche URLs werden separat ueber _find_pdf_links abgearbeitet und
    duerfen NICHT als page.goto() angesteuert werden (Playwright bricht
    sonst mit 'Download is starting' ab).
    """
    base_host = urlparse(base_url).netloc
    found, seen = [], set()
    download_marker_re = re.compile(
        r"(downloadlink|download(\.html|attachment|file)?|getfile|attachment|/file\?|/files/|fileservlet|/downloads/installer/)",
        re.IGNORECASE,
    )
    file_ext_re = re.compile(
        r"\.(pdf|docx?|xlsx?|zip|csv|rtf|odt|ods|txt)(\?|$)",
        re.IGNORECASE,
    )
    try:
        html = page.content()
    except Exception:
        return []
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if len(found) >= max_n:
            break
        text = (a.get_text(" ", strip=True) or "")[:120]
        href = a["href"].strip()
        if not text or not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        full = urljoin(base_url, href)
        if urlparse(full).netloc != base_host:
            continue
        if full == base_url or full in seen:
            continue
        # Datei-Downloads NICHT als Sub-Pages anfahren (page.goto wuerde sonst
        # mit 'Download is starting' abbrechen). Diese URLs werden separat
        # ueber _find_pdf_links + _download_and_extract_pdf erfasst.
        if download_marker_re.search(full) or file_ext_re.search(full):
            continue
        if not DETAIL_LINK_RE.search(text):
            continue
        seen.add(full)
        found.append(full)
    return found


def _find_pdf_links(page, base_url: str, max_n: int) -> list[str]:
    """Sammelt Dokument-Links der gleichen Domain.

    Erfasst nicht nur .pdf-URLs, sondern alle Endungen aus ATTACHMENT_EXTENSIONS
    sowie Server-Side-Downloads (download.html, downloadAttachment, getFile,
    attachment, file?id=, Link mit download-Attribut).
    """
    base_host = urlparse(base_url).netloc
    out, seen = [], set()
    try:
        html = page.content()
    except Exception:
        return []
    soup = BeautifulSoup(html, "html.parser")
    download_url_re = re.compile(
        r"(downloadlink|download(\.html|attachment|file)?|getfile|attachment|/file\?|/files/|fileservlet)",
        re.IGNORECASE,
    )
    # Bekannte False-Positives ausschliessen (Installer, Marketing-Downloads).
    blocklist_re = re.compile(
        r"(/downloads/installer/|/installer\.|/setup\.exe|/marketing/)",
        re.IGNORECASE,
    )
    for a in soup.find_all("a", href=True):
        if len(out) >= max_n:
            break
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        href_clean = href.split("#", 1)[0]
        if not href_clean:
            continue
        full = urljoin(base_url, href_clean)
        if urlparse(full).netloc != base_host:
            continue
        if full in seen:
            continue
        if blocklist_re.search(full):
            continue
        low = full.lower()
        # Direkter Dateilink ueber Endung?
        ext_match = any(low.endswith(ext) or (ext + "?") in low for ext in ATTACHMENT_EXTENSIONS)
        # Server-Side-Download-URL?
        download_match = bool(download_url_re.search(low))
        # <a download="..."> Attribut?
        has_download_attr = a.has_attr("download")
        if not (ext_match or download_match or has_download_attr):
            continue
        seen.add(full)
        out.append(full)
    return out


def _reveal_documents(page) -> int:
    """Klickt EINMAL den 'EINSEHEN'-Button (Wicket-AJAX) und navigiert
    damit zur Dokumenten-Seite mit der vollstaendigen Datei-Tabelle.

    WICHTIG: Nur EIN Klick, kein Pattern-Loop. Mehrfach-Klicks zerstoeren
    auf evergabe-online die Wicket-Session und landen auf internalerror.html.
    Liefert 1 wenn geklickt, 0 sonst.
    """
    try:
        # Bevorzugt: Anchor mit btn-primary-Klasse + 'einsehen' im Text
        loc = page.locator("a.btn.btn-primary").filter(
            has_text=re.compile(r"einsehen", re.IGNORECASE)).first
        if loc.count() == 0:
            # Fallback: irgendein Button/Link mit 'unterlagen' und 'einsehen'
            loc = page.locator("a, button").filter(
                has_text=re.compile(r"(unterlagen|dokument).*einsehen", re.IGNORECASE)).first
        if loc.count() == 0:
            return 0
        loc.scroll_into_view_if_needed(timeout=2000)
        try:
            with page.expect_navigation(timeout=10000):
                loc.click(timeout=5000)
        except Exception:
            # Manche Wicket-Klicks loesen kein navigation-event aus -> kein Problem
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
        page.wait_for_timeout(1500)
        log.info("reveal_documents: nach Klick URL=%s", page.url[:140])
        return 1
    except Exception as exc:
        log.info("reveal_documents fehlgeschlagen: %s", exc)
        return 0


def _reveal_documents_DISABLED(page) -> int:
    """Klickt 'Ausschreibungsunterlagen einsehen' o.ae., damit die Datei-Tabelle
    sichtbar wird. Liefert die Zahl der ausgefuehrten Klicks.

    Apache-Wicket-Plattformen (evergabe-online) reagieren mit AJAX *oder*
    Navigation auf eine eigene Anhangseite. Beides muss funktionieren:
    - bei AJAX warten wir auf zusaetzliche Dokument-Links im DOM
    - bei Navigation laesst Playwright die neue Seite einfach laden
    """
    clicked = 0
    pdf_count_before = _count_download_links(page)
    start_url = page.url

    for pat in REVEAL_DOCS_PATTERNS:
        try:
            # 1. Text-Match (gross/klein egal)
            locator = page.locator(
                f"a:has-text('{pat}'), button:has-text('{pat}'), input[value*='{pat}' i]"
            )
            n = min(locator.count(), 3)
            for i in range(n):
                try:
                    locator.nth(i).scroll_into_view_if_needed(timeout=1500)
                    # Wicket: KEIN no_wait_after - kann navigieren
                    locator.nth(i).click(timeout=5000)
                    clicked += 1
                    # Nach jedem Klick kurz warten + DOM-Check
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    page.wait_for_timeout(800)
                    # Genug PDFs zu sehen? Dann fertig
                    if _count_download_links(page) > pdf_count_before:
                        return clicked
                except Exception:
                    continue
        except Exception:
            continue

    # 2. Fallback ueber Klassen-Selektor (Wicket-Standard btn-primary)
    if clicked == 0:
        try:
            wicket_btn = page.locator("main a.btn.btn-primary, a.btn-primary, button.btn-primary").first
            if wicket_btn.count() > 0:
                txt = (wicket_btn.inner_text() or "").lower()
                if any(k in txt for k in ["unterlagen", "einsehen", "dokument", "anhang"]):
                    wicket_btn.scroll_into_view_if_needed(timeout=1500)
                    wicket_btn.click(timeout=5000)
                    clicked += 1
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    page.wait_for_timeout(1200)
        except Exception:
            pass

    # 3. Wenn navigiert wurde, ist der DOM ggf. ganz neu - egal, _find_pdf_links
    #    scannt einfach den aktuellen Zustand. Hier nur final stabilisieren.
    if clicked:
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    if page.url != start_url:
        log.info("reveal_documents: navigiert nach %s", page.url[:120])
    return clicked


def _count_download_links(page) -> int:
    """Best-effort: zaehlt, wie viele plausible Download-Links aktuell im DOM sind."""
    try:
        return int(page.evaluate("""() => {
            const re = /(\\.pdf|\\.docx?|\\.xlsx?|\\.zip|download|attachment|getfile|\\/files\\/)/i;
            return [...document.querySelectorAll('a[href]')].filter(a => re.test(a.getAttribute('href') || '')).length;
        }"""))
    except Exception:
        return 0


def _filename_from_response(resp, fallback_url: str) -> str:
    """Liest Content-Disposition oder faellt auf URL-Pfad zurueck."""
    from urllib.parse import unquote
    try:
        cd = resp.headers.get("content-disposition", "") if resp else ""
    except Exception:
        cd = ""
    if cd:
        m = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", cd, re.IGNORECASE)
        if m:
            return unquote(m.group(1).strip().strip('"'))
    path = urlparse(fallback_url).path
    name = unquote(path.rsplit("/", 1)[-1]) or "anhang.bin"
    return name


def _click_download_capture(page, ctx, url: str) -> tuple[str, bytes, str, str]:
    """Klickt das href=url auf der Seite an, faengt den Browser-Download ab.
    Liefert (filename, body, content_type, extracted_text). Leere Werte bei Fehler.
    Wird gebraucht, wenn ein Server nur per Session-Cookie + POST Downloads ausliefert."""
    try:
        sel = f"a[href='{url}']"
        loc = page.locator(sel).first
        if loc.count() == 0:
            # Versuche mit relativem href
            from urllib.parse import urlparse as _up
            rel = _up(url).path
            loc = page.locator(f"a[href='{rel}']").first
            if loc.count() == 0:
                return "", b"", "", ""
        with page.expect_download(timeout=PAGE_TIMEOUT_MS) as dl_info:
            loc.click(timeout=5000, no_wait_after=True)
        download = dl_info.value
        path = Path(download.path()) if download.path() else None
        if not path or not path.exists():
            return "", b"", "", ""
        body = path.read_bytes()
        filename = download.suggested_filename or _filename_from_response(None, url)
        ct = ""
        # Endung -> Content-Type-Hint
        low = filename.lower()
        if low.endswith(".pdf"):
            ct = "application/pdf"
        elif low.endswith(".docx"):
            ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif low.endswith(".xlsx"):
            ct = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif low.endswith(".zip"):
            ct = "application/zip"
        # Text aus PDF
        text = ""
        if low.endswith(".pdf") and pdf_extract_text:
            try:
                text = pdf_extract_text(io.BytesIO(body)) or ""
                text = _html_to_text(text, MAX_TEXT_PER_SOURCE) if text else ""
            except Exception:
                text = ""
        return filename, body, ct, text
    except Exception as exc:
        log.info("Download-Click fuer %s fehlgeschlagen: %s", url[:80], exc)
        return "", b"", "", ""


def _download_and_extract_pdf(ctx, url: str) -> tuple[str, bytes, str, str]:
    """Laedt ein Dokument ueber den Playwright-Kontext (= mit Session-Cookies)
    und extrahiert ggf. Text. Liefert (extracted_text, body, content_type, filename).
    body leer bei Fehler oder HTML-Antwort statt Datei."""
    try:
        req = ctx.request
        resp = req.get(url, timeout=PAGE_TIMEOUT_MS)
        if resp.status >= 400:
            return "", b"", "", ""
        body = resp.body()
        if not body or len(body) < 200:
            return "", b"", "", ""
        # Groessen-Limit
        if len(body) > MAX_ATTACHMENT_MB * 1024 * 1024:
            log.info("Anhang %s zu gross (%.1f MB), uebersprungen", url[:80], len(body) / 1024 / 1024)
            return "", b"", "", ""
        ct = ""
        cd = ""
        try:
            ct = resp.headers.get("content-type", "")
            cd = resp.headers.get("content-disposition", "")
        except Exception:
            pass
        filename = _filename_from_response(resp, url)
        # HTML-Antwort statt Datei? -> kein Anhang, ggf. via Klick versuchen
        low_ct = (ct or "").lower()
        has_attachment_cd = "attachment" in (cd or "").lower()
        is_file_ext = filename.lower().endswith(tuple(ATTACHMENT_EXTENSIONS))
        if "html" in low_ct and not (is_file_ext or has_attachment_cd):
            return "", b"", "", ""
        text = ""
        if filename.lower().endswith(".pdf") and pdf_extract_text:
            try:
                text = pdf_extract_text(io.BytesIO(body)) or ""
                text = _html_to_text(text, MAX_TEXT_PER_SOURCE) if text else ""
            except Exception:
                text = ""
        return text, body, ct, filename
    except Exception as exc:
        log.info("Download-Fehler %s: %s", url[:80], exc)
        return "", b"", "", ""


def _upload_attachment(tender_id: int, url: str, body: bytes, content_type: str, extracted_text: str, filename: str = "") -> None:
    """Speichert Anhang via Internal-API auf der Plattform. Failed silently."""
    import base64
    from urllib.parse import urlparse, unquote
    if not body:
        return
    if not filename:
        path = urlparse(url).path
        filename = unquote(path.rsplit("/", 1)[-1]) or "anhang.bin"
    try:
        http.post(
            f"/api/internal/tenders/{tender_id}/attachments",
            json={
                "filename": filename,
                "source_url": url,
                "content_type": content_type or "application/pdf",
                "size_bytes": len(body),
                "content_b64": base64.b64encode(body).decode("ascii"),
                "extracted_text": extracted_text[:2000] if extracted_text else "",
            },
            timeout=30,
        )
    except Exception as exc:
        log.info("Attachment-Upload fehlgeschlagen (%s): %s", filename, exc)


def deep_crawl(pw, url: str, tender_id: int | None = None) -> list[tuple[str, str, str]]:
    """Laedt url + bis zu MAX_SUBPAGES sinnvolle Unterseiten + bis zu MAX_PDFS PDFs.

    Liefert Liste von (kind, source_url, text). kind in {'main','sub','pdf'}.
    """
    sources: list[tuple[str, str, str]] = []
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    try:
        # Falls wir fuer den Host eine gespeicherte Session haben, laden
        # wir sie ins frische Browser-Context rein -> Cookies/localStorage
        # ueberleben Crawls + Container-Restarts.
        portal_logins = get_portal_logins()
        login_key, login_conf = _find_login_cfg(url, portal_logins)
        ctx_kwargs = dict(
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
            locale="de-DE",
            accept_downloads=True,
        )
        if login_key:
            sp = _saved_state_path(login_key)
            if sp:
                ctx_kwargs["storage_state"] = sp
                log.info("Session fuer %s aus %s wiederhergestellt", login_key, sp)
        try:
            ctx = browser.new_context(**ctx_kwargs)
        except Exception as exc:
            # storage_state-File kaputt? -> ohne probieren + Session loeschen
            log.warning("new_context mit storage_state fehlgeschlagen (%s) - fallback ohne Session", exc)
            if login_key:
                _clear_session(login_key)
            ctx_kwargs.pop("storage_state", None)
            ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        # 0) Login-Strategie:
        #    a) keine config -> skip
        #    b) Session restored UND alive -> skip
        #    c) sonst -> perform_login + save_session
        if login_conf:
            need_login = True
            if _saved_state_path(login_key):
                if _is_session_alive(page, login_conf, timeout_ms=PAGE_TIMEOUT_MS):
                    need_login = False
                    log.info("Bestehende Session fuer %s noch gueltig - kein Re-Login", login_key)
                else:
                    log.info("Bestehende Session fuer %s abgelaufen - Re-Login", login_key)
                    _clear_session(login_key)
            if need_login:
                log.info("Login-Versuch fuer Domain %s", login_key)
                ok, reason = _perform_login(page, login_conf, timeout_ms=PAGE_TIMEOUT_MS)
                if ok:
                    _save_session(ctx, login_key)
                    log.info("Login erfolgreich fuer %s (%s) - Session gespeichert", login_key, reason)
                    report_login_result(login_conf.get("id"), True, reason)
                else:
                    log.warning("Login fehlgeschlagen fuer %s: %s", login_key, reason)
                    report_login_result(login_conf.get("id"), False, reason)

        # 1) Hauptseite
        page.goto(url, wait_until="networkidle")
        _kill_overlays(page)
        _auto_expand(page)
        # 1b) Spezial: 'Ausschreibungsunterlagen einsehen' o.ae. klicken
        _reveal_documents(page)
        sources.append(("main", page.url, _page_text(page)))

        # 2) Dokumenten-Links auf der Main-Page sammeln
        pdf_urls = _find_pdf_links(page, url, MAX_PDFS)
        # Mapping URL -> page (fuer Click-Fallback noetig: auf welcher Seite war der Link?)
        url_origin: dict[str, str] = {u: page.url for u in pdf_urls}
        sub_urls = _find_detail_subpages(page, url, MAX_SUBPAGES)
        # Portal-spezifisch deterministisch konstruierbare Unterseiten
        # (z.B. evergabe-online tenderdocuments.html) ergaenzen
        for extra in _portal_specific_subpages(url):
            if extra not in sub_urls:
                sub_urls.insert(0, extra)

        # 3) Unterseiten besuchen
        for sub in sub_urls:
            try:
                page.goto(sub, wait_until="networkidle")
                _kill_overlays(page)
                _auto_expand(page)
                _reveal_documents(page)
                rest = max(0, MAX_PDFS - len(pdf_urls))
                if rest:
                    for u in _find_pdf_links(page, sub, rest):
                        if u not in pdf_urls:
                            pdf_urls.append(u)
                            url_origin[u] = page.url
                sources.append(("sub", sub, _page_text(page)))
            except Exception as exc:
                log.info("Sub-Page %s fehlgeschlagen: %s", sub[:80], exc)
                continue

        # 4) Dokumente laden:
        #    Strategie A: direkter GET ueber den Browser-Kontext (Cookies/Session).
        #    Strategie B (Fallback): wenn A leer/HTML zurueckliefert, navigieren wir
        #                          auf die Origin-Seite und klicken den Link an,
        #                          um Browser-Download-Events zu nutzen.
        for pu in pdf_urls[:MAX_PDFS]:
            txt, body, ct, fn = _download_and_extract_pdf(ctx, pu)
            if not body:
                # Fallback: per Klick downloaden
                origin = url_origin.get(pu)
                try:
                    if origin and page.url != origin:
                        page.goto(origin, wait_until="networkidle")
                        _kill_overlays(page); _reveal_documents(page)
                    fn2, body2, ct2, txt2 = _click_download_capture(page, ctx, pu)
                    if body2:
                        body, ct, fn, txt = body2, ct2, fn2, txt2
                except Exception as exc:
                    log.info("Click-Fallback fuer %s fehlgeschlagen: %s", pu[:80], exc)
            if body and tender_id:
                _upload_attachment(tender_id, pu, body, ct, txt, filename=fn)
            if txt:
                sources.append(("pdf", pu, txt))

        return sources
    finally:
        browser.close()


def combine_sources(sources: list[tuple[str, str, str]]) -> str:
    """Setzt die gesammelten Texte zu einer LLM-Eingabe zusammen, mit
    Quellen-Tags. Respektiert MAX_TOTAL_TEXT."""
    chunks, used = [], 0
    for kind, src, text in sources:
        if not text:
            continue
        header = f"\n\n--- [{kind.upper()}] {src} ---\n"
        budget = MAX_TOTAL_TEXT - used - len(header)
        if budget <= 200:
            break
        piece = text if len(text) <= budget else text[:budget] + "\n[... gekuerzt ...]"
        chunks.append(header + piece)
        used += len(header) + len(piece)
    return "".join(chunks).strip()


def llm_extract(tender: dict, text: str, knowledge: str = "") -> tuple[str, str]:
    """Liefert (markdown, summary).

    summary = die ersten ~300 Zeichen der Markdown-Antwort fuer das
    ai_analysis-Feld in der DB.

    `knowledge` ist optionaler vom Admin gepflegter Zusatzkontext aus
    /admin/notes (Wissensbasis), der dem System-Prompt vorangestellt wird.
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    user_msg = (
        f"Titel laut Plattform: {tender.get('title') or '-'}\n"
        f"Portal: {tender.get('portal') or '-'}\n"
        f"URL: {tender.get('url')}\n"
        f"Score (Plattform-Ranking): {tender.get('score')}\n\n"
        f"--- Gerenderter Seiteninhalt ---\n{text}\n"
    )
    system_msg = PROMPT.replace("{title}", tender.get("title", "(ohne Titel)")) \
                       .replace("{url}", tender.get("url", "")) \
                       .replace("{now}", now)
    if knowledge:
        system_msg = system_msg + "\n\n" + knowledge
    resp = oa.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
    )
    markdown = resp.choices[0].message.content or ""
    # Summary: erste Eckdaten-Zeilen, max 600 Zeichen
    summary = markdown.strip()[:600]
    return markdown.strip(), summary


def post_result(tender_id: int, markdown: str, summary: str, ok: bool, error: str = "") -> None:
    try:
        r = http.post(
            f"/api/internal/tenders/{tender_id}/enriched",
            json={"markdown": markdown, "summary": summary, "ok": ok, "error": error},
        )
        if r.status_code >= 300:
            log.warning("Result-POST fuer %s: HTTP %s %s", tender_id, r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("Result-POST fuer %s gescheitert: %s", tender_id, exc)


def process_one(pw, tender: dict, knowledge: str = "") -> None:
    tid = tender["id"]
    url = tender.get("url")
    if not url:
        post_result(tid, "", "", ok=False, error="leere URL")
        return
    log.info("[%s] Deep-Crawl Start: %s", tid, url[:120])
    try:
        sources = deep_crawl(pw, url, tender_id=tid)
    except Exception as exc:
        log.warning("[%s] Crawl-Fehler: %s", tid, exc)
        post_result(tid, "", "", ok=False, error=f"crawl: {exc}")
        return

    n_main = sum(1 for k, _, _ in sources if k == "main")
    n_sub = sum(1 for k, _, _ in sources if k == "sub")
    n_pdf = sum(1 for k, _, _ in sources if k == "pdf")
    log.info("[%s] gesammelt: %d main, %d sub, %d pdf", tid, n_main, n_sub, n_pdf)

    text = combine_sources(sources)
    if len(text) < 200:
        log.info("[%s] zu wenig Inhalt (%d Zeichen) - markiere als Login-Wall", tid, len(text))
        post_result(tid, "", "", ok=False, error=f"thin content ({len(text)}b)")
        return

    try:
        markdown, summary = llm_extract(tender, text, knowledge=knowledge)
    except Exception as exc:
        log.warning("[%s] LLM-Fehler: %s", tid, exc)
        post_result(tid, "", "", ok=False, error=f"llm: {exc}")
        return

    # Lokal in den ENRICH_DIR schreiben (Bind-Mount nach /srv/fbe-enrich).
    # fbe-tender macht das gleiche, wenn es den Result-POST empfaengt -
    # aber wir schreiben sicherheitshalber auch von hier, falls
    # fbe-tender und Enricher in unterschiedlichen Mounts arbeiten.
    try:
        (ENRICH_DIR / f"tender-{tid}.md").write_text(markdown, encoding="utf-8")
    except Exception as exc:
        log.warning("[%s] Konnte MD nicht lokal schreiben: %s", tid, exc)

    post_result(tid, markdown, summary, ok=True)
    log.info("[%s] fertig (%d Zeichen Markdown)", tid, len(markdown))


def run_once() -> int:
    items = fetch_tenders_to_enrich()
    if not items:
        log.info("Keine offenen Tender zum Anreichern.")
        return 0
    log.info("%d Tender zum Anreichern.", len(items))
    knowledge = fetch_knowledge()
    if knowledge:
        log.info("Wissensbasis geladen: %d Zeichen Kontext.", len(knowledge))
    with sync_playwright() as pw:
        for t in items:
            try:
                process_one(pw, t, knowledge=knowledge)
            except Exception as exc:
                log.exception("Unerwarteter Fehler bei tender %s: %s", t.get("id"), exc)
    return len(items)


LOGIN_TEST_POLL_S = int(os.environ.get("LOGIN_TEST_POLL_S", "15"))


def main() -> None:
    log.info(
        "Enricher gestartet. FBE=%s, Modell=%s, Tender-Intervall=%ds, Login-Test-Intervall=%ds, Batch=%d",
        FBE_URL, OPENAI_MODEL, POLL_INTERVAL_S, LOGIN_TEST_POLL_S, BATCH_LIMIT,
    )
    last_batch = 0.0
    while True:
        # Login-Tests jede Iteration pruefen - schnelle Reaktion auf
        # Admin-Klick "Jetzt testen".
        try:
            run_pending_login_tests()
        except Exception:
            log.exception("Login-Test-Lauf fehlgeschlagen.")

        # Tender-Batch nur alle POLL_INTERVAL_S Sekunden.
        if time.time() - last_batch >= POLL_INTERVAL_S:
            try:
                run_once()
            except Exception:
                log.exception("Poll-Run fehlgeschlagen.")
            last_batch = time.time()

        time.sleep(LOGIN_TEST_POLL_S)


if __name__ == "__main__":
    main()
