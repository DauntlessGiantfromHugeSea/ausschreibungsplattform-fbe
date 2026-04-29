"""SQLAlchemy-Engine, Session-Factory und init_db()."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings, PROJECT_ROOT

# Sicherstellen, dass /data existiert (fuer SQLite-Default).
(PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db() -> None:
    """Erzeugt das Schema falls noch nicht vorhanden."""
    from . import models  # noqa: F401  – stellt sicher, dass Modelle registriert sind

    models.Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Session:
    """Context-Manager fuer transaktionalen Zugriff."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Session:
    """FastAPI-Dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
