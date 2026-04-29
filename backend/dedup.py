"""Dublettenpruefung ueber Fingerprint aus URL, Titel, Auftraggeber, Frist."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Optional


def _norm(value: str | None) -> str:
    if not value:
        return ""
    s = value.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def fingerprint(
    url: str | None,
    title: str | None,
    authority: str | None,
    deadline: Optional[datetime],
) -> str:
    """Stabiler SHA-256-Fingerprint."""
    parts = [
        _norm(url),
        _norm(title),
        _norm(authority),
        deadline.date().isoformat() if deadline else "",
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_duplicate(session, fp: str) -> bool:
    from .models import Tender

    return session.query(Tender.id).filter(Tender.fingerprint == fp).first() is not None
