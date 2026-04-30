"""Einmal-Migrationen / Backfills, die beim Start laufen.

Idempotent: jede Funktion prueft selbst ob sie noch was zu tun hat.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text

from .database import SessionLocal, engine
from .models import Tender
from .region_resolver import infer_region


log = logging.getLogger(__name__)


def _column_exists(table: str, column: str) -> bool:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def add_score_breakdown_column() -> bool:
    """Fuegt die Spalte tenders.score_breakdown bei Bestandsdatenbanken nach.

    Liefert True wenn die Spalte angelegt wurde.
    """
    if _column_exists("tenders", "score_breakdown"):
        return False
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE tenders ADD COLUMN score_breakdown TEXT"))
        log.info("Migration: Spalte tenders.score_breakdown hinzugefuegt.")
        return True
    except Exception as exc:  # pragma: no cover
        log.exception("Migration score_breakdown fehlgeschlagen: %s", exc)
        return False


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
        "score_breakdown_added": add_score_breakdown_column(),
        "regions_backfilled": backfill_regions(),
    }
