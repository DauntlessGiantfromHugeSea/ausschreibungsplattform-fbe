"""Scraper fuer das Service-Portal des Bundes (service.bund.de).

Standard-Strategie: das oeffentliche Such-Listing aufrufen, das alle
aktuellen Ausschreibungen sortiert nach Veroeffentlichungsdatum zeigt:

    /Content/DE/Ausschreibungen/Suche/Formular.html
        ?resultsPerPage=100&sortOrder=dateOfIssue_dt+desc

Mit jobsrss=true im Querystring liefert service.bund.de denselben
Filter als RSS-Feed - das ist deutlich stabiler als HTML-Scraping.
Der Scraper erkennt den Response-Content-Type automatisch und routet
zu RSS- oder HTML-Parser.

Optional, falls in portals.yaml `enable_keyword_search: true` gesetzt ist,
schicken wir zusaetzlich pro query_term eine templateQueryString-Suche.

Pagination: GP=N (1..max_pages). Wenn eine Seite weniger als 100 Items
liefert, brechen wir ab.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
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
        # Auto-Erkennung RSS vs HTML: bei jobsrss=true liefert
        # service.bund.de XML/RSS, sonst HTML.
        if _looks_like_rss(resp):
            return self.parse_rss(
                resp.content, base_url=self.base_url, portal_name=self.name,
            )
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
    def parse_rss(
        cls, xml_bytes: bytes, base_url: str, portal_name: str = "bund.de",
    ) -> dict[str, TenderItem]:
        """Parst die RSS-Antwort von service.bund.de (jobsrss=true).

        Pro <item>: <title>, <link>, <description>, <pubDate>.
        Beschreibungstext enthaelt typischerweise auch Vergabestelle und
        Ort als Klartext - wir extrahieren beides per Heuristik.
        """
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            log.warning("[bund.de] RSS-Parse-Fehler: %s", exc)
            return {}

        out: dict[str, TenderItem] = {}
        items_xml = root.findall(".//item") or root.findall(
            ".//{http://www.w3.org/2005/Atom}entry"
        )
        for entry in items_xml:
            title_el = entry.find("title")
            if title_el is None:
                title_el = entry.find("{http://www.w3.org/2005/Atom}title")
            if title_el is None or not (title_el.text or "").strip():
                continue
            title = title_el.text.strip()

            link_el = entry.find("link")
            if link_el is not None and link_el.text:
                link = link_el.text.strip()
            else:
                # Atom-Link
                link = None
                for le in entry.findall("{http://www.w3.org/2005/Atom}link"):
                    if le.attrib.get("rel", "alternate") == "alternate":
                        link = le.attrib.get("href")
                        if link:
                            break
            if not link:
                continue
            link = urljoin(base_url, link)

            desc_el = entry.find("description")
            if desc_el is None:
                desc_el = entry.find("{http://www.w3.org/2005/Atom}summary")
            description = (desc_el.text or "").strip() if desc_el is not None else ""

            pub_el = entry.find("pubDate")
            if pub_el is None:
                pub_el = entry.find("{http://www.w3.org/2005/Atom}published")
            pub_date = None
            if pub_el is not None and pub_el.text:
                raw = pub_el.text.strip()
                try:
                    pub_date = parsedate_to_datetime(raw)
                except (TypeError, ValueError):
                    try:
                        pub_date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    except ValueError:
                        pub_date = None

            # Heuristik: 'Vergabestelle: X | Ort: Y | Frist: DD.MM.YYYY' im
            # Description-Text. Wie bei der HTML-Variante.
            authority = None
            location = None
            deadline = None
            full = "{} {}".format(title, description)
            am = re.search(
                r"Vergabestelle\s*[:\-]?\s*(.+?)(?=\s*(?:\||·|Ort|Frist|Veröffentlichung|$))",
                full, re.IGNORECASE,
            )
            if am:
                authority = am.group(1).strip(" .,;|·")
            lm = re.search(
                r"Ort\s*[:\-]?\s*(.+?)(?=\s*(?:\||·|Frist|Vergabestelle|Veröffentlichung|$))",
                full, re.IGNORECASE,
            )
            if lm:
                location = lm.group(1).strip(" .,;|·")
            fm = re.search(r"Frist[^0-9]*(\d{2}\.\d{2}\.\d{4})", full)
            if fm:
                deadline = _parse_de_date(fm.group(1))

            out[link] = TenderItem(
                title=title[:500],
                portal=portal_name,
                url=link,
                contracting_authority=authority,
                location=location,
                region=_guess_region(location or ""),
                publication_date=pub_date,
                deadline=deadline,
                description=description[:1000],
            )
        return out

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


def _looks_like_rss(resp) -> bool:
    """True wenn die Antwort RSS/Atom-XML ist - geprueft via Content-Type
    und durch Sniffing der ersten Bytes des Body."""
    headers = getattr(resp, "headers", None) or {}
    ctype = ""
    if hasattr(headers, "get"):
        ctype = (headers.get("content-type") or "").lower()
    if "xml" in ctype or "rss" in ctype:
        return True
    # Body-Sniffing: erste 512 Bytes auf <?xml oder <rss/<feed
    body = getattr(resp, "content", None) or b""
    if isinstance(body, str):
        body = body.encode("utf-8", "ignore")
    head = body[:512].lstrip()
    if not head:
        return False
    if head.startswith(b"<?xml"):
        return True
    if head.startswith(b"<rss") or head.startswith(b"<feed"):
        return True
    return False


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
