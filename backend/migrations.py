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


# Frueher hatten BundScraper / TedScraper portal="bund.de" / "TED" hartcodiert.
# Heute liefern sie self.name (= YAML-Eintragsname). Damit der Filter im
# Dashboard ueber Bestand + neue Eintraege gleich heisst, mappen wir die alten
# Werte auf die YAML-Namen.
PORTAL_NAME_MIGRATIONS = {
    "bund.de": "bund.de Service-Portal",
    "TED": "TED - Tenders Electronic Daily",
}


def normalize_portal_names() -> dict:
    """Aktualisiert tenders.portal von alten hartcodierten Werten auf YAML-Namen."""
    db = SessionLocal()
    out: dict[str, int] = {}
    try:
        for old, new in PORTAL_NAME_MIGRATIONS.items():
            n = (
                db.query(Tender)
                .filter(Tender.portal == old)
                .update({Tender.portal: new}, synchronize_session=False)
            )
            if n:
                out[old] = n
        if out:
            db.commit()
            log.info("Migration: portal-Namen umbenannt: %s", out)
    except Exception as exc:  # pragma: no cover
        db.rollback()
        log.exception("Migration normalize_portal_names fehlgeschlagen: %s", exc)
    finally:
        db.close()
    return out


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


def add_user_notify_columns() -> bool:
    """Fuegt die Spalten users.notify_frequency / notify_min_score /
    notify_last_sent_at bei Bestandsdatenbanken nach."""
    added = False
    statements = [
        ("notify_frequency",
         "ALTER TABLE users ADD COLUMN notify_frequency VARCHAR(10) DEFAULT 'off' NOT NULL"),
        ("notify_min_score",
         "ALTER TABLE users ADD COLUMN notify_min_score INTEGER DEFAULT 60 NOT NULL"),
        ("notify_last_sent_at",
         "ALTER TABLE users ADD COLUMN notify_last_sent_at DATETIME"),
    ]
    for col, ddl in statements:
        if _column_exists("users", col):
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
            log.info("Migration: Spalte users.%s hinzugefuegt.", col)
            added = True
        except Exception as exc:  # pragma: no cover
            log.exception("Migration users.%s fehlgeschlagen: %s", col, exc)
    return added


def run_all() -> dict:
    """Alle Migrationen einmal beim App-Start laufen lassen."""
    return {
        "score_breakdown_added": add_score_breakdown_column(),
        "portal_names_normalized": normalize_portal_names(),
        "regions_backfilled": backfill_regions(),
        "user_notify_columns_added": add_user_notify_columns(),
    }
