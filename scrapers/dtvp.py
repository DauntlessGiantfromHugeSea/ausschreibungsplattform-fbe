"""DTVP-spezifischer Scraper.

Healy-Hudson SPA mit base64-encoded Hash-Fragment. Wir rendern mit
Playwright (Xvfb, headless=False), greifen <a.noTextDecorationLink>-
Anchors und ziehen Titel aus aria-label/title-Attribut.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from typing import List

from bs4 import BeautifulSoup

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)
_DATE_DE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def _parse_date(s: str | None):
    if not s:
        return None
    m = _DATE_DE.search(s)
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


class DtvpScraper(BaseScraper):
    name = "dtvp"

    def fetch(self, terms: List[str]) -> List[TenderItem]:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            log.error("[%s] Playwright nicht installiert.", self.name)
            return []

        url_terms = self.config.get("url_terms") or list(terms)
        items: dict[str, TenderItem] = {}

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=bool(self.config.get("headless", False)),
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent=self.config.get(
                    "user_agent",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36",
                ),
                locale="de-DE",
                viewport={"width": 1920, "height": 1080},
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )

            try:
                for term in url_terms:
                    url = self._build_url(term)
                    page = ctx.new_page()
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        try:
                            page.wait_for_selector(
                                "a[href*='projectForwarding.do']", timeout=20000
                            )
                        except PWTimeout:
                            log.info("[%s] keine Treffer fuer term=%s", self.name, term)
                            continue
                        try:
                            page.wait_for_load_state("networkidle", timeout=3000)
                        except PWTimeout:
                            pass
                        html = page.content()
                    finally:
                        page.close()

                    parsed = self._parse(html)
                    log.info("[%s] %d Treffer fuer term=%s", self.name, len(parsed), term)
                    items.update(parsed)
            finally:
                ctx.close()
                browser.close()

        if self.config.get("filter_by_terms", True):
            match_terms = self._match_terms(terms)
            if match_terms:
                ml = [t.lower() for t in match_terms if t]
                items = {
                    u: it for u, it in items.items()
                    if any(
                        t in (it.title or "").lower()
                        or t in (it.description or "").lower()
                        for t in ml
                    )
                }

        return list(items.values())

    def _match_terms(self, terms):
        if "match_terms" in self.config:
            return list(self.config["match_terms"])
        try:
            from backend.search_terms import load_search_config
            return load_search_config().all_terms() or list(terms)
        except Exception:
            return list(terms)

    def _build_url(self, term: str) -> str:
        hash_template = self.config.get("hash_json")
        path = self.config.get("search_path", "")
        if hash_template:
            payload = json.loads(
                json.dumps(hash_template, ensure_ascii=False).replace("{term}", term)
            )
            encoded = (
                base64.b64encode(
                    json.dumps(payload, separators=(",", ":")).encode("utf-8")
                )
                .decode("ascii")
                .rstrip("=")
            )
            path = path.replace("{hash}", encoded)
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def _parse(self, html: str) -> dict[str, TenderItem]:
        out: dict[str, TenderItem] = {}
        soup = BeautifulSoup(html, "lxml")
        anchors = soup.select(
            "a.noTextDecorationLink[href*='projectForwarding.do']"
        )
        for a in anchors:
            href = (a.get("href") or "").strip()
            if "projectForwarding.do" not in href:
                continue

            title = (
                (a.get("aria-label") or "")
                .replace(
                    "Informationen werden in einem neuen Tab geöffnet ", ""
                )
                .strip()
                or (a.get("title") or "").strip()
                or a.get_text(strip=True)
            )
            if not title or len(title) < 5:
                continue

            row = a.find_parent("tr")
            cells = [td.get_text(strip=True) for td in row.find_all("td")] if row else []
            publication = cells[0] if len(cells) > 0 else ""
            deadline = cells[1] if len(cells) > 1 else ""
            ttype = cells[3] if len(cells) > 3 else ""
            publisher = cells[4] if len(cells) > 4 else ""

            description = ttype
            if publisher:
                description = f"{ttype} – Vergabestelle: {publisher}".strip(" –")

            out[href] = TenderItem(
                title=title[:500],
                portal=self.name,
                url=href,
                contracting_authority=(publisher or None),
                location=None,
                deadline=_parse_date(deadline),
                publication_date=_parse_date(publication),
                description=description[:1000],
            )
        return out
