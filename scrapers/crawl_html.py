"""Crawl-Scraper: folgt Listings, liest Detail-Seiten, filtert per Volltext.

Im Gegensatz zu `generic_html` (das nur Such-URLs aufruft und die Trefferliste
parst) macht dieser Scraper das, was viele Vergabeportale erwarten:

  1. Eine oder mehrere Listing-Seiten holen (z.B. /auftraege, /notices/list).
  2. Alle Links zu Detail-Seiten extrahieren (CSS-Selektor + optional Regex).
  3. Jede Detail-Seite einzeln laden.
  4. Den Volltext der Detail-Seite gegen die Suchbegriffe pruefen.
  5. Nur Treffer behalten, die mindestens einen Suchbegriff enthalten.

YAML-Konfig:

    - name: "evergabe.de"
      scraper: "crawl_html"
      base_url: "https://www.evergabe.de"
      strategy: "scrape"
      config:
        listing_paths:
          - "/auftraege"
          - "/auftraege?p=2"
          - "/auftraege?p=3"
        link_selector: "a[href*='/auftrag/']"
        link_pattern: "/auftrag/[0-9]+"      # optional, regex auf URL
        max_details: 50
        request_delay_s: 0.5
        # Optional: Felder auf der Detail-Seite
        detail_title_selector: "h1, .title"
        detail_authority_selector: ".buyer, .vergabestelle"
        detail_location_selector: ".place, .ort"
        detail_deadline_selector: ".deadline, .frist"
        detail_publication_selector: ".published"
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


class CrawlHtmlScraper(BaseScraper):
    name = "crawl"

    # ------------------------------------------------------------------
    def fetch(self, terms: List[str]) -> List[TenderItem]:
        # Crawl-Modus matched gegen ALLE Cluster-Begriffe (nicht nur die
        # query_terms, die fuer Portal-side-Search vorgesehen sind).
        match_terms = self._match_terms(terms)
        listing_paths = self.config.get("listing_paths") or []
        if not listing_paths:
            log.warning("[%s] keine listing_paths konfiguriert.", self.name)
            return []

        max_details = int(self.config.get("max_details", 50))
        delay = float(self.config.get("request_delay_s", 0.5))

        # 1) Detail-URLs einsammeln
        detail_urls: list[str] = []
        for path in listing_paths:
            detail_urls.extend(self._discover_links(path))
        # dedup unter Erhalt der Reihenfolge
        seen: set[str] = set()
        unique = [u for u in detail_urls if not (u in seen or seen.add(u))]
        unique = unique[:max_details]
        log.info("[%s] %d eindeutige Detail-URLs (%d Listings)", self.name, len(unique), len(listing_paths))

        # 2) Detail-Seiten holen + filtern
        items: dict[str, TenderItem] = {}
        for url in unique:
            try:
                item = self._fetch_detail(url, match_terms)
                if item:
                    items[url] = item
            except Exception as exc:  # pragma: no cover – Netzwerk
                log.warning("[%s] Fehler bei Detail %s: %s", self.name, url, exc)
            if delay > 0:
                time.sleep(delay)

        log.info("[%s] %d Treffer nach Volltext-Filter", self.name, len(items))
        return list(items.values())

    # ------------------------------------------------------------------
    def _match_terms(self, query_terms: List[str]) -> List[str]:
        """Welche Begriffe muessen im Volltext erscheinen?

        Standard: alle Cluster-Begriffe (umfassender als query_terms).
        Wenn `config.match_terms` angegeben ist, ueberschreibt das.
        """
        from backend.search_terms import load_search_config
        if "match_terms" in self.config:
            return list(self.config["match_terms"])
        return load_search_config().all_terms() or list(query_terms)

    # ------------------------------------------------------------------
    def _discover_links(self, listing_path: str) -> list[str]:
        url = urljoin(self.base_url + "/", listing_path.lstrip("/"))
        try:
            resp = self.get(url)
        except Exception as exc:
            log.warning("[%s] Listing %s fehlgeschlagen: %s", self.name, url, exc)
            return []
        if resp.status_code != 200:
            log.info("[%s] Listing HTTP %s: %s", self.name, resp.status_code, url)
            return []
        return self._extract_links(resp.text, base_url=self.base_url, config=self.config)

    @classmethod
    def _extract_links(cls, html: str, base_url: str, config: dict) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        link_sel = config.get("link_selector", "a[href]")
        pattern = config.get("link_pattern")
        regex = re.compile(pattern) if pattern else None

        out: list[str] = []
        for a in soup.select(link_sel):
            href = a.get("href", "").strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            full = urljoin(base_url, href)
            if regex and not regex.search(full):
                continue
            out.append(full)

        # Aggressiver Fallback: wenn der konfigurierte Selektor 0 Links bringt,
        # akzeptiere jeden internen Anchor mit plausiblem Bekanntmachungs-Pfad
        # ODER mit langem sichtbaren Linktext (echte Tender-Titel haben 30+
        # Zeichen, Menue-Eintraege < 25). Pattern nur auf Path+Query, weil
        # Hostnames wie 'evergabe-online.de' selbst schon 'vergabe' enthalten.
        if not out and config.get("aggressive_fallback", True):
            from urllib.parse import urlsplit
            broad = re.compile(
                r"(/notice|/publication|/bekanntmachung|/ausschreibung|"
                r"/auftrag|/vergabe|/tender|/announcement|/[0-9]{5,}|"
                r"[?&]id=[A-Za-z0-9])",
                re.IGNORECASE,
            )
            asset_re = re.compile(
                r"\.(css|js|png|jpe?g|gif|svg|ico|woff2?|ttf|pdf)(\?|$)",
                re.IGNORECASE,
            )
            junk_titles = {"hier", "weiter", "zurueck", "zurück", "merken",
                           "drucken", "details", "mehr", "anmelden",
                           "login", "kontakt", "impressum", "home",
                           "datenschutz", "barrierefreiheit", "agb",
                           "haftungsausschluss", "newsletter"}
            # Pfad-Praefixe, die typisch fuer Navigation/Verwaltungs-Seiten
            # sind, nicht fuer Bekanntmachungen.
            nav_path_prefixes = re.compile(
                r"^/(?:vergabestellen|informationen-fuer-bieter|"
                r"informationen|geltende-regelungen|service|hilfe|"
                r"kontakt|impressum|datenschutz|barrierefreiheit|"
                r"agb|home|startseite|ueber-uns|wir-ueber-uns|"
                r"staatskanzlei|ministerium|ministerien)",
                re.IGNORECASE,
            )
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    continue
                if href.startswith("http") and not href.startswith(base_url):
                    continue
                # Skip wenn der Anchor in einem Navigations-/Footer-Container
                # liegt. Echte Bekanntmachungen liegen in <main>/<table>/
                # <article>, nicht in <nav>/<footer>/<header>/<aside>.
                if _in_navigation_context(a):
                    continue
                full = urljoin(base_url, href)
                parts = urlsplit(full)
                pq = parts.path + ("?" + parts.query if parts.query else "")
                if asset_re.search(pq):
                    continue
                if nav_path_prefixes.match(pq):
                    continue
                title = a.get_text(" ", strip=True)
                if title and title.lower() in junk_titles:
                    continue
                # Akzeptiere wenn URL nach Bekanntmachung aussieht ODER der
                # sichtbare Linktext substantiell ist.
                if not (broad.search(pq) or (title and len(title) >= 25)):
                    continue
                out.append(full)
        return out

    # ------------------------------------------------------------------
    def _fetch_detail(self, url: str, match_terms: List[str]) -> TenderItem | None:
        try:
            resp = self.get(url)
        except Exception as exc:
            log.warning("[%s] Detail %s fehlgeschlagen: %s", self.name, url, exc)
            return None
        if resp.status_code != 200:
            return None
        return self._parse_detail(resp.text, url=url, portal=self.name,
                                  config=self.config, match_terms=match_terms)

    @classmethod
    def _parse_detail(
        cls,
        html: str,
        url: str,
        portal: str,
        config: dict,
        match_terms: List[str],
    ) -> TenderItem | None:
        soup = BeautifulSoup(html, "lxml")
        full_text = soup.get_text(" ", strip=True)
        if not _contains_any_term(full_text, match_terms):
            return None

        title = _select_text(soup, config.get("detail_title_selector", "h1, title"))
        if not title:
            title = (soup.title.string.strip() if soup.title and soup.title.string else url)

        authority = _select_text(soup, config.get("detail_authority_selector"))
        location = _select_text(soup, config.get("detail_location_selector"))
        deadline_raw = _select_text(soup, config.get("detail_deadline_selector"))
        publication_raw = _select_text(soup, config.get("detail_publication_selector"))

        return TenderItem(
            title=title[:500],
            portal=portal,
            url=url,
            contracting_authority=authority,
            location=location,
            deadline=_parse_date(deadline_raw),
            publication_date=_parse_date(publication_raw),
            description=full_text[:1500],
        )


# ---------------------------------------------------------------------------
def _in_navigation_context(el) -> bool:
    """True wenn der Anchor in einem Nav-/Footer-/Header-Container liegt.

    Schaut bis zu 6 Eltern hoch nach <nav>/<footer>/<header>/<aside>
    oder Klassen/IDs, die typisch fuer Navigation sind.
    """
    nav_class_re = re.compile(
        r"\b(nav|navigation|menu|footer|header|sidebar|aside|"
        r"breadcrumb|topbar|main-menu|sub-menu|metamenu|burger|"
        r"dropdown-menu|modul-teaser)\b",
        re.IGNORECASE,
    )
    cur = el
    for _ in range(6):
        if cur is None or getattr(cur, "name", None) is None:
            return False
        tag = cur.name.lower()
        if tag in {"nav", "footer", "header", "aside"}:
            return True
        cls = " ".join(cur.get("class") or [])
        eid = cur.get("id") or ""
        if nav_class_re.search(cls) or nav_class_re.search(eid):
            return True
        cur = cur.parent
    return False


def _contains_any_term(text: str, terms: Iterable[str]) -> bool:
    if not text:
        return False
    text_l = text.lower()
    for t in terms:
        if not t:
            continue
        if t.lower() in text_l:
            return True
    return False


def _select_text(soup, selector: str | None) -> str | None:
    if not selector:
        return None
    el = soup.select_one(selector)
    if not el:
        return None
    return el.get_text(" ", strip=True) or None
