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
    Table,
    Text,
    Float,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


profile_users = Table(
    "profile_users",
    Base.metadata,
    Column("profile_id", Integer, ForeignKey("search_profiles.id", ondelete="CASCADE"),
           primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"),
           primary_key=True),
)


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
    # KI-Analyse (Fluessigboden-Eignung + Kosteneinsparungs-Schaetzung).
    # Wird beim ersten Oeffnen der Detailseite generiert und gecached.
    ai_analysis = Column(Text, nullable=True)        # JSON-encoded
    ai_analyzed_at = Column(DateTime, nullable=True)
    # Tiefe Claude-Analyse - on-demand via Detail-Button.
    # Strukturiertes JSON: eckdaten, anhaenge, einsparungspotenzial,
    # fluessigboden_eignung, gesamteinschaetzung.
    claude_analysis = Column(Text, nullable=True)
    claude_analyzed_at = Column(DateTime, nullable=True)
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

    # KI-Funktionen (Claude-Analyse, Assistent) - default fuer 'user'
    # Rolle deaktiviert, kann pro User vom Admin freigeschaltet werden.
    ai_enabled = Column(Boolean, default=False, nullable=False)

    assigned_profiles = relationship(
        "SearchProfile",
        secondary=profile_users,
        back_populates="assigned_users",
        lazy="selectin",
    )


class SearchProfile(Base):
    """Gespeicherter Filter, der per Klick aufs Dashboard angewendet wird.

    Felder mappen 1:1 auf die Filter-Parameter der Index-Route.

    keywords (JSON-encoded list) liefert die OR-Match-Begriffe fuer
    restricted-User: ein Tender matched, wenn IRGENDEINER der Begriffe
    in Titel oder Beschreibung vorkommt. Wird AND-kombiniert mit den
    uebrigen Filter-Feldern (portal, region, score_min ...).
    """
    __tablename__ = "search_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(String(500), nullable=True)

    # Filterwerte (alle optional)
    keywords = Column(Text, nullable=True)            # JSON-Liste von OR-Begriffen
    query = Column(String(500), nullable=True)        # Volltextsuche (Legacy/Admin)
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

    def keyword_list(self) -> list[str]:
        """Parst das JSON-Feld 'keywords' als Liste. Toleriert leere/alte Werte."""
        import json
        raw = (self.keywords or "").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except (ValueError, TypeError):
            pass
        # Fallback: Komma/Newline-getrennte Strings akzeptieren
        return [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]

    assigned_users = relationship(
        "User",
        secondary="profile_users",
        back_populates="assigned_profiles",
        lazy="selectin",
    )


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


class PortalLogin(Base):
    """Admin-pflegbare Login-Konfiguration pro Portal.

    Der Enricher-Container holt diese ueber /api/internal/portal-logins.
    Passwoerter liegen im Klartext (gleicher Threat-Level wie .env auf
    dem selben Server) - werden im UI maskiert dargestellt und niemals
    geloggt.
    """
    __tablename__ = "portal_logins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    host = Column(String(200), nullable=False, unique=True)
    label = Column(String(200), nullable=True)
    login_url = Column(String(500), nullable=False)
    username_selector = Column(String(500), nullable=False)
    password_selector = Column(String(500), nullable=False)
    submit_selector = Column(String(500), nullable=False)
    success_selector = Column(String(500), nullable=True)
    # Direkter Klartext - liegt in der lokalen SQLite-DB neben anderen
    # Sensitiv-Feldern (Passwort-Hashes). Der UI maskiert das Eingabefeld.
    username = Column(String(200), nullable=False, default="")
    password = Column(String(500), nullable=False, default="")
    # Altlasten aus dem frueheren env-name-Schema. Werden nicht mehr genutzt,
    # bleiben aber im Model damit der INSERT bestehende DBs mit
    # NOT NULL-Constraint nicht bricht.
    username_env = Column(String(120), nullable=False, default="")
    password_env = Column(String(120), nullable=False, default="")
    enabled = Column(Boolean, default=True, nullable=False)
    # Vom Admin gesetzt: wenn != NULL, fuehrt der Enricher beim naechsten
    # Poll einen expliziten Login-Test aus und setzt das Feld wieder
    # auf NULL.
    test_requested_at = Column(DateTime, nullable=True)
    # Vom Enricher gesetzt bei Login-Versuchen.
    last_attempt_at = Column(DateTime, nullable=True)
    last_status = Column(String(50), nullable=True)  # 'ok' | 'fail' | None
    last_error = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class TenderAttachment(Base):
    """Vom Enricher gefundene + lokal gespeicherte Vergabeunterlage."""
    __tablename__ = "tender_attachments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tender_id = Column(Integer, ForeignKey("tenders.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    source_url = Column(String(1000), nullable=False)
    local_path = Column(String(500), nullable=True)   # relative zu settings.attachment_dir
    content_type = Column(String(120), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    extracted_text = Column(Text, nullable=True)       # erste ~2000 Zeichen, optional
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    tender = relationship("Tender", backref="attachments")


class UserTenderStatus(Base):
    """Per-User-Override des Tender-Status (privat).
    Wenn fuer (user_id, tender_id) ein Eintrag existiert, hat er Vorrang
    vor Tender.status. Admins koennen alle Eintraege sehen."""
    __tablename__ = "user_tender_status"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    tender_id = Column(Integer, ForeignKey("tenders.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    status = Column(String(30), nullable=False)
    note = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow,
                        onupdate=datetime.utcnow, nullable=False)
    __table_args__ = (
        Index("ix_uts_unique", "user_id", "tender_id", unique=True),
    )


class Feedback(Base):
    """User-Feedback (Bug, Wunsch, neues Suchprofil, ...)."""
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                     nullable=True, index=True)
    username = Column(String(80), nullable=False)
    kind = Column(String(40), nullable=False, default="general")
    title = Column(String(200), nullable=True)
    body = Column(Text, nullable=False)
    suggested_keywords = Column(Text, nullable=True)  # JSON-Liste, fuer Profil-Vorschlaege
    status = Column(String(30), default="neu", nullable=False)
    admin_reply = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class PendingRegistration(Base):
    """Selbst-Registrierungs-Anfrage (Microsoft-Login ODER manuelle
    E-Mail-Registrierung) - wartet auf Admin-Freigabe."""
    __tablename__ = "pending_registrations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), nullable=False, unique=True)
    full_name = Column(String(200), nullable=True)
    provider = Column(String(40), nullable=False, default="microsoft")
    provider_subject = Column(String(255), nullable=True)  # 'sub' Claim aus OIDC
    # Manuelle Registrierung: Firmendaten + Passwort-Hash
    company = Column(String(200), nullable=True)
    address = Column(String(500), nullable=True)
    phone = Column(String(80), nullable=True)
    message = Column(Text, nullable=True)
    password_hash = Column(String(255), nullable=True)
    status = Column(String(20), default="pending", nullable=False)  # pending|approved|rejected
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    decided_at = Column(DateTime, nullable=True)
    decided_by = Column(String(80), nullable=True)
