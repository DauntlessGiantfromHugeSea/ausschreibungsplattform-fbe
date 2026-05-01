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
        # ab. Robuster Ansatz: ein einziger Request mit reinem Country-Filter,
        # dann client-seitig nach Cluster-Begriffen filtern. So ueberleben
        # wir API-Format-Aenderungen.
        try:
            payload = self._build_payload()
        except Exception as exc:  # pragma: no cover
            log.warning("[TED] Payload-Bau fehlgeschlagen: %s", exc)
            return []
        try:
            items = self._search(payload)
        except Exception as exc:  # pragma: no cover - Netzwerk
            log.warning("[TED] Suche fehlgeschlagen: %s", exc)
            return []

        match_terms = self._match_terms(terms)
        if match_terms:
            items = {u: it for u, it in items.items() if _matches_any(it, match_terms)}
        return list(items.values())

    # ------------------------------------------------------------------
    def _build_payload(self) -> dict:
        # Minimale, stabile Query: alle aktiven Bekanntmachungen aus DE.
        return {
            "query": f'buyer-country="{COUNTRY_FILTER}"',
            "fields": DEFAULT_FIELDS,
            "page": 1,
            "limit": int(self.config.get("limit", 100)),
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
            place = _first(n.get("place-of-performance"))
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

            item = TenderItem(
                title=title,
                portal=portal_name,
                url=link,
                contracting_authority=buyer,
                location=place,
                region=None,
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
