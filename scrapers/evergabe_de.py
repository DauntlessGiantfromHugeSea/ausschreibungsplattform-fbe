"""Scraper fuer evergabe.de (Cosinex Next.js).

evergabe.de hat ein Next.js-Tailwind-v4-Frontend mit Client-Side-
Rendering. Statt mit fragilen CSS-Selektoren auf wechselnde
Tailwind-Klassen zu setzen, ist dieser Scraper anchor-zentriert:

  1. Playwright laedt die Seite (rendert JS)
  2. Wir warten auf den ersten <a href='/auftrag/...'> (Hydration durch)
  3. Fuer jeden /auftrag/-Anchor wandern wir den DOM hoch bis zum
     ersten Eltern-Container, der mindestens ein <h2>/<h3>/<title>-
     Element enthaelt - das ist die Card.
  4. Title kommt aus h2/h3, Description aus p, alles in der Card.
  5. Dedup ueber die URL.

Damit ist der Scraper robust gegen Tailwind-Klassen-Aenderungen.
Voraussetzung: pip install playwright && playwright install chromium.
"""
from __future__ import annotations

import logging
import re
import time
from typing import List
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)

DETAIL_LINK_RE = re.compile(r"/auftrag/", re.IGNORECASE)


class EvergabeDeScraper(BaseScraper):
    """Robuster Scraper fuer evergabe.de Tailwind-v4-SPA."""

    name = "evergabe.de"

    DEFAULT_UA = (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) "
        "Gecko/20100101 Firefox/120.0"
    )

    def __init__(self, *args, **kwargs):
        config = kwargs.get("config") or {}
        if "user_agent" not in config:
            kwargs["config"] = {**config, "user_agent": self.DEFAULT_UA}
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    def fetch(self, terms: List[str]) -> List[TenderItem]:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            log.error(
                "[%s] Playwright nicht installiert. Auf der VPS:\n"
                "  source .venv/bin/activate\n"
                "  pip install playwright\n"
                "  playwright install chromium",
                self.name,
            )
            return []

        path = self.config.get(
            "search_path",
            "/auftraege/auftrag-suchen?search%5Bper_page%5D=100"
            "&search%5Bsort_order%5D=newest",
        )
        url = urljoin(self.base_url + "/", path.lstrip("/"))

        ua = self._client.headers.get("User-Agent", self.DEFAULT_UA)
        timeout_ms = int(self.config.get("wait_timeout_ms", 25000))

        items: dict[str, TenderItem] = {}
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(user_agent=ua, locale="de-DE")
                try:
                    page = context.new_page()
                    try:
                        log.info("[%s] GET %s", self.name, url)
                        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                        # Warte bis mindestens ein Auftrag-Anchor da ist =
                        # Hydration ist abgeschlossen.
                        try:
                            page.wait_for_selector(
                                "a[href*='/auftrag/']", timeout=timeout_ms,
                            )
                        except PWTimeout:
                            log.info("[%s] Kein /auftrag/-Anchor nach %dms - "
                                     "evtl. Cloudflare-Block oder Layout-Aenderung.",
                                     self.name, timeout_ms)
                        # Kurz warten dass sekundaere Daten nachgeladen werden.
                        try:
                            page.wait_for_load_state("networkidle", timeout=4000)
                        except PWTimeout:
                            pass
                        html = page.content()
                    finally:
                        page.close()
                finally:
                    context.close()
                    browser.close()
        except Exception as exc:  # pragma: no cover
            log.warning("[%s] Playwright fehlgeschlagen: %s", self.name, exc)
            self.http_log.append({
                "url": url, "status": None, "size": 0,
                "error": "{}: {}".format(type(exc).__name__, str(exc)[:160]),
            })
            return []

        # HTML-Log fuer Diagnose-Sichtbarkeit auf der Status-Seite.
        self.http_log.append({
            "url": url, "status": 200, "size": len(html), "error": None,
        })

        items_dict = self.parse_rendered_html(
            html, base_url=self.base_url, portal_name=self.name,
        )

        if self.config.get("filter_by_terms", True):
            match_terms = self._match_terms(terms)
            items_dict = {
                u: it for u, it in items_dict.items()
                if _matches_any(it, match_terms)
            }

        log.info("[%s] %d Treffer", self.name, len(items_dict))
        return list(items_dict.values())

    # ------------------------------------------------------------------
    @classmethod
    def parse_rendered_html(
        cls, html: str, base_url: str, portal_name: str,
    ) -> dict[str, TenderItem]:
        """Extrahiert Cards anchor-zentriert.

        Strategie: jeder /auftrag/-Anchor zaehlt als eine Card. Wir
        wandern hoch bis zum ersten Eltern, der ein h2/h3 enthaelt -
        das ist der Card-Wrapper. Card-Volltext wird zur Description.
        Mehrere Anchors zur gleichen URL -> dedup ueber URL.
        """
        soup = BeautifulSoup(html, "lxml")
        out: dict[str, TenderItem] = {}

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href or not DETAIL_LINK_RE.search(href):
                continue
            url = urljoin(base_url, href.split("#")[0])
            if url in out:
                continue

            # Eltern-Container finden, der das Title-Element enthaelt.
            card = cls._find_card_container(anchor)
            if card is None:
                continue

            # Titel aus h2/h3 extrahieren, sonst Anchor-Text, sonst Fallback.
            title = None
            for tag in ("h2", "h3", "h1"):
                el = card.find(tag)
                if el and el.get_text(strip=True):
                    title = el.get_text(" ", strip=True)
                    break
            if not title:
                title = anchor.get_text(" ", strip=True)
            if not title:
                continue
            if len(title) < 4:
                continue

            description = ""
            p = card.find("p")
            if p:
                description = p.get_text(" ", strip=True)
            if not description:
                description = card.get_text(" ", strip=True)
                # Nur die ersten 500 Zeichen als Description-Snippet.
                description = description[:500]

            # Heuristisch: wenn der Card-Volltext 'Angebotsfrist:' / 'Frist:'
            # enthaelt, daraus DD.MM.YYYY ziehen.
            full_text = card.get_text(" ", strip=True)
            deadline = _parse_deadline(full_text)

            # Lokationen aus dem Volltext heuristisch (PLZ + Ort).
            location = _extract_location(full_text, title)

            out[url] = TenderItem(
                title=title[:500],
                portal=portal_name,
                url=url,
                location=location,
                deadline=deadline,
                description=description[:1000],
            )

        return out

    @staticmethod
    def _find_card_container(anchor):
        """Wandert vom Anchor hoch bis zum ersten Vorfahren, der ein h2/h3
        enthaelt. Bricht spaetestens nach 8 Ebenen ab (Heuristik gegen das
        Hochlaufen bis <body>)."""
        cur = anchor.parent
        for _ in range(8):
            if cur is None or getattr(cur, "name", None) in {"body", "html", None}:
                return None
            if cur.find(["h2", "h3", "h1"]):
                return cur
            cur = cur.parent
        return None

    # ------------------------------------------------------------------
    def _match_terms(self, query_terms: List[str]) -> List[str]:
        if "match_terms" in self.config:
            return list(self.config["match_terms"])
        try:
            from backend.search_terms import load_search_config
            return load_search_config().all_terms() or list(query_terms)
        except Exception:
            return list(query_terms)


# ---------------------------------------------------------------------------
DATE_DE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
PLZ_RE = re.compile(r"\b(\d{5})\s+([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß \-/]+)")


def _parse_deadline(text: str):
    """Erstes 'Angebotsfrist|Frist|Eingangsfrist' DD.MM.YYYY."""
    if not text:
        return None
    from datetime import datetime
    # Bevorzugt nach 'frist'-Markern
    m = re.search(
        r"(?:Angebotsfrist|Eingangsfrist|Frist|Abgabefrist)[^0-9]*?"
        r"(\d{2})\.(\d{2})\.(\d{4})",
        text, re.IGNORECASE,
    )
    if not m:
        return None
    d, mo, y = m.groups()
    try:
        return datetime(int(y), int(mo), int(d))
    except ValueError:
        return None


def _extract_location(full_text: str, title: str) -> str | None:
    """Sucht zuerst PLZ+Ort im Title, dann im Volltext."""
    for hay in (title or "", full_text or ""):
        m = PLZ_RE.search(hay)
        if m:
            return "{} {}".format(m.group(1), m.group(2).strip())
    # Fallback: Pattern 'in 12345 Ort' oder 'Ausfuehrungsort: 12345 Ort'
    return None


def _matches_any(item: TenderItem, terms) -> bool:
    hay = " ".join([
        item.title or "",
        item.description or "",
        item.contracting_authority or "",
        item.location or "",
    ]).lower()
    if not hay:
        return False
    for t in terms:
        if t and t.lower() in hay:
            return True
    return False
