"""Scraper fuer das Service-Portal des Bundes (service.bund.de).

Standard-Strategie: das oeffentliche Such-Listing aufrufen, das alle
aktuellen Ausschreibungen sortiert nach Veroeffentlichungsdatum zeigt:

    /Content/DE/Ausschreibungen/Suche/Formular.html
        ?resultsPerPage=100&sortOrder=dateOfIssue_dt+desc

Das liefert mit einem Request 100 frische Bekanntmachungen. Wir filtern
client-seitig auf die konfigurierten Cluster-Begriffe.

Optional, falls in portals.yaml `enable_keyword_search: true` gesetzt ist,
schicken wir zusaetzlich pro query_term eine templateQueryString-Suche.

Pagination: GP=N (1..max_pages). Wenn eine Seite weniger als 100 Items
liefert, brechen wir ab.

Teaser-Layout: <li class="standard-teaser"> oder Varianten. Der Titel-Link
zeigt auf /Content/DE/Ausschreibungen/Anzeige/<id>.html - wir bevorzugen
diesen Link statt des ersten Links in der Karte (sonst landen wir auf
"merken"- oder Pagination-Links).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Iterable, List
from urllib.parse import urljoin, urlencode

from bs4 import BeautifulSoup

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)


SEARCH_PATH = "/Content/DE/Ausschreibungen/Suche/Formular.html"
DETAIL_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
DETAIL_LINK_RE = re.compile(r"/Content/DE/Ausschreibungen/Anzeige/", re.IGNORECASE)


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

    DEFAULT_LISTING_PARAMS = {
        "resultsPerPage": "100",
        "sortOrder": "dateOfIssue_dt desc",
    }

    # ------------------------------------------------------------------
    def fetch(self, terms: List[str]) -> List[TenderItem]:
        results: dict[str, TenderItem] = {}

        # 1) Listing-Modus: ein paar Seiten neueste Bekanntmachungen.
        max_pages = int(self.config.get("max_pages", 2))
        delay = float(self.config.get("request_delay_s", 0.4))
        for page in range(1, max_pages + 1):
            try:
                page_items = self._fetch_listing(page=page)
            except Exception as exc:  # pragma: no cover - Netzwerk
                log.warning("[bund.de] Listing page %d fehlgeschlagen: %s", page, exc)
                break
            if not page_items:
                break
            results.update(page_items)
            if delay > 0:
                time.sleep(delay)

        # 2) Optional: zusaetzlich per Suchbegriff suchen.
        if self.config.get("enable_keyword_search", True):
            for term in terms:
                try:
                    results.update(self._search_term(term))
                except Exception as exc:  # pragma: no cover - Netzwerk
                    log.warning("[bund.de] Suche '%s' fehlgeschlagen: %s", term, exc)
                if delay > 0:
                    time.sleep(delay)

        # 3) Client-seitiger Filter auf Cluster-Begriffe (nur, wenn Listing
        #    Treffer gebracht hat - die Keyword-Suche filtert schon serverseitig).
        if self.config.get("filter_by_terms", True):
            match_terms = self._match_terms(terms)
            results = {u: it for u, it in results.items()
                       if _matches_any(it, match_terms)}

        log.info("[bund.de] %d Treffer", len(results))
        return list(results.values())

    # ------------------------------------------------------------------
    def _fetch_listing(self, page: int = 1) -> dict[str, TenderItem]:
        params = dict(self.DEFAULT_LISTING_PARAMS)
        # Erlaubt YAML-Override (z.B. resultsPerPage=200, anderes nn).
        for k, v in (self.config.get("listing_params") or {}).items():
            params[k] = str(v)
        if page > 1:
            params["GP"] = str(page)
        url = "{}{}?{}".format(self.base_url, SEARCH_PATH, urlencode(params))
        resp = self.get(url)
        if resp.status_code != 200:
            log.info("[bund.de] Listing HTTP %s page %d", resp.status_code, page)
            return {}
        return self.parse_search_html(
            resp.text, base_url=self.base_url, portal_name=self.name,
        )

    # ------------------------------------------------------------------
    def _search_term(self, term: str) -> dict[str, TenderItem]:
        params = {
            "templateQueryString": term,
            "pageLocale": "de",
            "resultsPerPage": "50",
            "sortOrder": "dateOfIssue_dt desc",
        }
        url = "{}{}?{}".format(self.base_url, SEARCH_PATH, urlencode(params))
        resp = self.get(url)
        if resp.status_code != 200:
            log.info("[bund.de] HTTP %s fuer '%s'", resp.status_code, term)
            return {}
        return self.parse_search_html(
            resp.text, base_url=self.base_url, portal_name=self.name,
        )

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
    @classmethod
    def parse_search_html(
        cls, html: str, base_url: str, portal_name: str = "bund.de",
    ) -> dict[str, TenderItem]:
        """Extrahiert Trefferliste. Public, damit Tests sie aufrufen koennen."""
        soup = BeautifulSoup(html, "lxml")
        items: dict[str, TenderItem] = {}

        candidates = soup.select(
            "li.standard-teaser, li.teaser, div.search-result, "
            "article.teaser, div.standard-teaser, [class*='standard-teaser']"
        )
        if not candidates:
            # Fallback: jedes Element, das einen Anzeige-Link enthaelt.
            seen: set[int] = set()
            for a in soup.find_all("a", href=True):
                if not DETAIL_LINK_RE.search(a["href"]):
                    continue
                # Naechster sinnvoller Vorfahre, der mehr Kontext hat als nur das <a>.
                container = a.find_parent(["li", "article", "div"])
                if container is None:
                    container = a
                if id(container) in seen:
                    continue
                seen.add(id(container))
                candidates.append(container)

        for el in candidates:
            # Bevorzugt der Anzeige-Link - sonst der erste Link.
            link = None
            for a in el.find_all("a", href=True):
                if DETAIL_LINK_RE.search(a["href"]):
                    link = a
                    break
            if link is None:
                link = el.find("a", href=True)
            if link is None:
                continue

            href = urljoin(base_url, link["href"].strip())
            title = link.get_text(" ", strip=True)
            # "merken"-, "drucken"- oder Pagination-Links rausfiltern.
            if not title or len(title) < 6:
                continue
            if title.lower() in {"merken", "drucken", "weiter", "zurueck", "zurück"}:
                continue

            text = el.get_text(" ", strip=True)
            authority = None
            location = None
            deadline = None
            pub_date = None

            field_pattern = lambda label: re.compile(
                rf"{label}\s*[:\-]?\s*(.+?)(?=\s*(?:\||·|$|Ort|Frist|Vergabestelle|Veröffentlichung|Veroeffentlichung|Auftragsart))",
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
            pub_match = re.search(
                r"Ver(?:ö|oe)ffentlichung[^0-9]*(\d{2}\.\d{2}\.\d{4})", text,
            )
            if pub_match:
                pub_date = _parse_de_date(pub_match.group(1))

            description = re.sub(r"\s+", " ", text)[:500]

            items[href] = TenderItem(
                title=title[:500],
                portal=portal_name,
                url=href,
                contracting_authority=authority,
                location=location,
                region=_guess_region(location or ""),
                publication_date=pub_date,
                deadline=deadline,
                description=description,
            )
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


def _matches_any(item: TenderItem, terms: Iterable[str]) -> bool:
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
