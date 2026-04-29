"""Persistiert Statistiken des letzten Suchlaufs in data/last_run.json."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT


log = logging.getLogger(__name__)
_PATH = PROJECT_ROOT / "data" / "last_run.json"


def save_run_state(stats: dict) -> None:
    payload = dict(stats)
    payload.setdefault("saved_at", datetime.utcnow().isoformat())
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(payload, default=str, ensure_ascii=False, indent=2))
    except OSError as exc:  # pragma: no cover
        log.warning("Konnte last_run.json nicht schreiben: %s", exc)


def load_run_state() -> dict | None:
    if not _PATH.exists():
        return None
    try:
        return json.loads(_PATH.read_text())
    except (OSError, ValueError) as exc:  # pragma: no cover
        log.warning("Konnte last_run.json nicht lesen: %s", exc)
        return None
