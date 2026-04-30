"""Scraper fuer evergabe.sachsen.de (NetServer / AI Informatics).

Die Plattform ist Java-basiert und stellt eine serverseitige Suche bereit:

    /NetServer/PublicationSearchControllerServlet
        ?function=SearchPublications
        &Category=InvitationToTender
        &thContext=publications
        &Search.SearchPattern=<term>      (optional, Volltextsuche)
        &Page=<n>                          (optional, Pagination)

Die Ergebnisseite ist server-rendered HTML mit einer Treffer-Tabelle bzw.
einer Liste von Publikationen. Jeder Treffer hat einen Link in der Form

    PublicationControllerServlet?function=Show...&PublicationID=...

Strategie:

1. Pro Suchbegriff einen Request mit Search.SearchPattern absetzen.
2. Trefferliste parsen (mehrere Selektor-Fallbacks fuer Layout-Varianten).
3. Pagination ueber Page=2..N folgen, solange neue Treffer kommen.
4. Wenn die Suche pro Term leer ist, einmal das ungefilterte Listing der
   aktuellen InvitationToTender-Bekanntmachungen holen und client-seitig
   gegen alle Cluster-Begriffe filtern.
5. Optional Detail-Seiten fuer reichere Felder besuchen (max_details).

Robust gegen Layout-Aenderungen: Selektoren sind als CSS-Listen mit Fallbacks
formuliert; wenn die Tabelle nicht erkannt wird, faellt der Scraper auf das
Einsammeln aller Publikations-Links der Seite zurueck.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Iterable, List
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)

DATE_DE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")

DEFAULT_SEARCH_PATH = (
    "/NetServer/PublicationSearchControllerServlet"
    "?function=SearchPublications"
    "&Category=InvitationToTender"
    "&thContext=publications"
)

# Detail-Seiten der NetServer-Plattform: Publication*Servlet mit PublicationID
DETAIL_LINK_RE = re.compile(
    r"PublicationControllerServlet|PublicationDisplay|PublicationID=",
    re.IGNORECASE,
)

ROW_SELECTORS = [
    "table.searchResults tbody tr",
    "table.publicationList tbody tr",
    "table.results tbody tr",
    "table tbody tr.publication",
    "tr.publication",
    "tr.searchResultRow",
    "li.publicationItem",
    "div.publicationItem",
    "div.searchResult",
    "article.publication",
]


class SachsenScraper(BaseScraper):
    name = "evergabe.sachsen.de"

    # NetServer reagiert empfindlich auf Bot-User-Agents. Default: Browser-UA.
    DEFAULT_UA = (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) "
        "Gecko/20100101 Firefox/120.0"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ua = self.config.get("user_agent") or self.DEFAULT_UA
        self._client.headers["User-Agent"] = ua
        self._client.headers.setdefault(
            "Accept",
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        self._client.headers.setdefault("Accept-Language", "de-DE,de;q=0.9,en;q=0.7")

    # ------------------------------------------------------------------
    def fetch(self, terms: List[str]) -> List[TenderItem]:
        search_path = self.config.get("search_path", DEFAULT_SEARCH_PATH)
        max_pages = int(self.config.get("max_pages", 3))
        delay = float(self.config.get("request_delay_s", 0.4))
        max_details = int(self.config.get("max_details", 0))

        items: dict[str, TenderItem] = {}

        # 1) Pro Suchbegriff serverseitig suchen.
        for term in terms:
            for page in range(1, max_pages + 1):
                url = self._search_url(search_path, term=term, page=page)
                try:
                    page_items = self._fetch_listing(url)
                except Exception as exc:  # pragma: no cover - Netzwerk
                    log.warning("[%s] Fehler bei '%s' (page %d): %s",
                                self.name, term, page, exc)
                    break
                if not page_items:
                    break
                items.update(page_items)
                if delay > 0:
                    time.sleep(delay)

        # 2) Fallback: ungefiltertes Listing + client-seitiger Term-Filter.
        if not items:
            log.info("[%s] Suche per Term leer - greife auf Listing zurueck.", self.name)
            match_terms = self._match_terms(terms)
            for page in range(1, max_pages + 1):
                url = self._search_url(search_path, term=None, page=page)
                try:
                    page_items = self._fetch_listing(url)
                except Exception as exc:  # pragma: no cover
                    log.warning("[%s] Listing fehlgeschlagen page %d: %s",
                                self.name, page, exc)
                    break
                if not page_items:
                    break
                for u, it in page_items.items():
                    if _matches_any(it, match_terms):
                        items[u] = it
                if delay > 0:
                    time.sleep(delay)

        # 3) Optional Detail-Seiten anreichern.
        if max_details > 0 and items:
            for url in list(items.keys())[:max_details]:
                try:
                    self._enrich_detail(items[url])
                except Exception as exc:  # pragma: no cover
                    log.debug("[%s] Detail enrich %s: %s", self.name, url, exc)
                if delay > 0:
                    time.sleep(delay)

        log.info("[%s] %d Treffer", self.name, len(items))
        return list(items.values())

    # ------------------------------------------------------------------
    def _search_url(self, search_path: str, term: str | None, page: int) -> str:
        url = urljoin(self.base_url + "/", search_path.lstrip("/"))
        sep = "&" if "?" in url else "?"
        params: list[str] = []
        if term:
            params.append(f"Search.SearchPattern={quote(term, safe='')}")
        if page > 1:
            params.append(f"Page={page}")
        if params:
            url = f"{url}{sep}{'&'.join(params)}"
        return url

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

        # Zeilen-basierte Erkennung mit mehreren Fallback-Selektoren.
        rows = []
        for sel in ROW_SELECTORS:
            rows = soup.select(sel)
            if rows:
                break

        out: dict[str, TenderItem] = {}

        if rows:
            for row in rows:
                item = cls._parse_row(row, base_url=base_url, portal_name=portal_name)
                if item:
                    out[item.url] = item

        # Letzter Fallback: jeden Detail-Link der Seite einsammeln.
        if not out:
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not DETAIL_LINK_RE.search(href):
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
        link = None
        for a in row.find_all("a", href=True):
            if DETAIL_LINK_RE.search(a["href"]):
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

        # Wenn keine Label-Treffer: irgendein Datum in der Zeile als Frist.
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
    def _enrich_detail(self, item: TenderItem) -> None:
        resp = self.get(item.url)
        if resp.status_code != 200:
            return
        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text(" ", strip=True)
        if not item.contracting_authority:
            item.contracting_authority = _clean(_extract_field(
                text, ["Vergabestelle", "Auftraggeber"]))
        if not item.location:
            item.location = _clean(_extract_field(
                text, ["Erfüllungsort", "Erfuellungsort", "Ausführungsort",
                       "Ausfuehrungsort", "Ort"]))
        if not item.deadline:
            item.deadline = _parse_date(_extract_field(
                text, ["Angebotsfrist", "Abgabefrist", "Frist"]))
        if not item.publication_date:
            item.publication_date = _parse_date(_extract_field(
                text, ["Veröffentlichung", "Veröffentlicht"]))
        if not item.description or len(item.description) < 200:
            item.description = text[:1500]

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
