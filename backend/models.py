"""ORM-Modelle."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    Float,
    Index,
)
from sqlalchemy.orm import DeclarativeBase


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
