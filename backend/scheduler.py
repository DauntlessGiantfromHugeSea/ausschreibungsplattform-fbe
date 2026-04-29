"""APScheduler-Setup.

Zwei Modi:
  * Continuous : alle X Minuten ein Suchlauf (Default 60 Min)
  * Daily      : einmal pro Tag zu fester Uhrzeit (klassische Cron-Variante)

Steuerung ueber .env:
  SCHEDULER_INTERVAL_MINUTES=60   # > 0 = Continuous-Mode
  SCHEDULER_INTERVAL_MINUTES=0    # taeglicher Cron (SCHEDULER_HOUR/MINUTE)
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings, PROJECT_ROOT
from .pipeline import run_pipeline
from .run_state import save_run_state


log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None
_pipeline_lock = threading.Lock()


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    sched = BackgroundScheduler(timezone="Europe/Berlin")

    if settings.scheduler_interval_minutes > 0:
        trigger = IntervalTrigger(minutes=settings.scheduler_interval_minutes)
        log.info("Scheduler-Modus: alle %d Minuten", settings.scheduler_interval_minutes)
    else:
        trigger = CronTrigger(hour=settings.scheduler_hour, minute=settings.scheduler_minute)
        log.info(
            "Scheduler-Modus: taeglich %02d:%02d",
            settings.scheduler_hour, settings.scheduler_minute,
        )

    sched.add_job(
        run_pipeline_with_lock,
        trigger=trigger,
        id="search_job",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(),  # gleich beim Start einmal laufen
    )
    sched.start()
    _scheduler = sched
    return sched


def shutdown_scheduler() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


def run_pipeline_with_lock() -> dict | None:
    """Fuehrt einen Suchlauf aus, falls nicht schon einer laeuft."""
    if not _pipeline_lock.acquire(blocking=False):
        log.info("Suchlauf laeuft bereits - dieser Trigger wird uebersprungen.")
        return None
    try:
        log.info("Suchlauf gestartet…")
        stats = run_pipeline()
        save_run_state(stats)
        log.info(
            "Suchlauf fertig: %d neu, %d aktualisiert, %d Fehler.",
            stats.get("new", 0), stats.get("updated", 0), stats.get("errors", 0),
        )
        return stats
    except Exception as exc:  # pragma: no cover
        log.exception("Suchlauf fehlgeschlagen: %s", exc)
        save_run_state({"error": str(exc), "started_at": datetime.utcnow().isoformat()})
        return None
    finally:
        _pipeline_lock.release()


def is_pipeline_running() -> bool:
    if _pipeline_lock.acquire(blocking=False):
        _pipeline_lock.release()
        return False
    return True


def next_run_time() -> datetime | None:
    if not _scheduler or not _scheduler.running:
        return None
    job = _scheduler.get_job("search_job")
    return job.next_run_time if job else None
