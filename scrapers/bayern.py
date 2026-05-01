"""Scraper fuer www.auftraege.bayern.de - Bayerische Auftragsboerse.

Zwei Datenquellen werden gecrawlt:

1. Landingpage / mit <ul id='webTicker'> - Schnell-Liste der aktuellen
   Bekanntmachungen. <li class='itemTicker'> mit <b>TITEL</b> + ' (BEHOERDE)'.
   Keine Detail-Anchors.

2. /Dashboards/Dashboard_off?BL=09 - das ASP.NET-Dashboard auf der
   deutsche-evergabe-Plattform mit ausfuehrlicher Auftragsliste.
   Enthaelt typischerweise eine <table> mit Datum, Titel-Link, Behoerde.

Beide Quellen werden zusammengefuehrt und dedupliziert (per stabiler
URL bzw. Titel-Hash).

Detail-Felder fuer den FBE-Use-Case:
  - title       aus <b> bzw. Tabellenzelle
  - authority   aus dem Klammertext bzw. Behoerden-Spalte
  - region      = "Bayern" (Plattform ist Bundesland-spezifisch)
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)

WEBTICKER_PATH = "/"
DASHBOARD_PATH = "/Dashboards/Dashboard_off?BL=09"
AUTHORITY_RE = re.compile(r"\(([^()]+?)\)\s*$")
DATE_DE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


class BayernScraper(BaseScraper):
    """Scraper fuer www.auftraege.bayern.de WebTicker + Dashboard."""

    name = "Auftragsbörse Bayern"

    DEFAULT_UA = (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) "
        "Gecko/20100101 Firefox/120.0"
    )

    def __init__(self, *args, **kwargs):
        config = kwargs.get("config") or {}
        if "user_agent" not in config:
            kwargs["config"] = {**config, "user_agent": self.DEFAULT_UA}
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    def fetch(self, terms: List[str]) -> List[TenderItem]:
        out: dict[str, TenderItem] = {}

        # 1) WebTicker auf der Landingpage
        ticker_path = self.config.get("listing_path", WEBTICKER_PATH)
        ticker_url = urljoin(self.base_url + "/", ticker_path.lstrip("/"))
        try:
            resp = self.get(ticker_url)
            if resp.status_code == 200:
                out.update(self.parse_webticker(
                    resp.text, base_url=self.base_url,
                    portal_name=self.name, landing_url=ticker_url,
                ))
        except Exception as exc:  # pragma: no cover
            log.warning("[%s] WebTicker fehlgeschlagen: %s", self.name, exc)

        # Kurze Pause zwischen den beiden Requests.
        delay = float(self.config.get("request_delay_s", 0.4))
        if delay > 0:
            time.sleep(delay)

        # 2) Dashboard /Dashboards/Dashboard_off?BL=09
        if self.config.get("crawl_dashboard", True):
            dash_path = self.config.get("dashboard_path", DASHBOARD_PATH)
            dash_url = urljoin(self.base_url + "/", dash_path.lstrip("/"))
            try:
                resp = self.get(dash_url)
                if resp.status_code == 200:
                    out.update(self.parse_dashboard(
                        resp.text, base_url=self.base_url,
                        portal_name=self.name, landing_url=dash_url,
                    ))
            except Exception as exc:  # pragma: no cover
                log.warning("[%s] Dashboard fehlgeschlagen: %s", self.name, exc)

        # Optional client-seitiger Filter auf Cluster-Begriffe.
        if self.config.get("filter_by_terms", True):
            match_terms = self._match_terms(terms)
            out = {k: v for k, v in out.items() if _matches_any(v, match_terms)}

        log.info("[%s] %d Treffer (WebTicker + Dashboard kombiniert)",
                 self.name, len(out))
        return list(out.values())

    # ------------------------------------------------------------------
    @classmethod
    def parse_webticker(
        cls, html: str, base_url: str, portal_name: str,
        landing_url: str | None = None,
    ) -> dict[str, TenderItem]:
        soup = BeautifulSoup(html, "lxml")
        out: dict[str, TenderItem] = {}

        ul = soup.find(id="webTicker")
        items = ul.find_all("li") if ul else soup.select("li.itemTicker")

        for li in items:
            title_el = li.find(["b", "strong"])
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            if not title or len(title) < 6:
                continue

            full = li.get_text(" ", strip=True)
            authority = None
            m = AUTHORITY_RE.search(full)
            if m:
                authority = m.group(1).strip()

            ident = "{}|{}".format(title, authority or "")
            h = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:12]
            url = (landing_url or base_url) + "#bayern-ticker-" + h

            out[url] = TenderItem(
                title=title[:500],
                portal=portal_name,
                url=url,
                contracting_authority=authority,
                region="Bayern",
                description=full[:500],
            )
        return out

    # ------------------------------------------------------------------
    @classmethod
    def parse_dashboard(
        cls, html: str, base_url: str, portal_name: str,
        landing_url: str | None = None,
    ) -> dict[str, TenderItem]:
        """Parst das ASP.NET-Dashboard-HTML.

        Strategie: alle <table>-Zeilen mit mindestens 3 Zellen durchgehen,
        Titel-Spalte heuristisch ueber Anchor finden, Datum/Behoerde aus
        anderen Zellen ziehen. Robust gegen Layout-Aenderungen.
        """
        soup = BeautifulSoup(html, "lxml")
        out: dict[str, TenderItem] = {}

        # 1) Klassischer Tabellen-Crawl
        for tr in soup.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) < 2:
                continue
            # Header-Zeile ueberspringen (nur <th> oder Text-Marker)
            if all(td.name == "th" for td in tds):
                continue

            # Anchor finden, der zu einer Detail-Seite zeigt.
            link = None
            for a in tr.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith(("#", "javascript:", "mailto:")):
                    continue
                # Plausible Detail-URLs: enthalten 'auftrag', 'tender',
                # 'bekanntmachung', 'detail' oder eine numerische ID.
                if re.search(
                    r"(auftrag|tender|bekanntmachung|detail|notice|"
                    r"vergabe|pruefung|/[0-9]{4,})",
                    href, re.IGNORECASE,
                ):
                    link = a
                    break
            if link is None:
                # Fallback: erster Anchor in der Zeile, der KEIN
                # Hash-/JS-/Mail-Link ist.
                for a in tr.find_all("a", href=True):
                    href = a["href"].strip()
                    if not href or href.startswith(("#", "javascript:", "mailto:")):
                        continue
                    link = a
                    break
            if link is None:
                continue

            title = link.get_text(" ", strip=True)
            if not title or len(title) < 6:
                continue
            # Junk-Titel ablehnen (Buttons / Sortier-Links)
            if title.lower() in {"weiter", "details", "drucken",
                                 "merken", "anzeigen", "mehr"}:
                continue

            full_text = tr.get_text(" | ", strip=True)
            # Behoerde: heuristisch aus den uebrigen Zellen oder Klammer.
            authority = _cell_with_buyer_keywords(tds, link)
            if not authority:
                m = AUTHORITY_RE.search(full_text)
                if m:
                    authority = m.group(1).strip()
            # Datum: erste DD.MM.YYYY in der Zeile als Frist verwenden.
            deadline = None
            dm = DATE_DE.search(full_text)
            if dm:
                d, mo, y = dm.groups()
                try:
                    from datetime import datetime as _dt
                    deadline = _dt(int(y), int(mo), int(d))
                except ValueError:
                    deadline = None

            href = link["href"].strip()
            if href.startswith(("http://", "https://")):
                url = href
            else:
                url = urljoin(base_url, href)

            out[url] = TenderItem(
                title=title[:500],
                portal=portal_name,
                url=url,
                contracting_authority=authority,
                region="Bayern",
                deadline=deadline,
                description=full_text[:500],
            )

        # 2) Fallback: wenn die Tabelle nichts brachte, sammle alle
        #    plausiblen Anchors als Karten.
        if not out:
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith(("#", "javascript:", "mailto:")):
                    continue
                if not re.search(
                    r"(auftrag|tender|bekanntmachung|detail|notice|"
                    r"vergabe|pruefung)",
                    href, re.IGNORECASE,
                ):
                    continue
                title = a.get_text(" ", strip=True)
                if not title or len(title) < 10:
                    continue
                url = href if href.startswith(("http://", "https://")) else urljoin(base_url, href)
                out.setdefault(url, TenderItem(
                    title=title[:500],
                    portal=portal_name,
                    url=url,
                    region="Bayern",
                ))

        return out

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
def _cell_with_buyer_keywords(cells, exclude_link) -> str | None:
    """Sucht in den Tabellenzellen nach 'Stadt/Land/Amt/...' Markern und
    gibt den Text der ersten passenden Zelle zurueck."""
    buyer_re = re.compile(
        r"\b(Stadt|Gemeinde|Land(kreis)?|Amt|Behoerde|Beh[oö]rde|"
        r"Ministerium|Universit|Klinik|Bundes|Staatl|Hoch|"
        r"Bezirk|Verband|Verwaltung)",
        re.IGNORECASE,
    )
    for td in cells:
        # Zelle, die den Link enthaelt, ueberspringen
        if exclude_link is not None and td.find(exclude_link.name, href=exclude_link.get("href")):
            continue
        text = td.get_text(" ", strip=True)
        if not text or len(text) > 200:
            continue
        if buyer_re.search(text):
            return text
    return None


def _matches_any(item: TenderItem, terms) -> bool:
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
