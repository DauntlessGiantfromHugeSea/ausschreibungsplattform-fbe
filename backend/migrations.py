"""Einmal-Migrationen / Backfills, die beim Start laufen.

Idempotent: jede Funktion prueft selbst ob sie noch was zu tun hat.
"""
from __future__ import annotations

import logging

from .database import SessionLocal
from .models import Tender
from .region_resolver import infer_region


log = logging.getLogger(__name__)


def backfill_regions() -> int:
    """Setzt region fuer alle Tender, bei denen es noch leer ist und sich
    aus location ableiten laesst.

    Liefert die Anzahl der aktualisierten Datensaetze.
    """
    db = SessionLocal()
    updated = 0
    try:
        candidates = (
            db.query(Tender)
            .filter((Tender.region.is_(None)) | (Tender.region == ""))
            .all()
        )
        for t in candidates:
            new_region = infer_region(t.location, t.region)
            if new_region and new_region != (t.region or ""):
                t.region = new_region
                updated += 1
        if updated:
            db.commit()
            log.info("backfill_regions: %d Tender aktualisiert.", updated)
    except Exception as exc:  # pragma: no cover
        db.rollback()
        log.exception("backfill_regions fehlgeschlagen: %s", exc)
    finally:
        db.close()
    return updated


def run_all() -> dict:
    """Alle Migrationen einmal beim App-Start laufen lassen."""
    return {
        "regions_backfilled": backfill_regions(),
    }
