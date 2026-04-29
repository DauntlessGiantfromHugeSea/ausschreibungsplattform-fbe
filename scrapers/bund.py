"""Scraper fuer das Service-Portal des Bundes (service.bund.de).

Die HTML-Suche ist serverseitig gerendert. URL-Schema:
    https://www.service.bund.de/Content/DE/Ausschreibungen/Suche/Formular.html
        ?nn=4641514&resourceId=4641528&input_=4641548
        &pageLocale=de&templateQueryString=<query>&submit.x=0&submit.y=0

Wir bauen einen vereinfachten Aufruf:
    https://www.service.bund.de/Content/DE/Ausschreibungen/Suche/Formular.html
        ?templateQueryString=<query>

Ergebnisse werden als <li class="standard-teaser"> (oder aehnlicher Container)
ausgeliefert. Da das Layout sich aendern kann, fangen wir Parser-Fehler ab.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List
from urllib.parse import urljoin, urlencode

from bs4 import BeautifulSoup

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)


SEARCH_PATH = "/Content/DE/Ausschreibungen/Suche/Formular.html"
DETAIL_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def _parse_de_date(text: str) -> datetime | None:
    if not text:
        return None
    m = DETAIL_DATE_RE.search(text)
    if not m:
        return None
    d, mo, y = m.groups()
    try:
        return datetime(int(y), int(mo), int(d))
    except ValueError:
        return None


class BundScraper(BaseScraper):
    name = "bund.de"

    def fetch(self, terms: List[str]) -> List[TenderItem]:
        results: dict[str, TenderItem] = {}
        for term in terms:
            try:
                results.update(self._search_term(term))
            except Exception as exc:  # pragma: no cover – Netzwerk
                log.warning("[bund.de] Fehler bei '%s': %s", term, exc)
        return list(results.values())

    # ------------------------------------------------------------------
    def _search_term(self, term: str) -> dict[str, TenderItem]:
        params = {"templateQueryString": term, "pageLocale": "de"}
        url = f"{self.base_url}{SEARCH_PATH}?{urlencode(params)}"
        resp = self.get(url)
        if resp.status_code != 200:
            log.info("[bund.de] HTTP %s fuer '%s'", resp.status_code, term)
            return {}
        return self.parse_search_html(resp.text, base_url=self.base_url)

    # ------------------------------------------------------------------
    @classmethod
    def parse_search_html(cls, html: str, base_url: str) -> dict[str, TenderItem]:
        """Extrahiert Trefferliste. Public, damit Tests sie aufrufen koennen."""
        soup = BeautifulSoup(html, "lxml")
        items: dict[str, TenderItem] = {}

        # Verschiedene Layout-Varianten beruecksichtigen.
        candidates = soup.select(
            "li.standard-teaser, li.teaser, div.search-result, article.teaser"
        )
        if not candidates:
            # Fallback: alle <a>, deren href in /Content/DE/Ausschreibungen/Anzeige/ zeigt.
            candidates = []
            for a in soup.find_all("a", href=True):
                if "Ausschreibungen/Anzeige" in a["href"]:
                    candidates.append(a.parent)

        for el in candidates:
            link = el.find("a", href=True)
            if not link:
                continue
            href = urljoin(base_url, link["href"])
            title = link.get_text(strip=True)
            if not title:
                continue

            text = el.get_text(" ", strip=True)
            authority = None
            location = None
            deadline = None
            pub_date = None

            # Heuristiken: Felder werden meist durch '|' oder '·' getrennt.
            field_pattern = lambda label: re.compile(
                rf"{label}\s*[:\-]?\s*(.+?)(?=\s*(?:\||·|$|Ort|Frist|Vergabestelle|Veröffentlichung))",
                re.IGNORECASE,
            )
            auth_match = field_pattern("Vergabestelle").search(text)
            if auth_match:
                authority = auth_match.group(1).strip(" .,;|·")
            loc_match = field_pattern("Ort").search(text)
            if loc_match:
                location = loc_match.group(1).strip(" .,;|·")
            frist_match = re.search(r"Frist[^0-9]*(\d{2}\.\d{2}\.\d{4})", text)
            if frist_match:
                deadline = _parse_de_date(frist_match.group(1))
            pub_match = re.search(r"Veröffentlichung[^0-9]*(\d{2}\.\d{2}\.\d{4})", text)
            if pub_match:
                pub_date = _parse_de_date(pub_match.group(1))

            description = re.sub(r"\s+", " ", text)[:500]

            item = TenderItem(
                title=title,
                portal="bund.de",
                url=href,
                contracting_authority=authority,
                location=location,
                region=_guess_region(location or ""),
                publication_date=pub_date,
                deadline=deadline,
                description=description,
            )
            items[href] = item
        return items


# ---------------------------------------------------------------------------
GERMAN_STATES = [
    "Baden-Württemberg",
    "Bayern",
    "Berlin",
    "Brandenburg",
    "Bremen",
    "Hamburg",
    "Hessen",
    "Mecklenburg-Vorpommern",
    "Niedersachsen",
    "Nordrhein-Westfalen",
    "NRW",
    "Rheinland-Pfalz",
    "Saarland",
    "Sachsen-Anhalt",
    "Sachsen",
    "Schleswig-Holstein",
    "Thüringen",
]


def _guess_region(text: str) -> str | None:
    if not text:
        return None
    for state in GERMAN_STATES:
        if state.lower() in text.lower():
            return "Nordrhein-Westfalen" if state == "NRW" else state
    return None
