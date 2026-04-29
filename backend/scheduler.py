"""APScheduler-Setup fuer taegliche Suche."""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .pipeline import run_pipeline


log = logging.getLogger(__name__)
_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler
    sched = BackgroundScheduler(timezone="Europe/Berlin")
    trigger = CronTrigger(hour=settings.scheduler_hour, minute=settings.scheduler_minute)
    sched.add_job(
        _job_run,
        trigger=trigger,
        id="daily_search",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    _scheduler = sched
    log.info(
        "Scheduler gestartet – taeglich um %02d:%02d",
        settings.scheduler_hour, settings.scheduler_minute,
    )
    return sched


def shutdown_scheduler() -> None:
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


def _job_run() -> None:
    log.info("Geplanter Suchlauf startet…")
    try:
        stats = run_pipeline()
        log.info("Geplanter Suchlauf fertig: %s", stats)
    except Exception as exc:  # pragma: no cover
        log.exception("Geplanter Suchlauf fehlgeschlagen: %s", exc)
