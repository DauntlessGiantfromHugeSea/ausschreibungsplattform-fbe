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
        # HTTP-Header muessen ASCII sein – nicht-ASCII Zeichen (z.B. Umlaute)
        # rauswerfen, statt UnicodeEncodeError beim Client-Init zu provozieren.
        ua = settings.scraper_user_agent.encode("ascii", "ignore").decode("ascii") \
            or "FBE-Ausschreibungsbot/1.0"
        self._client = httpx.Client(
            headers={"User-Agent": ua},
            timeout=settings.http_timeout,
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
        rp = self._load_robots()
        return rp.can_fetch(settings.scraper_user_agent, url)

    # --- HTTP --------------------------------------------------------
    def get(self, url: str, **kwargs) -> httpx.Response:
        if not self.can_fetch(url):
            raise PermissionError(f"robots.txt verbietet Abruf: {url}")
        log.debug("[%s] GET %s", self.name, url)
        return self._client.get(url, **kwargs)

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
