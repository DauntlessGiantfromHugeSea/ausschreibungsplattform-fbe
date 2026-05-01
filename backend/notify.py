"""E-Mail-Versand: Sofort-Benachrichtigung + Tagesszusammenfassung."""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Iterable, List

from sqlalchemy import or_

from .config import settings
from .database import SessionLocal
from .models import Tender


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
def is_configured() -> bool:
    """True wenn SMTP + Empfaenger fuer Mailversand gesetzt sind."""
    return bool(settings.notify_email and settings.smtp_host)


def _send(subject: str, plain_body: str, html_body: str | None = None) -> bool:
    """Niedrigschwelliger SMTP-Versand. Liefert True bei Erfolg."""
    if not is_configured():
        log.info("Mailversand uebersprungen - nicht konfiguriert.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = settings.notify_email
    msg.set_content(plain_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    except Exception as exc:
        log.warning("Mailversand fehlgeschlagen: %s", exc)
        return False
    log.info("Mail an %s versendet: %s", settings.notify_email, subject)
    return True


# ---------------------------------------------------------------------------
def send_high_relevance_email(tenders: Iterable[Tender]) -> bool:
    """Sofort-Benachrichtigung beim Lauf-Ende, wenn neue HIGH-Tender da sind."""
    items = list(tenders)
    if not items:
        return False

    subj = "[FBE Ausschreibungen] {} neue hochrelevante Treffer".format(len(items))
    plain = _format_plain(items, header="Neue hochrelevante Ausschreibungen:")
    html = _format_html(items, header="Neue hochrelevante Ausschreibungen")
    return _send(subj, plain, html)


# ---------------------------------------------------------------------------
def send_daily_summary(days: int = 1, min_score: int | None = None) -> bool:
    """Schickt eine Zusammenfassung der laufenden Ausschreibungen die in den
    letzten <days> Tagen erfasst wurden, sortiert nach Score absteigend."""
    if not is_configured():
        log.info("Daily-Summary uebersprungen - SMTP nicht konfiguriert.")
        return False

    threshold = (min_score if min_score is not None
                 else max(40, settings.high_relevance_threshold - 20))
    since = datetime.utcnow() - timedelta(days=days)
    now = datetime.utcnow()

    db = SessionLocal()
    try:
        items = (
            db.query(Tender)
            .filter(Tender.created_at >= since)
            .filter(Tender.relevance_score >= threshold)
            .filter(or_(Tender.deadline.is_(None), Tender.deadline >= now))
            .order_by(Tender.relevance_score.desc(), Tender.created_at.desc())
            .limit(100)
            .all()
        )
    finally:
        db.close()

    if not items:
        log.info("Daily-Summary: 0 Treffer mit score>=%d in den letzten %dd",
                 threshold, days)
        return False

    period = "heute" if days <= 1 else "letzten {} Tagen".format(days)
    subj = "[FBE Ausschreibungen] Tagesübersicht: {} neue Treffer ({})".format(
        len(items), period)
    plain = _format_plain(items, header="Neue Treffer aus {}:".format(period))
    html = _format_html(items, header="Neue Treffer aus {}".format(period))
    return _send(subj, plain, html)


# ---------------------------------------------------------------------------
def send_test_mail() -> bool:
    """Sendet eine 1-zeilige Test-Mail, um die SMTP-Konfig zu pruefen."""
    if not is_configured():
        return False
    plain = (
        "Das ist eine Test-Mail von der FBE-Ausschreibungsplattform.\n\n"
        "Wenn du das liest, ist der SMTP-Versand funktional. Tagesszusammen-\n"
        "fassungen werden im Continuous-Mode automatisch versendet, sofern\n"
        "im Scheduler aktiviert.\n\n"
        "Zeitstempel: {}\n".format(datetime.now().isoformat(timespec="seconds"))
    )
    return _send("[FBE] Test-Mail · SMTP funktioniert", plain)


# ---------------------------------------------------------------------------
def _format_plain(items: List[Tender], header: str) -> str:
    lines = [header, ""]
    for t in items:
        lines.append("- [{score:.0f}] {title}".format(
            score=t.relevance_score or 0, title=t.title or "(ohne Titel)"))
        meta = []
        if t.portal:
            meta.append(t.portal)
        if t.contracting_authority:
            meta.append(t.contracting_authority)
        if t.location:
            meta.append(t.location)
        if t.deadline:
            meta.append("Frist: {}".format(t.deadline.strftime("%d.%m.%Y")))
        if meta:
            lines.append("  " + " · ".join(meta))
        if t.url:
            lines.append("  {}".format(t.url))
        lines.append("")
    lines.append("---")
    lines.append("Dashboard: siehe FBE-Server (Tip: Filter 'Aktiv' + Score-Filter setzen).")
    return "\n".join(lines)


def _format_html(items: List[Tender], header: str) -> str:
    rows = []
    for t in items:
        score = t.relevance_score or 0
        color = "#16a34a" if score >= 70 else "#d97706" if score >= 50 else "#71717a"
        meta = []
        if t.contracting_authority:
            meta.append(t.contracting_authority)
        if t.location:
            meta.append(t.location)
        if t.deadline:
            meta.append("Frist {}".format(t.deadline.strftime("%d.%m.%Y")))
        meta_str = " · ".join(meta) if meta else ""
        rows.append("""
          <tr>
            <td style="padding:8px;border-bottom:1px solid #eee;vertical-align:top;">
              <span style="display:inline-block;min-width:32px;padding:2px 6px;background:{color}22;color:{color};border-radius:6px;font-weight:600;text-align:center;">{score:.0f}</span>
            </td>
            <td style="padding:8px;border-bottom:1px solid #eee;">
              <div style="font-weight:600;">
                <a href="{url}" style="color:#1f3556;text-decoration:none;">{title}</a>
              </div>
              <div style="color:#71717a;font-size:13px;margin-top:2px;">
                {portal}{meta_sep}{meta}
              </div>
            </td>
          </tr>
        """.format(
            color=color,
            score=score,
            url=(t.url or "#"),
            title=(t.title or "(ohne Titel)").replace("<", "&lt;").replace(">", "&gt;"),
            portal=(t.portal or "").replace("<", "&lt;").replace(">", "&gt;"),
            meta_sep=" · " if meta_str else "",
            meta=meta_str.replace("<", "&lt;").replace(">", "&gt;"),
        ))

    return """
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#27272a;background:#f4f4f5;margin:0;padding:24px;">
      <table style="max-width:720px;margin:0 auto;background:white;border-collapse:collapse;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
        <tr><td style="padding:20px 24px;background:#1f3556;color:white;font-size:18px;font-weight:600;">
          {header}
        </td></tr>
        <tr><td style="padding:0;">
          <table style="width:100%;border-collapse:collapse;">{rows}</table>
        </td></tr>
        <tr><td style="padding:16px 24px;background:#fafafa;color:#71717a;font-size:12px;text-align:center;">
          FBE Ausschreibungsplattform · {count} Treffer
        </td></tr>
      </table>
    </body></html>
    """.format(header=header, rows="".join(rows), count=len(items))
