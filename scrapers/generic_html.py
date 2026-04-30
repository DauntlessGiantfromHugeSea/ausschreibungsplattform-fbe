"""Generischer HTML-Scraper - jetzt mit zwei Modi.

Modus 1: Suche per URL (search_path enthaelt {term})
    Schickt jeden query_term als URL-Parameter, parst die Trefferliste.
    Sinnvoll wenn das Portal eine echte serverseitige Suche per URL hat.

Modus 2: Listing (search_path ohne {term})
    Holt eine offene Listing-Seite (z.B. /auftraege) und filtert die
    geparsten Karten client-seitig auf die Cluster-Begriffe.
    Sinnvoll fuer Portale, deren Detail-Seiten Login brauchen, deren
    Listing aber alle relevanten Felder zeigt (Titel, Beschreibung,
    Ort, Frist, Kategorie - z.B. evergabe.de).

Optional:
    filter_by_terms: true   -> nur Karten behalten die mindestens einen
                              Cluster-Begriff im Volltext haben
    label_extract:          -> Felder aus 'Label: Wert'-Strukturen ziehen
        deadline:  ['Angebotsfrist', 'Frist']
        location:  ['Ausführungsort', 'Ort']
        authority: ['Auftraggeber', 'Vergabestelle']
        publication: ['Veröffentlichung', 'Veröffentlicht']
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Iterable, List
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)

DATE_DE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")

# Fallback-Pfade fuer Auto-Discovery wenn das konfigurierte Listing leer ist.
DEFAULT_LISTING_FALLBACKS = [
    "/aktuell",
    "/ausschreibungen",
    "/auftraege",
    "/bekanntmachungen",
    "/notices",
    "/notice/list",
    "/VMPSatellite/notice",
    "/VMPCenter/notice",
]

# Standard-Labels die ein Card-Volltext nach 'Label: Wert' absucht
DEFAULT_LABELS = {
    "deadline":    ["Angebotsfrist", "Abgabefrist", "Frist", "Submission deadline"],
    "location":    ["Ausführungsort", "Ausfuehrungsort", "Ort", "Erfüllungsort", "Location"],
    "authority":   ["Auftraggeber", "Vergabestelle", "Buyer"],
    "publication": ["Veröffentlichung", "Veroeffentlichung", "Veröffentlicht", "Veroeffentlicht", "Publication"],
}

# Welche Labels gelten als "Trennzeichen" - das Ende eines Wertes
ALL_LABELS = sorted(
    {label for labels in DEFAULT_LABELS.values() for label in labels} |
    {"Auftragsart", "Leistungszeit"},
    key=len, reverse=True,
)


class GenericHtmlScraper(BaseScraper):
    name = "generic_html"

    # ------------------------------------------------------------------
    def fetch(self, terms: List[str]) -> List[TenderItem]:
        path = self.config.get("search_path")
        if not path:
            log.warning("[%s] search_path fehlt in der Konfiguration.", self.name)
            return []

        if "{term}" in path:
            return self._fetch_search_mode(path, terms)
        return self._fetch_listing_mode(path, terms)

    # ------------------------------------------------------------------
    # Modus 1: Suche per URL
    def _fetch_search_mode(self, path: str, terms: List[str]) -> List[TenderItem]:
        items: dict[str, TenderItem] = {}
        for term in terms:
            try:
                # URL-encode term, damit Umlaute & Sonderzeichen den Server
                # nicht stoeren (Flüssigboden -> Fl%C3%BCssigboden).
                items.update(self._fetch_one(path.replace("{term}", quote(term, safe=""))))
            except Exception as exc:  # pragma: no cover – Netzwerk
                log.warning("[%s] Fehler bei '%s': %s", self.name, term, exc)
        if self.config.get("filter_by_terms"):
            items = {u: it for u, it in items.items() if _matches_any(it, self._match_terms(terms))}
        return list(items.values())

    # ------------------------------------------------------------------
    # Modus 2: Listing-Seite + client-seitiger Filter
    def _fetch_listing_mode(self, path: str, terms: List[str]) -> List[TenderItem]:
        # listing_paths erlauben mehrere Eingangsseiten (Pagination)
        paths = self.config.get("listing_paths") or [path]
        items: dict[str, TenderItem] = {}
        for p in paths:
            try:
                items.update(self._fetch_one(p))
            except Exception as exc:  # pragma: no cover – Netzwerk
                log.warning("[%s] Fehler bei %s: %s", self.name, p, exc)

        # Auto-Discovery: wenn die konfigurierten Pfade nichts gebracht haben,
        # gaengige Fallback-Pfade durchprobieren.
        if not items and self.config.get("auto_discover", True):
            for fallback in DEFAULT_LISTING_FALLBACKS:
                if fallback in paths:
                    continue
                try:
                    found = self._fetch_one(fallback)
                except Exception:  # pragma: no cover
                    continue
                if found:
                    log.info("[%s] Auto-Discovery: %s lieferte %d Treffer",
                             self.name, fallback, len(found))
                    items.update(found)
                    break

        if self.config.get("filter_by_terms", True):
            match_terms = self._match_terms(terms)
            items = {u: it for u, it in items.items() if _matches_any(it, match_terms)}
        return list(items.values())

    # ------------------------------------------------------------------
    def _fetch_one(self, path: str) -> dict[str, TenderItem]:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        resp = self.get(url)
        if resp.status_code != 200:
            log.info("[%s] HTTP %s bei %s", self.name, resp.status_code, url)
            return {}
        return self.parse_html(resp.text, base_url=self.base_url, portal_name=self.name, config=self.config)

    # ------------------------------------------------------------------
    def _match_terms(self, query_terms: List[str]) -> List[str]:
        # Client-Filter laeuft gegen ALLE Cluster-Begriffe - nicht nur
        # die Such-Begriffe, weil Synonyme genauso interessant sind.
        if "match_terms" in self.config:
            return list(self.config["match_terms"])
        try:
            from backend.search_terms import load_search_config
            return load_search_config().all_terms() or list(query_terms)
        except Exception:
            return list(query_terms)

    # ------------------------------------------------------------------
    @classmethod
    def parse_html(
        cls,
        html: str,
        base_url: str,
        portal_name: str,
        config: dict,
    ) -> dict[str, TenderItem]:
        soup = BeautifulSoup(html, "lxml")

        result_sel = config.get("result_selector")
        if not result_sel:
            return {}

        title_sel = config.get("title_selector", "h2, h3, a, .title, .auftrag-title")
        link_sel = config.get("link_selector")
        auth_sel = config.get("authority_selector")
        loc_sel = config.get("location_selector")
        deadline_sel = config.get("deadline_selector")
        pub_sel = config.get("publication_selector")
        desc_sel = config.get("description_selector")

        out: dict[str, TenderItem] = {}
        for el in soup.select(result_sel):
            title_el = el.select_one(title_sel)
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            if not title:
                continue

            url_full = _find_link(el, base_url, link_sel, title_el) or _synthetic_url(base_url, title)

            # Volltext der Karte fuer Label- und Filter-Faelle
            full_text = el.get_text(" ", strip=True)

            authority = _text_or_label(el, auth_sel, full_text, DEFAULT_LABELS["authority"])
            location = _text_or_label(el, loc_sel, full_text, DEFAULT_LABELS["location"])
            deadline = _parse_date(
                _text_or_label(el, deadline_sel, full_text, DEFAULT_LABELS["deadline"])
            )
            publication = _parse_date(
                _text_or_label(el, pub_sel, full_text, DEFAULT_LABELS["publication"])
            )
            description = _text(el, desc_sel) or full_text[:500]

            out[url_full] = TenderItem(
                title=title[:500],
                portal=portal_name,
                url=url_full,
                contracting_authority=_clean(authority),
                location=_clean(location),
                deadline=deadline,
                publication_date=publication,
                description=(description or "")[:1000],
            )
        return out


# ---------------------------------------------------------------------------
def _find_link(el, base_url: str, link_sel: str | None, title_el) -> str | None:
    if link_sel:
        link_el = el.select_one(link_sel)
        if link_el and link_el.get("href"):
            return urljoin(base_url, link_el["href"].strip())
    if title_el and title_el.name == "a" and title_el.get("href"):
        return urljoin(base_url, title_el["href"].strip())
    fallback = el.find("a", href=True)
    if fallback:
        href = fallback["href"].strip()
        if href and not href.startswith("#") and not href.startswith("javascript:"):
            return urljoin(base_url, href)
    return None


def _synthetic_url(base_url: str, title: str) -> str:
    h = hashlib.md5(title.encode("utf-8")).hexdigest()[:10]
    return f"{base_url.rstrip('/')}#card-{h}"


def _text(parent, selector: str | None) -> str | None:
    if not selector:
        return None
    el = parent.select_one(selector)
    if not el:
        return None
    return el.get_text(" ", strip=True) or None


def _text_or_label(parent, css_sel: str | None, full_text: str, label_alts: Iterable[str]) -> str | None:
    """Erst CSS-Selector versuchen, dann Label-basiertes Extrahieren."""
    via_css = _text(parent, css_sel)
    if via_css:
        return via_css
    return _extract_by_label(full_text, label_alts)


def _extract_by_label(text: str, labels: Iterable[str]) -> str | None:
    if not text:
        return None
    for label in labels:
        # Wert nach 'Label: ' bis zum naechsten bekannten Label oder Zeilenende.
        # ALL_LABELS ist nach Laenge sortiert, damit 'Veröffentlichung' nicht
        # von 'Ort' zerschnitten wird.
        stop = "|".join(re.escape(l) for l in ALL_LABELS if l != label) or "$"
        pattern = rf"{re.escape(label)}\s*[:>]?\s*(.+?)(?=\s*(?:{stop})\s*[:>]|\s*$)"
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            value = m.group(1).strip(" .,;|·:")
            # Hidden-Werte rausfiltern (z.B. evergabe.de "Nach Freischalten sichtbar")
            if value and "freischalten" not in value.lower() and "login" not in value.lower():
                return value[:200]
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


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    v = re.sub(r"\s+", " ", value).strip(" .,;|·")
    if not v:
        return None
    if "freischalten" in v.lower() or "nach login" in v.lower():
        return None
    return v


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
