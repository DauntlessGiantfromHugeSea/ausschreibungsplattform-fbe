"""Playwright-basierter Scraper fuer JS-gerenderte Portale.

Wenn ein Portal seine Tender erst per JavaScript nachlaedt (Next.js,
React, oder klassische Hash-Fragment-SPAs wie DTVP), reicht httpx
nicht - wir brauchen einen echten Browser. Playwright startet Headless-
Chromium, laedt die Seite vollstaendig, wartet auf das gewuenschte
Element und liefert den fertig gerenderten DOM.

Voraussetzung:
    pip install playwright
    playwright install chromium

YAML-Konfig:

    - name: "DTVP"
      scraper: "playwright_html"
      base_url: "https://www.dtvp.de"
      strategy: "scrape"
      config:
        # Die echte URL inkl. Hash-Fragment (Browser fuehrt es aus).
        # {term} wird URL-encoded eingesetzt.
        search_path: "/Center/common/project/search.do?method=showExtendedSearch&fromExternal=true#{hash}"
        # Optional: Vorlage fuer das Hash-JSON. Falls vorhanden, wird daraus
        # ein base64-encodiertes JSON mit eingesetztem {term} gebaut.
        hash_json:
          searchText: "{term}"
          publicationTypes: ["Tender"]
          contractingRules: ["VOL", "VOB", "VSVGV", "SEKTVO", "OTHER"]
          page: "1"
          sortField: "rank"
        # Auf welches Element wird gewartet, bevor wir HTML lesen?
        wait_for_selector: ".project-list, .search-result, .notice-result, table"
        # Wie lange max. warten?
        wait_timeout_ms: 15000
        # Selektoren wie bei generic_html:
        result_selector: ".project-list-row, .search-result-row, tr.notice"
        title_selector: "a.project-title, a.notice-title, a"
        authority_selector: ".buyer, .vergabestelle"
        location_selector: ".place, .ort"
        deadline_selector: ".deadline, .frist"
        description_selector: ".description, .excerpt"
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, List
from urllib.parse import quote, urljoin

from .base import BaseScraper, TenderItem
from .generic_html import GenericHtmlScraper, _matches_any


log = logging.getLogger(__name__)


class PlaywrightHtmlScraper(BaseScraper):
    name = "playwright"

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

        # Drei Modi:
        #  - search_path mit {term} oder hash_json -> Suchmodus, pro Term ein Goto
        #  - search_path ohne {term} -> Listing-Modus, EIN Goto
        #  - listing_paths: [...] -> Listing-Modus, je Pfad EIN Goto
        #    (search_path und listing_paths koennen kombiniert sein)
        search_path = self.config.get("search_path")
        listing_paths = list(self.config.get("listing_paths") or [])
        if not search_path and not listing_paths:
            log.warning("[%s] search_path bzw. listing_paths fehlen.", self.name)
            return []

        ua = self._client.headers.get("User-Agent", "Mozilla/5.0")
        timeout = int(self.config.get("wait_timeout_ms", 15000))
        wait_for = self.config.get("wait_for_selector")

        items: dict[str, TenderItem] = {}

        # Liste der (path, term)-Tupel, die wir laden sollen.
        plan: list[tuple[str, str | None]] = []
        if search_path:
            if "{term}" in search_path or self.config.get("hash_json"):
                for t in (self.config.get("url_terms") or terms):
                    plan.append((search_path, t))
            else:
                plan.append((search_path, None))
        for lp in listing_paths:
            plan.append((lp, None))

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=bool(self.config.get("headless", True)), args=self.config.get("chromium_args") or ["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(user_agent=ua, locale="de-DE")
            try:
                for path, term in plan:
                    url = self._build_url(path, term)
                    page = context.new_page()
                    try:
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                        except PWTimeout:
                            log.info("[%s] Goto-Timeout %s", self.name, url[:120])
                            continue
                        except Exception as exc:
                            log.info("[%s] Goto-Fehler %s: %s", self.name, url[:120], exc)
                            continue
                        if wait_for:
                            try:
                                page.wait_for_selector(wait_for, timeout=timeout)
                            except PWTimeout:
                                log.info("[%s] wait_for_selector '%s' Timeout - parse trotzdem.",
                                         self.name, wait_for)
                        # Kurz nach networkidle warten (max 3s)
                        try:
                            page.wait_for_load_state("networkidle", timeout=3000)
                        except PWTimeout:
                            pass
                        html = page.content()
                    finally:
                        page.close()

                    parsed = GenericHtmlScraper.parse_html(
                        html, base_url=self.base_url,
                        portal_name=self.name, config=self.config,
                    )
                    if self.config.get("filter_by_terms", True):
                        match_terms = self._match_terms(terms)
                        parsed = {u: it for u, it in parsed.items() if _matches_any(it, match_terms)}
                    log.info("[%s] %d Treffer auf %s (term=%s)", self.name, len(parsed), path[:80], term)
                    items.update(parsed)
            finally:
                context.close()
                browser.close()

        return list(items.values())

    # ------------------------------------------------------------------
    def _match_terms(self, query_terms: List[str]) -> List[str]:
        if "match_terms" in self.config:
            return list(self.config["match_terms"])
        try:
            from backend.search_terms import load_search_config
            return load_search_config().all_terms() or list(query_terms)
        except Exception:
            return list(query_terms)

    # ------------------------------------------------------------------
    def _build_url(self, path: str, term: str | None) -> str:
        # Hash-JSON fuer Hash-Fragment-SPAs (z.B. DTVP, viele Vergabe-Portale)
        hash_template = self.config.get("hash_json")
        if hash_template:
            payload = _substitute(hash_template, term or "")
            encoded = base64.b64encode(
                json.dumps(payload, separators=(",", ":")).encode("utf-8")
            ).decode("ascii").rstrip("=")
            path = path.replace("{hash}", encoded)
        if term is not None and "{term}" in path:
            path = path.replace("{term}", quote(term, safe=""))
        return urljoin(self.base_url + "/", path.lstrip("/"))


# ---------------------------------------------------------------------------
def _substitute(value: Any, term: str) -> Any:
    """Geht rekursiv durch ein dict/list/str und ersetzt {term}-Platzhalter."""
    if isinstance(value, str):
        return value.replace("{term}", term)
    if isinstance(value, list):
        return [_substitute(v, term) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, term) for k, v in value.items()}
    return value
