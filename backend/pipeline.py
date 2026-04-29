"""Orchestrierung: Scraper -> Scoring -> Dedup -> DB -> Notify."""
from __future__ import annotations

import importlib
import json
import logging
from datetime import datetime
from typing import List

from .config import settings
from .database import session_scope
from .dedup import fingerprint
from .models import Tender, TenderStatus
from .portal_config import PortalConfig, enabled_portals
from .scoring import score_text
from .search_terms import load_search_config
from . import notify


log = logging.getLogger(__name__)


def _load_scraper(portal: PortalConfig):
    """Laedt scrapers/<portal.scraper>.py und liefert die Scraper-Klasse."""
    module = importlib.import_module(f"scrapers.{portal.scraper}")
    # Konvention: Klassenname ist <Capitalized>Scraper
    class_name = f"{portal.scraper.capitalize()}Scraper"
    if not hasattr(module, class_name):
        # Fallback: erste BaseScraper-Subklasse im Modul.
        from scrapers.base import BaseScraper
        for attr in dir(module):
            obj = getattr(module, attr)
            if isinstance(obj, type) and issubclass(obj, BaseScraper) and obj is not BaseScraper:
                return obj
        raise ImportError(f"Keine Scraper-Klasse in {module.__name__} gefunden")
    return getattr(module, class_name)


def run_pipeline() -> dict:
    """Fuehrt einen vollstaendigen Lauf aus. Liefert Statistik-Dict."""
    cfg = load_search_config()
    stats = {"started_at": datetime.utcnow().isoformat(), "portals": [], "new": 0, "updated": 0, "errors": 0}
    new_high_relevance: List[Tender] = []

    for portal in enabled_portals():
        portal_stats = {"name": portal.name, "fetched": 0, "new": 0, "errors": 0}
        try:
            ScraperCls = _load_scraper(portal)
        except Exception as exc:
            log.exception("Konnte Scraper fuer %s nicht laden: %s", portal.name, exc)
            portal_stats["errors"] = 1
            stats["errors"] += 1
            stats["portals"].append(portal_stats)
            continue

        try:
            with ScraperCls(base_url=portal.base_url, name=portal.name) as scraper:
                items = scraper.fetch(cfg.query_terms)
        except Exception as exc:
            log.exception("Scraper %s fehlgeschlagen: %s", portal.name, exc)
            portal_stats["errors"] = 1
            stats["errors"] += 1
            stats["portals"].append(portal_stats)
            continue

        portal_stats["fetched"] = len(items)
        log.info("[%s] %d Treffer", portal.name, len(items))

        with session_scope() as db:
            for item in items:
                fp = fingerprint(item.url, item.title, item.contracting_authority, item.deadline)
                existing = db.query(Tender).filter(Tender.fingerprint == fp).first()
                sr = score_text(
                    title=item.title,
                    description=item.description,
                    cpv_codes=item.cpv_codes,
                    region=item.region,
                    deadline=item.deadline,
                    config=cfg,
                )
                matched_str = "; ".join(sr.matched_terms)
                if existing:
                    # Aktualisieren, falls Frist/Score sich geaendert haben
                    existing.relevance_score = sr.score
                    existing.relevance_level = sr.level
                    existing.matched_terms = matched_str
                    if item.deadline:
                        existing.deadline = item.deadline
                    stats["updated"] += 1
                    continue
                tender = Tender(
                    title=item.title[:1000],
                    portal=item.portal,
                    contracting_authority=item.contracting_authority,
                    location=item.location,
                    region=item.region,
                    publication_date=item.publication_date,
                    deadline=item.deadline,
                    url=item.url,
                    description=item.description,
                    matched_terms=matched_str,
                    relevance_score=sr.score,
                    relevance_level=sr.level,
                    status=TenderStatus.NEU.value,
                    cpv_codes=";".join(item.cpv_codes) if item.cpv_codes else None,
                    documents=json.dumps(item.documents) if item.documents else None,
                    fingerprint=fp,
                )
                db.add(tender)
                portal_stats["new"] += 1
                stats["new"] += 1
                if sr.score >= settings.high_relevance_threshold:
                    new_high_relevance.append(tender)
        stats["portals"].append(portal_stats)

    stats["finished_at"] = datetime.utcnow().isoformat()

    if new_high_relevance and settings.notify_email:
        try:
            notify.send_high_relevance_email(new_high_relevance)
        except Exception as exc:  # pragma: no cover – SMTP
            log.warning("Mailversand fehlgeschlagen: %s", exc)

    return stats
