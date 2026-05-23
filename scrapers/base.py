"""Basis-Klassen und gemeinsames Datenobjekt fuer Scraper."""
from __future__ import annotations

import logging
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlparse

import httpx

from backend.config import settings


log = logging.getLogger(__name__)


@dataclass
class TenderItem:
    """Roh-Datensatz, den jeder Scraper zurueckgibt."""

    title: str
    portal: str
    url: str
    contracting_authority: Optional[str] = None
    location: Optional[str] = None
    region: Optional[str] = None
    publication_date: Optional[datetime] = None
    deadline: Optional[datetime] = None
    description: Optional[str] = None
    cpv_codes: List[str] = field(default_factory=list)
    documents: List[str] = field(default_factory=list)


class BaseScraper:
    """Grundgeruest mit HTTP-Client, robots.txt-Pruefung und Logging."""

    name: str = "base"

    def __init__(self, base_url: str, name: str | None = None, config: dict | None = None):
        self.base_url = base_url.rstrip("/")
        if name:
            self.name = name
        # Pro-Portal-Konfig (z.B. search_url, selectors, feed_urls).
        self.config = config or {}
        # Diagnose pro Lauf: jede HTTP-Antwort wird hier aufgesammelt, damit
        # die Pipeline + das Dashboard zeigen koennen, was wirklich gecrawlt
        # wurde - auch wenn 0 Treffer extrahiert wurden.
        self.http_log: list[dict] = []
        # HTTP-Header muessen ASCII sein – nicht-ASCII Zeichen (z.B. Umlaute)
        # rauswerfen, statt UnicodeEncodeError beim Client-Init zu provozieren.
        # Per-Portal-User-Agent ueberschreibt den globalen (manche Portale
        # blocken den FBE-Bot-UA mit 403).
        ua_raw = self.config.get("user_agent") or settings.scraper_user_agent
        ua = ua_raw.encode("ascii", "ignore").decode("ascii") \
            or "FBE-Ausschreibungsbot/1.0"
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
        }
        self._client = httpx.Client(
            headers=headers,
            timeout=int(self.config.get("timeout", settings.http_timeout)),
            follow_redirects=True,
        )
        self._robots: urllib.robotparser.RobotFileParser | None = None

    # --- robots.txt --------------------------------------------------
    def _load_robots(self) -> urllib.robotparser.RobotFileParser:
        if self._robots is not None:
            return self._robots
        rp = urllib.robotparser.RobotFileParser()
        parsed = urlparse(self.base_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            resp = self._client.get(robots_url, timeout=10)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp.parse([])  # leer = alles erlaubt
        except Exception as exc:  # pragma: no cover – Netzwerk
            log.warning("robots.txt konnte nicht geladen werden (%s): %s", robots_url, exc)
            rp.parse([])
        self._robots = rp
        return rp

    def can_fetch(self, url: str) -> bool:
        # Override per Portal (config.ignore_robots = true) oder global
        # (settings.ignore_robots_global). Wird einmal pro Scraper geloggt,
        # damit der Bypass nachvollziehbar bleibt.
        if self.config.get("ignore_robots") or getattr(settings, "ignore_robots_global", False):
            if not getattr(self, "_robots_bypass_logged", False):
                log.warning("[%s] robots.txt wird ignoriert (config-flag aktiv)", self.name)
                self._robots_bypass_logged = True
            return True
        rp = self._load_robots()
        # Wir pruefen mit DEM User-Agent, mit dem wir auch tatsaechlich
        # anfragen. Wenn das Portal robots.txt-Disallow nur fuer den
        # FBE-Bot definiert hat, der per-Portal-Browser-UA aber nicht
        # blockiert ist, soll der Browser-UA durchgelassen werden.
        ua = self.config.get("user_agent") or settings.scraper_user_agent
        if rp.can_fetch(ua, url):
            return True
        # Fallback: einige Portale lehnen jeden spezifischen UA ab, lassen
        # aber '*' zu - oder umgekehrt. Wenn '*' erlaubt, akzeptieren wir.
        return rp.can_fetch("*", url)

    # --- HTTP --------------------------------------------------------
    def get(self, url: str, **kwargs) -> httpx.Response:
        if not self.can_fetch(url):
            self.http_log.append({
                "url": url, "status": None, "size": 0,
                "error": "robots.txt verbietet Abruf",
            })
            raise PermissionError(f"robots.txt verbietet Abruf: {url}")
        log.debug("[%s] GET %s", self.name, url)
        try:
            resp = self._client.get(url, **kwargs)
            self.http_log.append({
                "url": url,
                "final_url": str(resp.url) if str(resp.url) != url else None,
                "status": resp.status_code,
                "size": len(resp.content) if resp.content else 0,
                "error": None,
            })
            return resp
        except Exception as exc:
            self.http_log.append({
                "url": url, "status": None, "size": 0,
                "error": "{}: {}".format(type(exc).__name__, str(exc)[:160]),
            })
            raise

    # --- Lifecycle ---------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # --- API ---------------------------------------------------------
    def fetch(self, terms: List[str]) -> List[TenderItem]:  # pragma: no cover
        raise NotImplementedError
