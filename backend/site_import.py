"""Website-Import in die KI-Wissensbasis.

Crawlt eine Website (nur gleiche Domain), extrahiert den Textinhalt
jeder Seite und schreibt alles als eine Markdown-Datei in die
Wissensbasis (settings.knowledge_dir). Die Datei fliesst damit
automatisch in den Enricher-Prompt und die Claude-Analyse ein.

Gedacht fuer die eigene Firmen-Website (fb-eng.de): Leistungen,
Planungsleistungen, Referenzen, Verfahren - alles, was die KI braucht,
um Ausschreibungen fachlich einzuordnen.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import settings


log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Pfad-Muster, die wir NICHT crawlen (Rechtliches, Feeds, Binaries).
SKIP_PATH_RE = re.compile(
    r"(impressum|datenschutz|agb|cookie|wp-json|wp-admin|feed|xmlrpc|"
    r"\.(pdf|jpg|jpeg|png|gif|svg|webp|zip|mp4|css|js|ico|xml)([?#]|$))",
    re.IGNORECASE,
)

MAX_TEXT_PER_PAGE = 15000


def _clean_text(soup: BeautifulSoup) -> str:
    """Extrahiert lesbaren Text: Navigation/Footer/Skripte raus."""
    for tag in soup(["script", "style", "noscript", "svg", "iframe",
                     "nav", "footer", "form", "button"]):
        tag.decompose()
    # Cookie-/Consent-Container raus
    for el in soup.select("[id*=cookie i], [class*=cookie i], [id*=consent i], [class*=consent i]"):
        el.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    lines = []
    for el in main.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "dt", "dd"]):
        txt = el.get_text(" ", strip=True)
        if not txt or len(txt) < 3:
            continue
        if el.name in ("h1", "h2"):
            lines.append(f"\n## {txt}\n")
        elif el.name in ("h3", "h4"):
            lines.append(f"\n### {txt}\n")
        elif el.name == "li":
            lines.append(f"- {txt}")
        else:
            lines.append(txt)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_TEXT_PER_PAGE:
        text = text[:MAX_TEXT_PER_PAGE] + "\n[... gekuerzt ...]"
    return text


def crawl_site(base_url: str, max_pages: int = 40) -> tuple[int, str]:
    """Crawlt base_url (gleiche Domain, BFS) und schreibt eine Markdown-
    Datei in die Wissensbasis. Liefert (seiten_importiert, dateiname).

    Wirft Exception bei komplettem Fehlschlag (Startseite nicht ladbar).
    """
    base_url = base_url.strip().rstrip("/")
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    host = urlparse(base_url).hostname or ""
    if not host:
        raise ValueError(f"Ungueltige URL: {base_url}")

    client = httpx.Client(
        headers={"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"},
        timeout=20, follow_redirects=True,
    )
    queue: list[str] = [base_url]
    seen: set[str] = set()
    pages: list[tuple[str, str, str]] = []  # (url, title, text)

    try:
        while queue and len(pages) < max_pages:
            url = queue.pop(0)
            norm = url.split("#")[0].rstrip("/")
            if norm in seen:
                continue
            seen.add(norm)
            if SKIP_PATH_RE.search(norm):
                continue
            try:
                resp = client.get(url)
            except httpx.HTTPError as exc:
                log.info("site_import: %s nicht ladbar: %s", url[:80], exc)
                continue
            if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            title = (soup.title.get_text(strip=True) if soup.title else "") or norm
            text = _clean_text(soup)
            if len(text) > 100:
                pages.append((norm, title, text))
                log.info("site_import: %s (%d Zeichen)", norm[:80], len(text))
            # Gleiche-Domain-Links einsammeln
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                full = urljoin(url + "/", href).split("#")[0].rstrip("/")
                p = urlparse(full)
                if p.hostname != host:
                    continue
                if full not in seen and full not in queue and not SKIP_PATH_RE.search(full):
                    queue.append(full)
    finally:
        client.close()

    if not pages:
        raise RuntimeError(
            f"Keine Seiten von {base_url} importierbar (Blockiert? JS-only-Site?)")

    # Markdown zusammensetzen
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    chunks = [
        f"# Website-Import: {host}",
        f"\n> Automatisch importiert am {now} von {base_url} "
        f"({len(pages)} Seiten). Neu importieren ueber Einstellungen → KI-Wissensbasis.\n",
    ]
    for url, title, text in pages:
        chunks.append(f"\n\n---\n\n# {title}\n\n*Quelle: {url}*\n\n{text}")
    md = "\n".join(chunks)

    slug = re.sub(r"[^a-z0-9.-]+", "-", host.lower()).strip("-")
    filename = f"website-{slug}.md"
    out_dir = Path(settings.knowledge_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / filename).write_text(md, encoding="utf-8")
    log.info("site_import: %d Seiten -> %s (%d Zeichen)", len(pages), filename, len(md))
    return len(pages), filename
