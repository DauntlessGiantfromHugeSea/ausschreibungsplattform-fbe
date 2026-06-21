"""Scraper fuer den OpenData-Bulk-Export von oeffentlichevergabe.de (DOEE).

Einziger Endpoint:
    GET /api/notice-exports?pubDay=YYYY-MM-DD&format=csv.zip
    GET /api/notice-exports?pubMonth=YYYY-MM&format=csv.zip

Liefert ein ZIP mit einer (oder mehreren) CSV-Datei(en) - eine Zeile pro
veroeffentlichter Bekanntmachung. Wir holen die letzten N Tage, mergen
alle Zeilen, filtern auf die FBE-Cluster-Keywords (Tiefbau, Verfuellung,
Spundwand, Leitungsbau, Fluessigboden, etc.) und liefern TenderItems.

Format-Alternativen 'eforms.zip' (XML) und 'ocds.zip' (JSON) sind moeglich,
falls man Detailfelder + Attachment-URIs direkt aus dem Export ziehen
will. Default ist 'csv.zip' weil leicht zu parsen.

YAML-Konfig:

    - name: "oeffentlichevergabe.de (DOEE)"
      enabled: true
      scraper: "oeffentlichevergabe_api"
      base_url: "https://oeffentlichevergabe.de"
      strategy: "api"
      config:
        export_format: "csv.zip"   # oder eforms.zip / ocds.zip
        lookback_days: 7           # heute - N Tage werden gezogen
        max_items: 2000
        filter_by_terms: true
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import date, datetime, timedelta
from typing import List
from urllib.parse import urljoin

import httpx

from .base import BaseScraper, TenderItem


log = logging.getLogger(__name__)


FORMAT_ACCEPT = {
    "csv.zip":    "application/vnd.bekanntmachungsservice.csv.zip+zip",
    "eforms.zip": "application/vnd.bekanntmachungsservice.eforms.zip+zip",
    "ocds.zip":   "application/vnd.bekanntmachungsservice.ocds.zip+zip",
}


class OeffentlichevergabeApiScraper(BaseScraper):
    name = "oeffentlichevergabe_api"

    def fetch(self, terms: List[str]) -> List[TenderItem]:
        fmt = (self.config.get("export_format") or "csv.zip").lower()
        if fmt not in FORMAT_ACCEPT:
            log.warning("[%s] Unbekanntes Format '%s', fallback csv.zip", self.name, fmt)
            fmt = "csv.zip"
        lookback = int(self.config.get("lookback_days", 7))
        max_items = int(self.config.get("max_items", 2000))
        filter_by_terms = bool(self.config.get("filter_by_terms", True))

        days = [date.today() - timedelta(days=i) for i in range(1, lookback + 1)]
        all_rows: list[dict] = []
        for d in days:
            url = urljoin(self.base_url + "/", f"api/notice-exports?pubDay={d.isoformat()}&format={fmt}")
            try:
                resp = self._client.get(url, headers={"Accept": FORMAT_ACCEPT[fmt]}, timeout=60)
            except httpx.HTTPError as exc:
                log.info("[%s] %s: %s", self.name, d, exc)
                continue
            if resp.status_code != 200 or not resp.content:
                log.info("[%s] %s -> HTTP %s (%d bytes)", self.name, d, resp.status_code, len(resp.content or b""))
                continue
            rows = self._extract_rows(resp.content, fmt)
            log.info("[%s] %s -> %d Eintraege", self.name, d, len(rows))
            all_rows.extend(rows)
            if len(all_rows) >= max_items:
                break

        items: dict[str, TenderItem] = {}
        match_terms = [t.lower() for t in (terms or []) if t]
        for row in all_rows[:max_items]:
            item = self._row_to_item(row)
            if not item or not item.url:
                continue
            if filter_by_terms and match_terms:
                hay = " ".join([
                    (item.title or ""), (item.description or ""),
                    (item.contracting_authority or ""), (item.location or ""),
                ]).lower()
                if not any(t in hay for t in match_terms):
                    continue
            items[item.url] = item
        log.info("[%s] %d Treffer nach Keyword-Filter", self.name, len(items))
        return list(items.values())

    # ------------------------------------------------------------------
    def _extract_rows(self, blob: bytes, fmt: str) -> list[dict]:
        """Entpackt das ZIP und liefert eine Liste von Row-Dicts."""
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            log.warning("[%s] Keine gueltige ZIP-Datei", self.name)
            return []
        out: list[dict] = []
        for name in zf.namelist():
            if not name or name.endswith("/"):
                continue
            try:
                data = zf.read(name)
            except Exception as exc:
                log.info("[%s] ZIP-Read %s: %s", self.name, name, exc)
                continue
            if fmt == "csv.zip" and name.lower().endswith(".csv"):
                out.extend(self._parse_csv(data))
            elif fmt == "ocds.zip" and name.lower().endswith(".json"):
                out.extend(self._parse_ocds(data))
            # eForms (.xml) parsen wir vorerst nicht - CSV/OCDS reichen.
        return out

    def _parse_csv(self, data: bytes) -> list[dict]:
        # Bekanntmachungsservice nutzt UTF-8 mit ; oder , als Separator. csv.Sniffer.
        text = data.decode("utf-8", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        return [r for r in reader if r]

    def _parse_ocds(self, data: bytes) -> list[dict]:
        import json
        try:
            doc = json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            return []
        # OCDS-Release-Package: {"releases":[{...}, ...]} oder {"records":[...]}
        rows = []
        for release in doc.get("releases") or []:
            tender = release.get("tender") or {}
            buyer = (release.get("buyer") or {}).get("name")
            rows.append({
                "title": tender.get("title"),
                "description": tender.get("description"),
                "buyer": buyer,
                "url": (release.get("url") or tender.get("url")
                        or (tender.get("documents") or [{}])[0].get("url")),
                "publication_date": release.get("date"),
                "deadline": ((tender.get("tenderPeriod") or {}).get("endDate")),
            })
        return rows

    def _row_to_item(self, row: dict) -> TenderItem | None:
        # CSV-Spaltennamen variieren - wir greifen mehrere uebliche Bezeichner ab.
        def pick(*keys, default=None):
            for k in keys:
                v = row.get(k)
                if v not in (None, "", "null"):
                    return v
            return default

        title = pick("title", "Titel", "noticeTitle", "tender.title", "TenderTitle")
        if not title:
            return None
        url = pick("url", "URL", "noticeUrl", "publicationUrl", "tender.url", "uri")
        if not url:
            # Aus Notice-ID bauen, falls vorhanden
            nid = pick("noticeId", "notice_id", "id", "publicationId", "ocid")
            if nid:
                url = urljoin(self.base_url + "/", f"ui/de/notices/{nid}")
        if not url:
            return None

        buyer = pick("buyer", "buyer.name", "Auftraggeber", "contractingAuthority", "vergabestelle")
        location = pick("location", "Ort", "place", "erfuellungsort", "buyer.address.locality")
        description = pick("description", "Beschreibung", "tender.description", "shortDescription")
        cpv_raw = pick("cpv", "cpvCode", "CPV", "tender.classification.id", default="")
        cpv_codes = [c.strip() for c in str(cpv_raw).split(";") if c.strip()][:5]

        pub = _parse_date(pick("publication_date", "publicationDate", "veroeffentlichungsdatum", "datePublished"))
        deadline = _parse_date(pick("deadline", "Frist", "submissionDeadline", "tenderPeriod.endDate"))

        return TenderItem(
            title=str(title)[:500],
            portal=self.name,
            url=str(url)[:500],
            contracting_authority=str(buyer)[:300] if buyer else None,
            location=str(location)[:200] if location else None,
            description=str(description)[:1000] if description else None,
            cpv_codes=cpv_codes,
            publication_date=pub,
            deadline=deadline,
        )


def _parse_date(value) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d.%m.%Y", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None
