"""Scraper fuer Next.js-Seiten.

Next.js-Seiten betten ihre Initial-Daten als JSON in einen
<script id="__NEXT_DATA__" type="application/json"> Tag ein. Wir
extrahieren das JSON, finden die Tender-Liste und mappen die Felder.

YAML-Konfig:

    - name: "evergabe.de"
      scraper: "nextjs"
      base_url: "https://www.evergabe.de"
      strategy: "search_url"
      config:
        # Pfad mit {term}-Platzhalter (term wird URL-encoded eingesetzt).
        search_path: "/auftraege/auftrag-suchen?search%5Bquery%5D={term}&search%5Bper_page%5D=50&search%5Bsort_order%5D=best"

        # Optional: dot-Pfad in den JSON-Daten zur Treffer-Liste.
        # Wenn leer, wird die Liste heuristisch gesucht.
        # data_path: "props.pageProps.results.items"

        # Optional: Mapping von TenderItem-Feldern auf JSON-Keys.
        # Mehrere Kandidaten als Liste - der erste, der einen Wert liefert,
        # gewinnt.
        field_map:
          title:       ["title", "name", "subject", "bezeichnung"]
          url:         ["url", "link", "href", "permalink", "slug"]
          deadline:    ["deadline", "submission_deadline", "angebotsfrist", "frist"]
          location:    ["location", "place", "place_of_performance", "ausfuehrungsort", "ort"]
          authority:   ["buyer", "buyer_name", "vergabestelle", "auftraggeber"]
          description: ["description", "excerpt", "summary", "beschreibung"]
          publication: ["published_at", "publication_date", "veroeffentlicht"]

        # Optional: URL-Template wenn der JSON nur einen Slug/ID liefert
        # url_template: "/auftraege/{slug}"
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Iterable, List, Sequence
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)


DEFAULT_FIELD_MAP = {
    "title":       ["title", "name", "subject", "bezeichnung", "headline"],
    "url":         ["url", "link", "href", "permalink", "absolute_url"],
    "slug":        ["slug", "id", "uuid", "publication_id"],
    "deadline":    ["deadline", "submission_deadline", "angebotsfrist", "frist", "submission_date", "submission_deadline_date"],
    "location":    ["location", "place", "place_of_performance", "ausfuehrungsort", "ort", "city"],
    "authority":   ["buyer", "buyer_name", "vergabestelle", "auftraggeber", "organization", "organisation"],
    "description": ["description", "excerpt", "summary", "beschreibung", "short_description"],
    "publication": ["published_at", "publication_date", "veroeffentlicht", "created_at"],
}

# Welche Keys in Listen-Items deuten auf "Das ist ein Tender"?
TENDER_HINTS = (
    "title", "name", "subject", "bezeichnung",
    "deadline", "frist", "submission_deadline",
    "buyer", "vergabestelle", "auftraggeber",
)


class NextjsScraper(BaseScraper):
    name = "nextjs"

    # ------------------------------------------------------------------
    def fetch(self, terms: List[str]) -> List[TenderItem]:
        path = self.config.get("search_path")
        if not path:
            log.warning("[%s] search_path fehlt.", self.name)
            return []

        items: dict[str, TenderItem] = {}
        url_terms = terms if "{term}" in path else [None]
        for term in url_terms:
            try:
                items.update(self._fetch_one(path, term))
            except Exception as exc:  # pragma: no cover – Netzwerk
                log.warning("[%s] Fehler bei term '%s': %s", self.name, term, exc)
        return list(items.values())

    # ------------------------------------------------------------------
    def _fetch_one(self, path: str, term: str | None) -> dict[str, TenderItem]:
        if term is not None:
            path = path.replace("{term}", quote(term, safe=""))
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        resp = self.get(url)
        if resp.status_code != 200:
            log.info("[%s] HTTP %s bei %s", self.name, resp.status_code, url)
            return {}
        return self.parse_html(
            resp.text,
            base_url=self.base_url,
            portal_name=self.name,
            config=self.config,
        )

    # ------------------------------------------------------------------
    @classmethod
    def parse_html(
        cls,
        html: str,
        base_url: str,
        portal_name: str,
        config: dict,
    ) -> dict[str, TenderItem]:
        data = extract_next_data(html)
        if data is None:
            log.warning("[%s] __NEXT_DATA__ nicht gefunden.", portal_name)
            return {}

        # 1) Treffer-Liste finden
        tenders_list = _resolve_data_path(data, config.get("data_path")) \
            if config.get("data_path") else None
        if tenders_list is None:
            tenders_list = _find_tender_list(data)
        if not tenders_list:
            log.warning("[%s] Keine Tender-Liste im JSON gefunden.", portal_name)
            return {}

        log.info("[%s] %d Eintraege im JSON gefunden.", portal_name, len(tenders_list))

        # 2) Feld-Mapping
        field_map = _merge_field_map(config.get("field_map"))
        url_template = config.get("url_template")

        out: dict[str, TenderItem] = {}
        for raw in tenders_list:
            if not isinstance(raw, dict):
                continue
            title = _first_value(raw, field_map["title"])
            if not title:
                continue
            slug = _first_value(raw, field_map.get("slug", []))
            url = _first_value(raw, field_map["url"])
            if not url and url_template and slug:
                url = url_template.format(slug=slug, id=slug)
            if not url and slug:
                # Fallback: base + slug
                url = f"/auftraege/{slug}"
            if not url:
                # Kein URL bekannt: synthetischen Hash bauen
                import hashlib
                h = hashlib.md5(str(title).encode("utf-8")).hexdigest()[:10]
                url = f"#nextjs-{h}"
            if not url.startswith("http"):
                url = urljoin(base_url, url)

            deadline = _parse_any_date(_first_value(raw, field_map["deadline"]))
            publication = _parse_any_date(_first_value(raw, field_map["publication"]))
            location = _stringify(_first_value(raw, field_map["location"]))
            authority = _stringify(_first_value(raw, field_map["authority"]))
            description = _stringify(_first_value(raw, field_map["description"]))

            out[url] = TenderItem(
                title=str(title)[:500],
                portal=portal_name,
                url=url,
                contracting_authority=authority,
                location=location,
                deadline=deadline,
                publication_date=publication,
                description=(description or "")[:1000],
            )
        return out


# ---------------------------------------------------------------------------
def extract_next_data(html: str) -> Any | None:
    """Holt das JSON aus <script id="__NEXT_DATA__" type="application/json">."""
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return None
    try:
        return json.loads(script.string)
    except (TypeError, ValueError) as exc:
        log.warning("__NEXT_DATA__ konnte nicht geparst werden: %s", exc)
        return None


def _resolve_data_path(data: Any, dotted_path: str | None) -> Any:
    if not dotted_path:
        return None
    cur = data
    for key in dotted_path.split("."):
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        elif isinstance(cur, list) and key.isdigit():
            idx = int(key)
            if 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        else:
            return None
    return cur if isinstance(cur, list) else None


def _find_tender_list(data: Any, depth: int = 0) -> list | None:
    """Sucht rekursiv die laengste Liste, deren Items wie Tender aussehen."""
    if depth > 10:
        return None
    if isinstance(data, list):
        if len(data) > 0 and all(isinstance(x, dict) for x in data[:5]):
            sample = data[0]
            if any(k in sample for k in TENDER_HINTS):
                return data
    if isinstance(data, dict):
        # bevorzugt: bekannte Listen-Keys
        priority = ("results", "items", "data", "tenders", "auftraege", "notices",
                    "edges", "nodes", "list")
        for key in priority:
            if key in data:
                found = _find_tender_list(data[key], depth + 1)
                if found:
                    return found
        # Fallback: alle Werte durchwuehlen
        best: list | None = None
        for v in data.values():
            found = _find_tender_list(v, depth + 1)
            if found and (best is None or len(found) > len(best)):
                best = found
        return best
    return None


def _merge_field_map(user: dict | None) -> dict[str, list[str]]:
    out = {k: list(v) for k, v in DEFAULT_FIELD_MAP.items()}
    if user:
        for k, v in user.items():
            if isinstance(v, str):
                out[k] = [v] + out.get(k, [])
            elif isinstance(v, list):
                out[k] = list(v) + [c for c in out.get(k, []) if c not in v]
    return out


def _first_value(item: dict, keys: Sequence[str]) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, "", []):
            return item[key]
    return None


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        # Komplexe Strukturen: bevorzugt 'name'/'title'-Felder
        for k in ("name", "title", "value", "label", "de", "deu"):
            if k in value:
                return _stringify(value[k])
        # sonst JSON-encoded zurueck
        return json.dumps(value, ensure_ascii=False)[:200]
    if isinstance(value, list):
        return ", ".join(filter(None, (_stringify(x) for x in value[:5])))[:200]
    return str(value)


_DATE_DE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def _parse_any_date(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, dict):
        # Z.B. {"date": "...", "timezone": "..."}
        for k in ("date", "iso", "value", "datetime", "time"):
            if k in value:
                return _parse_any_date(value[k])
        return None
    if isinstance(value, (int, float)):
        try:
            # epoch seconds or millis
            ts = value / 1000 if value > 1e12 else value
            return datetime.fromtimestamp(ts)
        except (ValueError, OverflowError, OSError):
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # ISO 8601
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    # DE
    m = _DATE_DE.search(s)
    if m:
        d, mo, y = m.groups()
        try:
            return datetime(int(y), int(mo), int(d))
        except ValueError:
            return None
    return None
