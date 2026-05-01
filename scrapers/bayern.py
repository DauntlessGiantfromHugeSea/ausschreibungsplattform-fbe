"""Scraper fuer www.auftraege.bayern.de - Bayerische Auftragsboerse.

Live-Dump zeigt: Die Landingpage / hat direkt einen WebTicker mit allen
aktuellen Bekanntmachungen eingebettet:

    <ul id="webTicker">
      <li class="itemTicker">
        <i class="fa fa-exclamation-circle fa-2x"></i>
        <b>Sonderpaedagogisches Foerderzentrum Vohenstrauss - Abbrucharbeiten</b>
        (Landratsamt Neustadt a.d.Waldnaab)
      </li>
      ... (50+ Eintraege)
    </ul>

Format pro Zeile: <b>TITEL</b> + ' (BEHOERDE)' im Klartext. Keine
Detail-Anchors - die echten Detail-Seiten liegen hinter dem Login auf
vst.deutsche-evergabe.de. Daher generieren wir synthetische URLs aus
Titel-Hash und verlinken auf die Landingpage.

Detail-Felder fuer den FBE-Use-Case:
  - title       aus <b>
  - authority   aus dem Klammertext nach <b>
  - region      = "Bayern" (Plattform ist Bundesland-spezifisch)
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)

WEBTICKER_PATH = "/"
AUTHORITY_RE = re.compile(r"\(([^()]+?)\)\s*$")


class BayernScraper(BaseScraper):
    """Scraper fuer www.auftraege.bayern.de WebTicker."""

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
        path = self.config.get("listing_path", WEBTICKER_PATH)
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        try:
            resp = self.get(url)
        except Exception as exc:  # pragma: no cover - Netzwerk
            log.warning("[%s] GET %s fehlgeschlagen: %s", self.name, url, exc)
            return []
        if resp.status_code != 200:
            log.info("[%s] HTTP %s bei %s", self.name, resp.status_code, url)
            return []

        items_dict = self.parse_webticker(
            resp.text, base_url=self.base_url, portal_name=self.name,
            landing_url=url,
        )

        # Optional client-seitiger Filter auf Cluster-Begriffe.
        if self.config.get("filter_by_terms", True):
            match_terms = self._match_terms(terms)
            items_dict = {
                k: v for k, v in items_dict.items()
                if _matches_any(v, match_terms)
            }

        log.info("[%s] %d Treffer", self.name, len(items_dict))
        return list(items_dict.values())

    # ------------------------------------------------------------------
    @classmethod
    def parse_webticker(
        cls, html: str, base_url: str, portal_name: str,
        landing_url: str | None = None,
    ) -> dict[str, TenderItem]:
        soup = BeautifulSoup(html, "lxml")
        out: dict[str, TenderItem] = {}

        # Primaer: id="webTicker", Fallback auf class itemTicker irgendwo.
        ul = soup.find(id="webTicker")
        items = ul.find_all("li") if ul else soup.select("li.itemTicker")

        for li in items:
            title_el = li.find(["b", "strong"])
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            if not title or len(title) < 6:
                continue

            # Volltext der li -> Behoerde aus Klammern am Ende ziehen.
            full = li.get_text(" ", strip=True)
            authority = None
            m = AUTHORITY_RE.search(full)
            if m:
                authority = m.group(1).strip()

            # Synthetische URL: stabiler Hash aus Titel + Behoerde, damit
            # Dedup-Fingerprint zwischen Laeufen reproduzierbar bleibt.
            ident = "{}|{}".format(title, authority or "")
            h = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:12]
            url = (landing_url or base_url) + "#bayern-" + h

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
    def _match_terms(self, query_terms: List[str]) -> List[str]:
        if "match_terms" in self.config:
            return list(self.config["match_terms"])
        try:
            from backend.search_terms import load_search_config
            return load_search_config().all_terms() or list(query_terms)
        except Exception:
            return list(query_terms)


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
