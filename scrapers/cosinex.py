"""Scraper fuer Cosinex Vergabemarktplaetze (VMP Satellite & VMP Center).

Cosinex stellt zwei Varianten der gleichen Engine:

* VMP Satellite: Brandenburg, Sachsen-Anhalt, Thueringen, Schleswig-
  Holstein, Mecklenburg-Vorpommern, Berlin u.a.
* VMP Center:    NRW, Rheinland-Pfalz, Saarland, Hamburg u.a.

Beide haben ein oeffentliches Listing der aktuellen Bekanntmachungen
unter Pfaden wie:

    /VMPSatellite/notice
    /VMPSatellite/notice/list
    /VMPCenter/notice
    /VMPCenter/notice/list

Die Such-Endpunkte (`/notice/search?searchTerm=...`) sind oft fuer
authentifizierte Bieter gedacht und liefern fuer anonyme Clients leere
Listen oder einen 302-Redirect zur Login-Seite. Deshalb crawlen wir das
oeffentliche Listing, paginieren und filtern client-seitig auf die
Cluster-Begriffe.

YAML-Konfig (alle Felder optional, sinnvolle Defaults):

    - name: "Vergabe NRW"
      scraper: "cosinex"
      base_url: "https://www.evergabe.nrw.de"
      config:
        variant: "center"           # "center" | "satellite" | leer = autoerkennen
        listing_paths:              # ueberschreibt die Defaults
          - "/VMPCenter/notice"
        max_pages: 3
        request_delay_s: 0.4
        filter_by_terms: true
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Iterable, List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)

DATE_DE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")

DEFAULTS_BY_VARIANT = {
    "satellite": [
        "/VMPSatellite/notice",
        "/VMPSatellite/notice/list",
        "/VMPSatellite/public/notice",
    ],
    "center": [
        "/VMPCenter/notice",
        "/VMPCenter/notice/list",
        "/VMPCenter/public/notice",
    ],
}

# Detail-Link-Muster fuer Cosinex (Notice-IDs / CXP-IDs).
DETAIL_LINK_RE = re.compile(
    r"/notice/(CXP|notice|view|detail|[A-Z0-9]{6,})", re.IGNORECASE,
)

# Treffer-Container - Layout variiert leicht zwischen Satellite/Center und
# einzelnen Portalen, daher mehrere Selektoren mit Fallback.
ROW_SELECTORS = [
    "table.publications tbody tr",
    "table.notice-list tbody tr",
    "table.publications tr.publication",
    "tr.publication",
    "tr.notice",
    "li.notice",
    "li.publication",
    "div.notice-result",
    "div.notice-item",
    "[class*='notice-item']",
    "[class*='publication-item']",
    "article.notice",
]


class CosinexScraper(BaseScraper):
    """Listing-Crawler fuer Cosinex VMP Satellite & Center."""

    name = "cosinex"

    DEFAULT_UA = (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) "
        "Gecko/20100101 Firefox/120.0"
    )

    def __init__(self, *args, **kwargs):
        # Portal-spezifischen UA bevorzugen, sonst Browser-UA.
        config = kwargs.get("config") or {}
        if "user_agent" not in config:
            kwargs["config"] = {**config, "user_agent": self.DEFAULT_UA}
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    def fetch(self, terms: List[str]) -> List[TenderItem]:
        listing_paths = self._listing_paths()
        if not listing_paths:
            log.warning("[%s] Keine Listing-Pfade ermittelbar.", self.name)
            return []

        max_pages = int(self.config.get("max_pages", 3))
        delay = float(self.config.get("request_delay_s", 0.4))
        filter_terms = self.config.get("filter_by_terms", True)
        match_terms = self._match_terms(terms) if filter_terms else []

        items: dict[str, TenderItem] = {}
        for path in listing_paths:
            for page in range(1, max_pages + 1):
                url = self._page_url(path, page)
                try:
                    page_items = self._fetch_listing(url)
                except Exception as exc:  # pragma: no cover - Netzwerk
                    log.warning("[%s] Fehler bei %s: %s", self.name, url, exc)
                    break
                if not page_items:
                    break
                items.update(page_items)
                if delay > 0:
                    time.sleep(delay)
            if items:
                # Erster funktionierender Pfad reicht - andere sind Fallbacks.
                break

        if filter_terms and match_terms:
            items = {u: it for u, it in items.items() if _matches_any(it, match_terms)}

        log.info("[%s] %d Treffer", self.name, len(items))
        return list(items.values())

    # ------------------------------------------------------------------
    def _listing_paths(self) -> list[str]:
        explicit = self.config.get("listing_paths")
        if explicit:
            return list(explicit)
        variant = (self.config.get("variant") or "").strip().lower()
        if variant in DEFAULTS_BY_VARIANT:
            return list(DEFAULTS_BY_VARIANT[variant])
        # Autoerkennung anhand der base_url.
        if "VMPCenter" in self.base_url or "evergabe.nrw" in self.base_url \
                or "vergabe.rlp" in self.base_url \
                or "vergabe.saarland" in self.base_url \
                or "vergabe.hamburg" in self.base_url:
            return list(DEFAULTS_BY_VARIANT["center"])
        # Fuer Satellites + Unbekannt: beide Varianten als Fallback.
        return DEFAULTS_BY_VARIANT["satellite"] + DEFAULTS_BY_VARIANT["center"]

    # ------------------------------------------------------------------
    def _page_url(self, path: str, page: int) -> str:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        if page <= 1:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}page={page}"

    # ------------------------------------------------------------------
    def _fetch_listing(self, url: str) -> dict[str, TenderItem]:
        resp = self.get(url)
        if resp.status_code != 200:
            log.info("[%s] HTTP %s bei %s", self.name, resp.status_code, url)
            return {}
        return self.parse_listing(
            resp.text, base_url=self.base_url, portal_name=self.name,
        )

    # ------------------------------------------------------------------
    @classmethod
    def parse_listing(
        cls, html: str, base_url: str, portal_name: str,
    ) -> dict[str, TenderItem]:
        soup = BeautifulSoup(html, "lxml")

        rows = []
        for sel in ROW_SELECTORS:
            rows = soup.select(sel)
            if rows:
                break

        out: dict[str, TenderItem] = {}

        for row in rows:
            item = cls._parse_row(row, base_url=base_url, portal_name=portal_name)
            if item:
                out[item.url] = item

        # Fallback: alle Notice-Links der Seite einsammeln.
        if not out:
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not _looks_like_notice(href):
                    continue
                title = a.get_text(" ", strip=True)
                if not title or len(title) < 4:
                    continue
                url_full = urljoin(base_url, href)
                out.setdefault(url_full, TenderItem(
                    title=title[:500],
                    portal=portal_name,
                    url=url_full,
                ))

        return out

    # ------------------------------------------------------------------
    @classmethod
    def _parse_row(cls, row, base_url: str, portal_name: str) -> TenderItem | None:
        # Bevorzugt erkennbare Notice-Links.
        link = None
        for a in row.find_all("a", href=True):
            if _looks_like_notice(a["href"]):
                link = a
                break
        if link is None:
            link = row.find("a", href=True)
        if link is None:
            return None

        title = link.get_text(" ", strip=True)
        if not title:
            return None

        full_text = row.get_text(" | ", strip=True)
        url_full = urljoin(base_url, link["href"].strip())

        authority = _extract_field(full_text,
            ["Vergabestelle", "Auftraggeber", "Buyer"])
        location = _extract_field(full_text,
            ["Erfüllungsort", "Erfuellungsort", "Ausführungsort",
             "Ausfuehrungsort", "Ort"])
        deadline = _parse_date(_extract_field(full_text,
            ["Angebotsfrist", "Abgabefrist", "Submission deadline", "Frist"]))
        publication = _parse_date(_extract_field(full_text,
            ["Veröffentlichung", "Veroeffentlichung", "Veröffentlicht",
             "Publication date"]))

        if deadline is None:
            for cell in row.find_all(["td", "span", "div"]):
                d = _parse_date(cell.get_text(" ", strip=True))
                if d:
                    deadline = d
                    break

        return TenderItem(
            title=title[:500],
            portal=portal_name,
            url=url_full,
            contracting_authority=_clean(authority),
            location=_clean(location),
            deadline=deadline,
            publication_date=publication,
            description=full_text[:1000],
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


# ---------------------------------------------------------------------------
LABEL_STOPS = sorted({
    "Vergabestelle", "Auftraggeber", "Erfüllungsort", "Erfuellungsort",
    "Ausführungsort", "Ausfuehrungsort", "Ort",
    "Angebotsfrist", "Abgabefrist", "Frist", "Submission deadline",
    "Veröffentlichung", "Veroeffentlichung", "Veröffentlicht",
    "Publication date", "Auftragsart", "Vergabeart", "CPV",
}, key=len, reverse=True)


def _looks_like_notice(href: str) -> bool:
    if not href:
        return False
    if href.startswith("#") or href.startswith("javascript:"):
        return False
    return bool(DETAIL_LINK_RE.search(href))


def _extract_field(text: str, labels: Iterable[str]) -> str | None:
    if not text:
        return None
    for label in labels:
        stop = "|".join(re.escape(l) for l in LABEL_STOPS if l != label)
        pattern = (
            rf"{re.escape(label)}\s*[:>|]?\s*"
            rf"(.+?)(?=\s*(?:{stop})\s*[:>|]|\s*\||\s*$)"
        )
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            value = m.group(1).strip(" .,;|·:>")
            if value and len(value) < 250:
                return value
    return None


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    v = re.sub(r"\s+", " ", value).strip(" .,;|·:>")
    return v or None


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
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
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
