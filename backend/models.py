"""ORM-Modelle."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Float,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class TenderStatus(str, Enum):
    NEU = "neu"
    GEPRUEFT = "geprüft"
    INTERESSANT = "interessant"
    UNINTERESSANT = "uninteressant"
    BEWORBEN = "beworben"
    ARCHIVIERT = "archiviert"


class Tender(Base):
    __tablename__ = "tenders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(1000), nullable=False)
    portal = Column(String(200), nullable=False, index=True)
    contracting_authority = Column(String(500), nullable=True)
    location = Column(String(300), nullable=True)
    region = Column(String(100), nullable=True, index=True)  # Bundesland
    publication_date = Column(DateTime, nullable=True, index=True)
    deadline = Column(DateTime, nullable=True, index=True)
    url = Column(String(2000), nullable=False)
    description = Column(Text, nullable=True)
    matched_terms = Column(Text, nullable=True)        # ; separated
    relevance_score = Column(Float, default=0.0, index=True)
    relevance_level = Column(String(20), default="low", index=True)  # high/medium/low
    status = Column(String(30), default=TenderStatus.NEU.value, index=True)
    notes = Column(Text, nullable=True)
    cpv_codes = Column(String(500), nullable=True)
    documents = Column(Text, nullable=True)            # JSON-encoded list
    fingerprint = Column(String(64), nullable=False, unique=True, index=True)
    # JSON-Liste mit Score-Komponenten: [{"label":..., "points":..., "detail":...}, ...]
    score_breakdown = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        Index("ix_tenders_status_score", "status", "relevance_score"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "portal": self.portal,
            "contracting_authority": self.contracting_authority,
            "location": self.location,
            "region": self.region,
            "publication_date": self.publication_date.isoformat() if self.publication_date else None,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "url": self.url,
            "description": self.description,
            "matched_terms": self.matched_terms,
            "relevance_score": self.relevance_score,
            "relevance_level": self.relevance_level,
            "status": self.status,
            "notes": self.notes,
            "cpv_codes": self.cpv_codes,
            "documents": self.documents,
        }


class User(Base):
    """Web-User mit gehashtem Passwort und Rolle.

    Rollen:
      admin  - voller Zugriff (Portale/Suchbegriffe/User-Verwaltung)
      viewer - nur Lesezugriff aufs Dashboard
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(80), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="admin", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_login_at = Column(DateTime, nullable=True)
    email = Column(String(255), nullable=True)
    invite_token = Column(String(255), nullable=True)
    invite_token_expires_at = Column(DateTime, nullable=True)
    reset_token = Column(String(255), nullable=True)
    reset_token_expires_at = Column(DateTime, nullable=True)

    # Persoenliche Benachrichtigungen: off | daily | weekly
    notify_frequency = Column(String(10), default="off", nullable=False)
    notify_min_score = Column(Integer, default=60, nullable=False)
    notify_last_sent_at = Column(DateTime, nullable=True)


class SearchProfile(Base):
    """Gespeicherter Filter, der per Klick aufs Dashboard angewendet wird.

    Felder mappen 1:1 auf die Filter-Parameter der Index-Route.
    """
    __tablename__ = "search_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(String(500), nullable=True)

    # Filterwerte (alle optional)
    query = Column(String(500), nullable=True)        # Volltextsuche
    portal = Column(String(200), nullable=True)
    region = Column(String(100), nullable=True)
    status = Column(String(30), nullable=True)
    level = Column(String(20), nullable=True)         # high/medium/low
    score_min = Column(Integer, nullable=True)
    score_max = Column(Integer, nullable=True)
    deadline_days = Column(Integer, nullable=True)    # naechste N Tage

    sort = Column(String(30), default="score_desc")
    notify = Column(Integer, default=0)               # 0/1: bei neuen Treffern mailen

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_query_string(self) -> str:
        """Liefert die URL-Parameter, die dieses Profil aufs Dashboard anwenden."""
        from datetime import datetime as _dt, timedelta as _td
        from urllib.parse import urlencode

        params: dict[str, str] = {}
        if self.query: params["q"] = self.query
        if self.portal: params["portal"] = self.portal
        if self.region: params["region"] = self.region
        if self.status: params["status"] = self.status
        if self.level: params["level"] = self.level
        if self.score_min is not None: params["score_min"] = str(self.score_min)
        if self.score_max is not None: params["score_max"] = str(self.score_max)
        if self.deadline_days:
            today = _dt.utcnow().date()
            params["deadline_from"] = today.isoformat()
            params["deadline_to"] = (today + _td(days=self.deadline_days)).isoformat()
        if self.sort: params["sort"] = self.sort
        return urlencode(params)


class Comment(Base):
    """Kommentar zu einer Ausschreibung.

    username wird denormalisiert mitgespeichert, damit Kommentare auch
    erhalten bleiben, wenn ein User-Account spaeter geloescht wird.
    """
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tender_id = Column(Integer, ForeignKey("tenders.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                     nullable=True, index=True)
    username = Column(String(80), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    tender = relationship("Tender", backref="comments")


class TenderEvent(Base):
    """Audit-Eintrag pro Ausschreibung: Status-Wechsel, Mail-Versand etc.

    username wird denormalisiert mitgespeichert, damit der Verlauf auch dann
    lesbar bleibt, wenn ein User-Account spaeter geloescht wird.
    """
    __tablename__ = "tender_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tender_id = Column(Integer, ForeignKey("tenders.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                     nullable=True, index=True)
    username = Column(String(80), nullable=False)
    event_type = Column(String(40), nullable=False, index=True)  # 'status' | 'mail_sent'
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    tender = relationship("Tender", backref="events")
