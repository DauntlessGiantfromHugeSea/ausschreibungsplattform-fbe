"""Startpunkt: FastAPI-Server + APScheduler.

Aufruf:
    python run.py
"""
from __future__ import annotations

import logging
import os

import uvicorn

from backend.api import app  # noqa: F401  – wird von uvicorn referenziert
from backend.config import settings
from backend.database import init_db
from backend.scheduler import start_scheduler


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main() -> None:
    init_db()
    start_scheduler()
    uvicorn.run(
        "backend.api:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()
