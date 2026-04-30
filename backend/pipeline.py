"""Orchestrierung: Scraper -> Scoring -> Dedup -> DB -> Notify.

Robustheit:
- Jede Portal-Iteration in try/except.
- Jeder einzelne Item-Insert in try/except + per-Item-Commit, damit
  ein einzelner Fehler nicht die ganze Portal-Charge zurueckrollt.
- Sonderfaelle (leere Titel/URLs, Fingerprint-Kollisionen, DB-Fehler)
  werden geloggt aber unterbrechen den Lauf nicht.
"""
from __future__ import annotations

import importlib
import json
import logging
from datetime import datetime
from typing import List

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .config import settings
from .database import SessionLocal
from .dedup import fingerprint
from .models import Tender, TenderStatus
from .portal_config import PortalConfig, enabled_portals
from .region_resolver import infer_region
from .scoring import score_text
from .search_terms import load_search_config
from . import notify


log = logging.getLogger(__name__)


def _load_scraper(portal: PortalConfig):
    """Laedt scrapers/<portal.scraper>.py und liefert die Scraper-Klasse.

    Konvention: 'rss_generic' -> 'RssGenericScraper', 'bund' -> 'BundScraper'.
    """
    module = importlib.import_module(f"scrapers.{portal.scraper}")
    class_name = "".join(p.capitalize() for p in portal.scraper.split("_")) + "Scraper"
    if hasattr(module, class_name):
        return getattr(module, class_name)
    from scrapers.base import BaseScraper
    for attr in dir(module):
        obj = getattr(module, attr)
        if isinstance(obj, type) and issubclass(obj, BaseScraper) and obj is not BaseScraper:
            return obj
    raise ImportError(f"Keine Scraper-Klasse in {module.__name__} gefunden")


def run_pipeline() -> dict:
    """Fuehrt einen vollstaendigen Lauf aus. Liefert Statistik-Dict.

    Garantiert: kein einzelner Portal- oder Item-Fehler bricht den Lauf ab.
    """
    cfg = load_search_config()
    stats = {
        "started_at": datetime.utcnow().isoformat(),
        "portals": [],
        "new": 0,
        "updated": 0,
        "errors": 0,
    }
    new_high_relevance: List[Tender] = []

    for portal in enabled_portals():
        portal_stats = {
            "name": portal.name, "scraper": portal.scraper, "base_url": portal.base_url,
            "fetched": 0, "new": 0, "updated": 0, "errors": 0,
            "http_log": [], "sample_titles": [], "error_msg": None,
        }
        try:
            ScraperCls = _load_scraper(portal)
        except Exception as exc:
            log.exception("[%s] Konnte Scraper nicht laden: %s", portal.name, exc)
            portal_stats["errors"] = 1
            portal_stats["error_msg"] = str(exc)[:200]
            stats["errors"] += 1
            stats["portals"].append(portal_stats)
            continue

        try:
            with ScraperCls(base_url=portal.base_url, name=portal.name, config=portal.config) as scraper:
                items = scraper.fetch(cfg.query_terms)
                # HTTP-Diagnose: was wurde wirklich gehit?
                # Nur die letzten 5 Requests behalten - reicht fuer das Dashboard.
                portal_stats["http_log"] = list(scraper.http_log)[-5:]
        except Exception as exc:
            log.exception("[%s] Scraper.fetch fehlgeschlagen: %s", portal.name, exc)
            portal_stats["errors"] = 1
            portal_stats["error_msg"] = "{}: {}".format(type(exc).__name__, str(exc)[:180])
            # HTTP-Log auch im Fehlerfall, falls einzelne Requests durchgekommen sind.
            try:
                portal_stats["http_log"] = list(scraper.http_log)[-5:]  # type: ignore[name-defined]
            except Exception:
                pass
            stats["errors"] += 1
            stats["portals"].append(portal_stats)
            continue

        portal_stats["fetched"] = len(items)
        portal_stats["sample_titles"] = [(it.title or "")[:120] for it in items[:3]]
        log.info("[%s] %d Treffer", portal.name, len(items))

        for item in items:
            try:
                result = _save_item(item, cfg, new_high_relevance)
                if result == "new":
                    portal_stats["new"] += 1
                    stats["new"] += 1
                elif result == "updated":
                    portal_stats["updated"] += 1
                    stats["updated"] += 1
            except Exception as exc:
                log.warning("[%s] Item '%s' konnte nicht gespeichert werden: %s",
                            portal.name, (item.title or "")[:80], exc)
                portal_stats["errors"] += 1

        stats["portals"].append(portal_stats)

    stats["finished_at"] = datetime.utcnow().isoformat()

    if new_high_relevance and settings.notify_email:
        try:
            notify.send_high_relevance_email(new_high_relevance)
        except Exception as exc:  # pragma: no cover – SMTP
            log.warning("Mailversand fehlgeschlagen: %s", exc)

    return stats


# ---------------------------------------------------------------------------
def _save_item(item, cfg, new_high_relevance: List[Tender]) -> str | None:
    """Speichert einen einzelnen TenderItem. Liefert 'new' | 'updated' | None."""
    if not item.title or not item.url:
        log.debug("Skip Item ohne Titel/URL")
        return None

    # Bundesland aus PLZ/Ort ableiten falls noch nicht gesetzt.
    item.region = infer_region(item.location, item.region)

    fp = fingerprint(item.url, item.title, item.contracting_authority, item.deadline)
    sr = score_text(
        title=item.title,
        description=item.description,
        cpv_codes=item.cpv_codes,
        region=item.region,
        deadline=item.deadline,
        config=cfg,
    )
    matched_str = "; ".join(sr.matched_terms) if sr.matched_terms else None

    breakdown_json = json.dumps(sr.breakdown_dicts(), ensure_ascii=False)

    db = SessionLocal()
    try:
        existing = db.query(Tender).filter(Tender.fingerprint == fp).first()
        if existing:
            existing.relevance_score = sr.score
            existing.relevance_level = sr.level
            existing.matched_terms = matched_str
            existing.score_breakdown = breakdown_json
            if item.deadline:
                existing.deadline = item.deadline
            db.commit()
            return "updated"

        tender = Tender(
            title=(item.title or "")[:1000],
            portal=item.portal,
            contracting_authority=(item.contracting_authority or None),
            location=(item.location or None),
            region=(item.region or None),
            publication_date=item.publication_date,
            deadline=item.deadline,
            url=item.url,
            description=item.description,
            matched_terms=matched_str,
            relevance_score=sr.score,
            relevance_level=sr.level,
            score_breakdown=breakdown_json,
            status=TenderStatus.NEU.value,
            cpv_codes=";".join(item.cpv_codes) if item.cpv_codes else None,
            documents=json.dumps(item.documents) if item.documents else None,
            fingerprint=fp,
        )
        db.add(tender)
        try:
            db.commit()
        except IntegrityError:
            # Fingerprint-Kollision (race condition): kein Fehler, einfach skippen.
            db.rollback()
            log.debug("Fingerprint-Kollision: %s", item.url)
            return None
        if sr.score >= settings.high_relevance_threshold:
            new_high_relevance.append(tender)
        return "new"
    except SQLAlchemyError as exc:
        db.rollback()
        raise
    finally:
        db.close()
