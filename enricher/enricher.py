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

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright


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
Aus dem unten gerenderten HTML-Inhalt einer Ausschreibungs-Detailseite extrahiere strukturiert die wichtigsten Informationen.

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

## Leistungsbeschreibung
3-6 Saetze, was ausgeschrieben ist - was, wo, in welchem Umfang.

## Gewerke & Schluesselbegriffe
Bullet-Liste aller fachlich relevanten Begriffe (Tiefbau, Verfuellung, Spundwand, ZFSV, Fluessigboden, Leitungsbau, etc.) - aber nur die, die im Text wirklich vorkommen.

## Submission-Hinweise
Wie wird abgegeben? Welche Unterlagen werden gefordert? Welche Plattform?

## Bewertung fuer FBE
Kurze Einschaetzung (max. 3 Saetze): warum ist das fuer Fluessigboden Engineering relevant - oder warum nicht?

Wenn der HTML-Inhalt offensichtlich Login-/Cookie-Wall ist und nichts Inhaltliches enthaelt, schreibe stattdessen:
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


def render_page(pw, url: str) -> str:
    """Laedt die URL mit Chromium und liefert den extrahierten Text-Inhalt."""
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    try:
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
            locale="de-DE",
        )
        page = ctx.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        page.goto(url, wait_until="networkidle")
        # Cookie-Banner einfach via JS killen (best-effort).
        try:
            page.evaluate(
                "document.querySelectorAll('[id*=cookie i], [class*=cookie i], [class*=consent i]').forEach(e=>e.remove())"
            )
        except Exception:
            pass
        html = page.content()
        return html
    finally:
        browser.close()


def html_to_text(html: str, limit_chars: int = 18000) -> str:
    """Reduziert HTML auf lesbaren Text - LLM-Eingabe-Budget."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "footer", "nav", "header"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    # Mehrfach-Leerzeilen reduzieren
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    text = "\n".join(lines)
    if len(text) > limit_chars:
        text = text[:limit_chars] + "\n[... gekuerzt ...]"
    return text


def llm_extract(tender: dict, text: str) -> tuple[str, str]:
    """Liefert (markdown, summary).

    summary = die ersten ~300 Zeichen der Markdown-Antwort fuer das
    ai_analysis-Feld in der DB.
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


def process_one(pw, tender: dict) -> None:
    tid = tender["id"]
    url = tender.get("url")
    if not url:
        post_result(tid, "", "", ok=False, error="leere URL")
        return
    log.info("[%s] rendere %s", tid, url[:120])
    try:
        html = render_page(pw, url)
    except Exception as exc:
        log.warning("[%s] Render-Fehler: %s", tid, exc)
        post_result(tid, "", "", ok=False, error=f"render: {exc}")
        return

    text = html_to_text(html)
    if len(text) < 200:
        log.info("[%s] zu wenig Inhalt (%d Zeichen) - markiere als Login-Wall", tid, len(text))
        post_result(tid, "", "", ok=False, error=f"thin content ({len(text)}b)")
        return

    try:
        markdown, summary = llm_extract(tender, text)
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
    with sync_playwright() as pw:
        for t in items:
            try:
                process_one(pw, t)
            except Exception as exc:
                log.exception("Unerwarteter Fehler bei tender %s: %s", t.get("id"), exc)
    return len(items)


def main() -> None:
    log.info("Enricher gestartet. FBE=%s, Modell=%s, Intervall=%ds, Batch=%d",
             FBE_URL, OPENAI_MODEL, POLL_INTERVAL_S, BATCH_LIMIT)
    while True:
        try:
            run_once()
        except Exception:
            log.exception("Poll-Run fehlgeschlagen.")
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
