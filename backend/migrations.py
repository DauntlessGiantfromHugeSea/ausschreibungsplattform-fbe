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


def add_tender_ai_columns() -> bool:
    """Fuegt die Spalten tenders.ai_analysis und tenders.ai_analyzed_at
    bei Bestandsdatenbanken nach."""
    added = False
    statements = [
        ("ai_analysis",     "ALTER TABLE tenders ADD COLUMN ai_analysis TEXT"),
        ("ai_analyzed_at",  "ALTER TABLE tenders ADD COLUMN ai_analyzed_at DATETIME"),
    ]
    for col, ddl in statements:
        if _column_exists("tenders", col):
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
            log.info("Migration: Spalte tenders.%s hinzugefuegt.", col)
            added = True
        except Exception as exc:  # pragma: no cover
            log.exception("Migration tenders.%s fehlgeschlagen: %s", col, exc)
    return added


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


def add_search_profile_keywords_column() -> bool:
    """Fuegt search_profiles.keywords (JSON-Text-Liste) bei Bestandsdatenbanken nach."""
    if _column_exists("search_profiles", "keywords"):
        return False
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE search_profiles ADD COLUMN keywords TEXT"))
        log.info("Migration: Spalte search_profiles.keywords hinzugefuegt.")
        return True
    except Exception as exc:  # pragma: no cover
        log.exception("Migration search_profiles.keywords fehlgeschlagen: %s", exc)
        return False


def create_profile_users_table() -> bool:
    """Legt die M2M-Tabelle profile_users an, falls noch nicht vorhanden."""
    insp = inspect(engine)
    if "profile_users" in insp.get_table_names():
        return False
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE profile_users ("
                "profile_id INTEGER NOT NULL, "
                "user_id INTEGER NOT NULL, "
                "PRIMARY KEY (profile_id, user_id), "
                "FOREIGN KEY (profile_id) REFERENCES search_profiles(id) ON DELETE CASCADE, "
                "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
                ")"
            ))
        log.info("Migration: Tabelle profile_users angelegt.")
        return True
    except Exception as exc:  # pragma: no cover
        log.exception("Migration profile_users fehlgeschlagen: %s", exc)
        return False


def migrate_viewer_role_to_user() -> int:
    """Bestehende Rolle 'viewer' wird durch 'user' (= profilgebunden) ersetzt.

    Liefert die Anzahl der aktualisierten User-Datensaetze.
    """
    db = SessionLocal()
    try:
        n = db.execute(
            text("UPDATE users SET role='user' WHERE role='viewer'")
        ).rowcount or 0
        if n:
            db.commit()
            log.info("Migration: %d User-Rollen von 'viewer' auf 'user' migriert.", n)
        return int(n)
    except Exception as exc:  # pragma: no cover
        db.rollback()
        log.exception("Migration viewer->user fehlgeschlagen: %s", exc)
        return 0
    finally:
        db.close()


def add_portal_login_credential_columns() -> bool:
    """Stellt sicher, dass portal_logins.username/password existieren.

    Frueheres Schema hatte username_env/password_env. Neuer simpler Ansatz:
    Klartext direkt in der DB. Spalten werden idempotent angelegt; die alten
    *_env-Spalten bleiben unangetastet, werden aber nicht mehr genutzt.
    """
    insp = inspect(engine)
    if "portal_logins" not in insp.get_table_names():
        return False
    cols = {c["name"] for c in insp.get_columns("portal_logins")}
    added = False
    statements = []
    if "username" not in cols:
        statements.append(
            "ALTER TABLE portal_logins ADD COLUMN username VARCHAR(200) NOT NULL DEFAULT ''"
        )
    if "password" not in cols:
        statements.append(
            "ALTER TABLE portal_logins ADD COLUMN password VARCHAR(500) NOT NULL DEFAULT ''"
        )
    for ddl in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
            added = True
        except Exception as exc:  # pragma: no cover
            log.exception("Migration portal_logins credentials fehlgeschlagen: %s", exc)
    if added:
        log.info("Migration: portal_logins.username/password hinzugefuegt.")
    return added


def add_portal_login_test_requested_column() -> bool:
    """portal_logins.test_requested_at - Flag fuer Admin-getriggerten Login-Test."""
    if "portal_logins" not in {t for t in inspect(engine).get_table_names()}:
        return False
    cols = {c["name"] for c in inspect(engine).get_columns("portal_logins")}
    if "test_requested_at" in cols:
        return False
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE portal_logins ADD COLUMN test_requested_at DATETIME"))
        log.info("Migration: portal_logins.test_requested_at hinzugefuegt.")
        return True
    except Exception as exc:  # pragma: no cover
        log.exception("Migration test_requested_at fehlgeschlagen: %s", exc)
        return False


def run_all() -> dict:
    """Alle Migrationen einmal beim App-Start laufen lassen."""
    return {
        "score_breakdown_added": add_score_breakdown_column(),
        "portal_names_normalized": normalize_portal_names(),
        "regions_backfilled": backfill_regions(),
        "user_notify_columns_added": add_user_notify_columns(),
        "tender_ai_columns_added": add_tender_ai_columns(),
        "search_profile_keywords_added": add_search_profile_keywords_column(),
        "profile_users_table_created": create_profile_users_table(),
        "viewer_role_migrated": migrate_viewer_role_to_user(),
        "portal_login_credentials_added": add_portal_login_credential_columns(),
        "portal_login_test_flag_added": add_portal_login_test_requested_column(),
    }
