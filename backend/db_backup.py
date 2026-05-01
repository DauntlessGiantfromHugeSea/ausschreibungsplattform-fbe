"""Automatisches DB-Backup mit Retention.

Wird vor jedem Reset und vor jedem Suchlauf aufgerufen, falls die
SQLite-DB existiert. Loescht alte Backups (default: aelter als 7 Tage).
"""
from __future__ import annotations

import gzip
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from .config import PROJECT_ROOT, settings


log = logging.getLogger(__name__)

BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
RETENTION_DAYS = 7
MIN_INTERVAL_MINUTES = 30  # nicht oefter als alle 30 Min ein Backup


def _db_file() -> Path | None:
    """Pfad zur SQLite-DB, falls die App eine SQLite-DB nutzt."""
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///"):]
    p = Path(raw) if raw.startswith("/") else (PROJECT_ROOT / raw)
    return p if p.exists() else None


def _last_backup_time() -> datetime | None:
    if not BACKUP_DIR.exists():
        return None
    backups = sorted(BACKUP_DIR.glob("tenders-*.db.gz"), key=lambda p: p.stat().st_mtime)
    if not backups:
        return None
    return datetime.fromtimestamp(backups[-1].stat().st_mtime)


def maybe_backup(force: bool = False) -> Path | None:
    """Erstellt ein gzip-komprimiertes Backup der SQLite-DB.

    Wenn force=False (default) und das letzte Backup juenger als
    MIN_INTERVAL_MINUTES ist, wird uebersprungen - so wird die Disk
    nicht durch jeden Suchlauf voll geschrieben.
    """
    db = _db_file()
    if db is None:
        return None

    if not force:
        last = _last_backup_time()
        if last and (datetime.now() - last) < timedelta(minutes=MIN_INTERVAL_MINUTES):
            log.debug("Backup uebersprungen - letztes Backup vor %s.",
                      datetime.now() - last)
            return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out = BACKUP_DIR / "tenders-{}.db.gz".format(stamp)
    try:
        with db.open("rb") as src, gzip.open(out, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        log.info("DB-Backup: %s (%d KB)", out.name, out.stat().st_size // 1024)
    except OSError as exc:  # pragma: no cover
        log.warning("Backup fehlgeschlagen: %s", exc)
        return None

    _prune_old_backups()
    return out


def _prune_old_backups() -> None:
    if not BACKUP_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    for f in BACKUP_DIR.glob("tenders-*.db.gz"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
                log.info("Altes Backup geloescht: %s", f.name)
        except OSError:  # pragma: no cover
            pass


def list_backups() -> List[dict]:
    """Liefert eine Liste aller Backups, neuester zuerst."""
    if not BACKUP_DIR.exists():
        return []
    out = []
    for f in sorted(BACKUP_DIR.glob("tenders-*.db.gz"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        st = f.stat()
        out.append({
            "name": f.name,
            "path": str(f),
            "size_kb": st.st_size // 1024,
            "created_at": datetime.fromtimestamp(st.st_mtime),
        })
    return out
