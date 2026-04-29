"""Generischer HTML-Scraper, ueber YAML konfigurierbar.

Konfiguration in portals.yaml:

    - name: "Vergabeplattform NRW"
      enabled: false
      scraper: "generic_html"
      base_url: "https://www.evergabe.nrw.de"
      strategy: "search_url"
      config:
        # Pfad mit {term}-Platzhalter, der mit jedem query_terms-Eintrag
        # ersetzt wird.
        search_path: "/VMPCenter/notice/search?query={term}"
        # CSS-Selektor, der einen Treffer (Block) auswaehlt.
        result_selector: "div.notice-result"
        # Selektor relativ zum Treffer:
        title_selector: "a.notice-title"
        link_selector: "a.notice-title"      # default: title_selector
        authority_selector: ".buyer"
        location_selector: ".place"
        deadline_selector: ".deadline"
        publication_selector: ".published"
        description_selector: ".excerpt"

Wenn ein Selector nicht angegeben ist, wird das Feld leer gelassen.
Datumsfelder werden als 'TT.MM.JJJJ' oder ISO erwartet.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)

DATE_DE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


class GenericHtmlScraper(BaseScraper):
    name = "generic_html"

    def fetch(self, terms: List[str]) -> List[TenderItem]:
        path = self.config.get("search_path")
        if not path:
            log.warning("[%s] search_path fehlt in der Konfiguration.", self.name)
            return []

        items: dict[str, TenderItem] = {}
        for term in terms:
            try:
                items.update(self._search(term, path))
            except Exception as exc:  # pragma: no cover – Netzwerk
                log.warning("[%s] Fehler bei '%s': %s", self.name, term, exc)
        return list(items.values())

    # ------------------------------------------------------------------
    def _search(self, term: str, search_path: str) -> dict[str, TenderItem]:
        url = self.base_url + search_path.replace("{term}", term)
        resp = self.get(url)
        if resp.status_code != 200:
            log.info("[%s] HTTP %s bei %s", self.name, resp.status_code, url)
            return {}
        return self.parse_html(resp.text, self.base_url, self.name, self.config)

    # ------------------------------------------------------------------
    @classmethod
    def parse_html(
        cls,
        html: str,
        base_url: str,
        portal_name: str,
        config: dict,
    ) -> dict[str, TenderItem]:
        soup = BeautifulSoup(html, "lxml")

        result_sel = config.get("result_selector")
        if not result_sel:
            return {}

        title_sel = config.get("title_selector", "a")
        link_sel = config.get("link_selector", title_sel)
        auth_sel = config.get("authority_selector")
        loc_sel = config.get("location_selector")
        deadline_sel = config.get("deadline_selector")
        pub_sel = config.get("publication_selector")
        desc_sel = config.get("description_selector")

        out: dict[str, TenderItem] = {}
        for el in soup.select(result_sel):
            title_el = el.select_one(title_sel)
            link_el = el.select_one(link_sel)
            if not title_el or not link_el:
                continue
            href = link_el.get("href", "").strip()
            if not href:
                continue
            url_full = urljoin(base_url, href)
            title = title_el.get_text(" ", strip=True)
            if not title:
                continue

            tender = TenderItem(
                title=title,
                portal=portal_name,
                url=url_full,
                contracting_authority=_text(el, auth_sel),
                location=_text(el, loc_sel),
                deadline=_parse_date(_text(el, deadline_sel)),
                publication_date=_parse_date(_text(el, pub_sel)),
                description=(_text(el, desc_sel) or el.get_text(" ", strip=True))[:500],
            )
            out[url_full] = tender
        return out


# ---------------------------------------------------------------------------
def _text(parent, selector: str | None) -> str | None:
    if not selector:
        return None
    el = parent.select_one(selector)
    if not el:
        return None
    return el.get_text(" ", strip=True) or None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    m = DATE_DE.search(value)
    if m:
        d, mo, y = m.groups()
        try:
            return datetime(int(y), int(mo), int(d))
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
