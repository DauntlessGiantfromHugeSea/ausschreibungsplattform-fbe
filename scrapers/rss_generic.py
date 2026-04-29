"""Generischer RSS/Atom-Scraper.

Konfiguration in portals.yaml:

    - name: "bi-medien Bauausschreibungen"
      enabled: true
      scraper: "rss_generic"
      base_url: "https://www.bi-medien.de"
      strategy: "rss"
      config:
        feed_urls:
          - "https://www.bi-medien.de/feeds/ausschreibungen.xml"
        filter_by_terms: true   # nur Eintraege, die einen Suchbegriff enthalten
        max_items_per_feed: 200

Nutzt nur die Standardbibliothek (xml.etree) – keine zusaetzliche Abhaengigkeit.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import List

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)


class RssGenericScraper(BaseScraper):
    name = "rss"

    def fetch(self, terms: List[str]) -> List[TenderItem]:
        feed_urls = list(self.config.get("feed_urls", []))
        if not feed_urls:
            log.warning("[%s] Keine feed_urls konfiguriert.", self.name)
            return []

        filter_by_terms = bool(self.config.get("filter_by_terms", True))
        max_per_feed = int(self.config.get("max_items_per_feed", 200))
        terms_l = [t.lower() for t in terms]

        items: dict[str, TenderItem] = {}
        for url in feed_urls:
            try:
                items.update(
                    self._read_feed(url, terms_l, filter_by_terms, max_per_feed)
                )
            except Exception as exc:  # pragma: no cover – Netzwerk
                log.warning("[%s] RSS-Fehler %s: %s", self.name, url, exc)
        return list(items.values())

    # ------------------------------------------------------------------
    def _read_feed(
        self,
        url: str,
        terms_l: list[str],
        filter_by_terms: bool,
        max_per_feed: int,
    ) -> dict[str, TenderItem]:
        resp = self.get(url)
        if resp.status_code != 200:
            log.info("[%s] HTTP %s fuer %s", self.name, resp.status_code, url)
            return {}
        return self.parse_feed(resp.content, self.name, terms_l, filter_by_terms, max_per_feed)

    # ------------------------------------------------------------------
    @classmethod
    def parse_feed(
        cls,
        xml_bytes: bytes,
        portal_name: str,
        terms_l: list[str] | None = None,
        filter_by_terms: bool = False,
        max_items: int = 200,
    ) -> dict[str, TenderItem]:
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            log.warning("[%s] XML-Parse-Fehler: %s", portal_name, exc)
            return {}

        # RSS 2.0: <rss><channel><item>...
        # Atom:    <feed><entry>...
        items_xml = root.findall(".//item") or root.findall(
            ".//{http://www.w3.org/2005/Atom}entry"
        )
        out: dict[str, TenderItem] = {}
        for entry in items_xml[:max_items]:
            title = _text(entry, "title") or _text(entry, "{http://www.w3.org/2005/Atom}title")
            link = _link(entry)
            description = (
                _text(entry, "description")
                or _text(entry, "{http://www.w3.org/2005/Atom}summary")
                or _text(entry, "{http://www.w3.org/2005/Atom}content")
            )
            pub_date = _date(entry)

            if not title or not link:
                continue
            if filter_by_terms and terms_l:
                hay = f"{title} {description or ''}".lower()
                if not any(t in hay for t in terms_l):
                    continue

            out[link] = TenderItem(
                title=title.strip(),
                portal=portal_name,
                url=link,
                description=(description or "").strip()[:1000],
                publication_date=pub_date,
            )
        return out


# ---------------------------------------------------------------------------
def _text(el: ET.Element, tag: str) -> str | None:
    found = el.find(tag)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _link(el: ET.Element) -> str | None:
    # RSS 2.0 nutzt <link>...</link>
    rss_link = _text(el, "link")
    if rss_link:
        return rss_link
    # Atom: <link href="..." rel="alternate"/>
    for link_el in el.findall("{http://www.w3.org/2005/Atom}link"):
        rel = link_el.attrib.get("rel", "alternate")
        if rel == "alternate":
            href = link_el.attrib.get("href")
            if href:
                return href
    return None


def _date(el: ET.Element) -> datetime | None:
    raw = (
        _text(el, "pubDate")
        or _text(el, "{http://www.w3.org/2005/Atom}published")
        or _text(el, "{http://www.w3.org/2005/Atom}updated")
        or _text(el, "{http://purl.org/dc/elements/1.1/}date")
    )
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
