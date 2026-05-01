"""Scraper fuer TED – Tenders Electronic Daily.

Verwendet die offizielle Search-API v3:
    POST https://api.ted.europa.eu/v3/notices/search
    Content-Type: application/json
    {
      "query": "<lucene-style-query>",
      "fields": [...],
      "page": 1,
      "limit": 50
    }

Die API ist oeffentlich und erfordert keinen API-Key.
Doku: https://ted.europa.eu/api/v3/swagger
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterable, List

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)

SEARCH_ENDPOINT = "/v3/notices/search"
DEFAULT_FIELDS = [
    "publication-number",
    "notice-title",
    "buyer-name",
    "buyer-country",
    "place-of-performance",
    "publication-date",
    "deadline-date-lot",
    "classification-cpv",
    "description-lot",
    "links",
]
COUNTRY_FILTER = "DEU"  # nur Deutschland


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class TedScraper(BaseScraper):
    name = "TED"

    def fetch(self, terms: List[str]) -> List[TenderItem]:
        # TED v3 lehnt komplexe Query-Strings mit ~= teilweise mit HTTP 400
        # ab. Robuster Ansatz: Country-Filter + Pagination, dann client-
        # seitig nach Cluster-Begriffen filtern.
        max_pages = int(self.config.get("max_pages", 4))
        items: dict[str, TenderItem] = {}
        for page in range(1, max_pages + 1):
            try:
                payload = self._build_payload(page=page)
                page_items = self._search(payload)
            except Exception as exc:  # pragma: no cover - Netzwerk
                log.warning("[TED] Suche page %d fehlgeschlagen: %s", page, exc)
                break
            if not page_items:
                break
            before = len(items)
            items.update(page_items)
            # Wenn die neue Seite keinen einzigen neuen Eintrag bringt -> Ende.
            if len(items) == before:
                break

        match_terms = self._match_terms(terms)
        if match_terms:
            items = {u: it for u, it in items.items() if _matches_any(it, match_terms)}
        return list(items.values())

    # ------------------------------------------------------------------
    def _build_payload(self, page: int = 1) -> dict:
        # Minimale, stabile Query: alle aktiven Bekanntmachungen aus DE.
        return {
            "query": f'buyer-country="{COUNTRY_FILTER}"',
            "fields": DEFAULT_FIELDS,
            "page": page,
            "limit": int(self.config.get("limit", 250)),
            "scope": "ACTIVE",
        }

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
    def _search(self, payload: dict) -> dict[str, TenderItem]:
        url = f"{self.base_url}{SEARCH_ENDPOINT}"
        if not self.can_fetch(url):
            log.warning("[TED] robots.txt verbietet %s", url)
            self.http_log.append({"url": url, "status": None, "size": 0,
                                  "error": "robots.txt verbietet Abruf"})
            return {}
        try:
            resp = self._client.post(url, json=payload)
        except Exception as exc:
            self.http_log.append({"url": url, "status": None, "size": 0,
                                  "error": "{}: {}".format(type(exc).__name__, str(exc)[:160])})
            raise
        self.http_log.append({
            "url": url,
            "status": resp.status_code,
            "size": len(resp.content) if resp.content else 0,
            "error": None,
        })
        if resp.status_code != 200:
            log.info("[TED] HTTP %s: %s", resp.status_code, resp.text[:200])
            return {}
        data = resp.json()
        return self.parse_results(data.get("notices", []), portal_name=self.name)

    # ------------------------------------------------------------------
    @classmethod
    def parse_results(
        cls, notices: Iterable[dict], portal_name: str = "TED",
    ) -> dict[str, TenderItem]:
        out: dict[str, TenderItem] = {}
        for n in notices:
            pub_no = _first(n.get("publication-number"))
            title = _first(n.get("notice-title")) or "(ohne Titel)"
            buyer = _first(n.get("buyer-name"))
            place_raw = _first(n.get("place-of-performance"))
            pub_date = _parse_iso(_first(n.get("publication-date")))
            deadline = _parse_iso(_first(n.get("deadline-date-lot")))
            cpvs = _as_list(n.get("classification-cpv"))
            description = _first(n.get("description-lot"))
            link = None
            links_field = n.get("links") or {}
            if isinstance(links_field, dict):
                link = links_field.get("html") or links_field.get("xml")
                if isinstance(link, dict):
                    link = link.get("DEU") or next(iter(link.values()), None)
                if isinstance(link, list):
                    link = link[0] if link else None
            if not link and pub_no:
                link = f"https://ted.europa.eu/udl?uri=TED:NOTICE:{pub_no}"

            if not link:
                continue

            # NUTS-Code (DEU/DE0/DEA/DEB/DEG01 ...) zu Bundesland mappen.
            # Original-NUTS bleibt in description-Vorschau, location wird
            # auf den klaren Stadt-/Region-Namen aus dem Titel gesetzt
            # falls TED nichts Lesbares liefert.
            region = _nuts_to_bundesland(place_raw)
            location = _location_from_title(title) or _readable_place(place_raw)

            # Sinnvolle Beschreibung bauen wenn description-lot fehlt.
            if not description or len(description.strip()) < 10:
                description = _build_description(
                    title=title, buyer=buyer, location=location,
                    cpvs=cpvs, pub_no=pub_no,
                )

            item = TenderItem(
                title=title,
                portal=portal_name,
                url=link,
                contracting_authority=buyer,
                location=location,
                region=region,
                publication_date=pub_date,
                deadline=deadline,
                description=description,
                cpv_codes=[str(c) for c in cpvs if c],
            )
            out[link] = item
        return out


# ---------------------------------------------------------------------------
def _first(value):
    """TED-Felder sind oft Listen oder Multilang-Dicts – Hilfsfunktion."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _first(value[0]) if value else None
    if isinstance(value, dict):
        # bevorzugt deutsche Sprachversion
        for key in ("deu", "DEU", "de", "DE", "eng", "ENG"):
            if key in value:
                return _first(value[key])
        # Fallback: erstes Element
        if value:
            return _first(next(iter(value.values())))
    return None


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# NUTS-Praefix (erste 3-4 Zeichen) -> Bundesland.
# https://ec.europa.eu/eurostat/web/nuts/national-structures
_NUTS_TO_BUNDESLAND = {
    "DE1": "Baden-Württemberg",
    "DE2": "Bayern",
    "DE3": "Berlin",
    "DE4": "Brandenburg",
    "DE5": "Bremen",
    "DE6": "Hamburg",
    "DE7": "Hessen",
    "DE8": "Mecklenburg-Vorpommern",
    "DE9": "Niedersachsen",
    "DEA": "Nordrhein-Westfalen",
    "DEB": "Rheinland-Pfalz",
    "DEC": "Saarland",
    "DED": "Sachsen",
    "DEE": "Sachsen-Anhalt",
    "DEF": "Schleswig-Holstein",
    "DEG": "Thüringen",
}


def _nuts_to_bundesland(code: str | None) -> str | None:
    """DEG01 -> Thueringen, DEA32 -> NRW etc. DEU/DE bleiben None."""
    if not code:
        return None
    code = code.strip().upper()
    if code in {"DEU", "DE", ""}:
        return None
    # Erste 3 Zeichen treffen alle 16 Laender (DEA, DEB, ..., DE1, DE2, ...).
    return _NUTS_TO_BUNDESLAND.get(code[:3])


def _readable_place(value: str | None) -> str | None:
    """Wenn TED nur den NUTS-Code in place-of-performance liefert (DEU/DEA32),
    geben wir leeren Wert zurueck statt 'DEU' in die location zu schreiben."""
    if not value:
        return None
    v = value.strip()
    # NUTS-Codes sind 2-5 Zeichen alphanumerisch und beginnen mit DE
    if re.match(r"^DE[0-9A-Z]{0,4}$", v):
        return None
    return v


def _location_from_title(title: str | None) -> str | None:
    """TED-Titel sind im Format 'Deutschland-STADT: Was'. Wir ziehen die
    Stadt aus dem Titel, weil place-of-performance oft nur ein NUTS-Code ist."""
    if not title:
        return None
    m = re.match(r"\s*Deutschland\s*-\s*([^:]+?):", title)
    if m:
        return m.group(1).strip()
    return None


def _build_description(title, buyer, location, cpvs, pub_no) -> str:
    """Baut eine zumindest informative Beschreibung wenn description-lot leer."""
    parts = []
    if buyer:
        parts.append("Auftraggeber: {}".format(buyer))
    if location:
        parts.append("Ort: {}".format(location))
    if cpvs:
        parts.append("CPV: {}".format(", ".join(str(c) for c in cpvs[:5])))
    if pub_no:
        parts.append("TED-Nr: {}".format(pub_no))
    return " · ".join(parts) if parts else (title or "")


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
