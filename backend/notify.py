"""Optionaler E-Mail-Versand fuer hochrelevante Treffer."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Iterable

from .config import settings
from .models import Tender


log = logging.getLogger(__name__)


def send_high_relevance_email(tenders: Iterable[Tender]) -> None:
    if not settings.notify_email or not settings.smtp_host:
        log.info("Mailversand uebersprungen – nicht konfiguriert.")
        return

    items = list(tenders)
    if not items:
        return

    msg = EmailMessage()
    msg["Subject"] = f"[FBE Ausschreibungen] {len(items)} neue hochrelevante Treffer"
    msg["From"] = settings.smtp_from
    msg["To"] = settings.notify_email

    body_lines = ["Neue hochrelevante Ausschreibungen:\n"]
    for t in items:
        body_lines.append(f"• [{t.relevance_score:.0f}] {t.title}")
        body_lines.append(f"   {t.portal} – {t.contracting_authority or 'n/a'}")
        body_lines.append(f"   {t.url}\n")
    msg.set_content("\n".join(body_lines))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    log.info("Benachrichtigungs-Mail an %s versendet (%d Treffer).", settings.notify_email, len(items))
