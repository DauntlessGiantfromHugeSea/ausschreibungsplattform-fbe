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
    """Niedrigschwelliger SMTP-Versand an NOTIFY_EMAIL. Liefert True bei Erfolg."""
    ok, _err = _send_to(
        to=[settings.notify_email] if settings.notify_email else [],
        subject=subject, plain_body=plain_body, html_body=html_body,
    )
    return ok


def _send_to(
    to: list[str],
    subject: str,
    plain_body: str,
    html_body: str | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: str | None = None,
) -> tuple[bool, str | None]:
    """SMTP-Versand mit expliziten Empfaengern. Liefert (ok, error_message).

    error_message ist None bei Erfolg oder enthaelt eine kurze, fuer den
    User lesbare Fehlerbeschreibung (z.B. 'SMTP-Auth fehlgeschlagen' statt
    nur 'failed').
    """
    if not (settings.smtp_host and settings.smtp_from):
        log.info("Mailversand uebersprungen - SMTP_HOST/SMTP_FROM fehlt.")
        return False, "SMTP_HOST oder SMTP_FROM nicht konfiguriert"
    to = [a for a in (to or []) if a]
    cc = [a for a in (cc or []) if a]
    bcc = [a for a in (bcc or []) if a]
    if not (to or cc or bcc):
        return False, "Kein Empfaenger angegeben"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(plain_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    rcpt = list(to) + list(cc) + list(bcc)
    port = int(settings.smtp_port or 587)
    # Connect-Timeout 10s (schnelles Feedback bei Provider-Sperre wie
    # Hetzner-Port-25-Block), Send-Operation insgesamt bis 30s.
    connect_timeout = 10

    try:
        # Port 465 = implizites SSL, sonst STARTTLS auf 587/25.
        if port == 465:
            smtp = smtplib.SMTP_SSL(settings.smtp_host, port, timeout=connect_timeout)
        else:
            smtp = smtplib.SMTP(settings.smtp_host, port, timeout=connect_timeout)
        # Nach erfolgreichem Connect den Timeout aufs Send-Limit anheben.
        try:
            smtp.sock.settimeout(30)
        except (AttributeError, OSError):
            pass
        try:
            smtp.ehlo()
            if port != 465:
                # STARTTLS nur wenn der Server es anbietet, sonst skippen
                # (manche interne Relays laufen unverschluesselt).
                try:
                    if smtp.has_extn("STARTTLS"):
                        smtp.starttls()
                        smtp.ehlo()
                except smtplib.SMTPException as exc:
                    log.info("STARTTLS nicht moeglich (%s) - fahre ohne TLS fort.", exc)
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg, to_addrs=rcpt)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError as exc:
        msg_short = "SMTP-Authentifizierung fehlgeschlagen ({}): User/Passwort pruefen".format(
            exc.smtp_code if hasattr(exc, "smtp_code") else "?")
        log.warning("Mailversand fehlgeschlagen: %s", exc)
        return False, msg_short
    except smtplib.SMTPRecipientsRefused as exc:
        msg_short = "Empfaenger abgelehnt: {}".format(list(exc.recipients.keys())[:3])
        log.warning("Mailversand fehlgeschlagen: %s", exc)
        return False, msg_short
    except smtplib.SMTPSenderRefused as exc:
        msg_short = "Absender abgelehnt: {} (SMTP_FROM in .env pruefen)".format(exc.sender)
        log.warning("Mailversand fehlgeschlagen: %s", exc)
        return False, msg_short
    except smtplib.SMTPConnectError as exc:
        msg_short = "Verbindung zum SMTP-Server fehlgeschlagen: {}".format(exc)
        log.warning(msg_short)
        return False, msg_short
    except (TimeoutError, OSError) as exc:
        msg_short = "Netzwerk/Timeout zum SMTP-Server: {}".format(str(exc)[:120])
        log.warning(msg_short)
        return False, msg_short
    except smtplib.SMTPException as exc:
        msg_short = "SMTP-Fehler: {}".format(str(exc)[:160])
        log.warning(msg_short)
        return False, msg_short
    except Exception as exc:
        msg_short = "{}: {}".format(type(exc).__name__, str(exc)[:160])
        log.warning("Mailversand fehlgeschlagen: %s", msg_short)
        return False, msg_short

    log.info("Mail an %s versendet: %s",
             ", ".join(rcpt[:3]) + ("..." if len(rcpt) > 3 else ""),
             subject)
    return True, None


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
def send_test_mail() -> tuple[bool, str | None]:
    """Sendet eine 1-zeilige Test-Mail. Liefert (ok, error_message)."""
    if not is_configured():
        return False, "SMTP nicht konfiguriert (NOTIFY_EMAIL / SMTP_HOST leer)"
    plain = (
        "Das ist eine Test-Mail von der FBE-Ausschreibungsplattform.\n\n"
        "Wenn du das liest, ist der SMTP-Versand funktional. Tagesszusammen-\n"
        "fassungen werden im Continuous-Mode automatisch versendet, sofern\n"
        "im Scheduler aktiviert.\n\n"
        "Zeitstempel: {}\n".format(datetime.now().isoformat(timespec="seconds"))
    )
    ok, err = _send_to(
        to=[settings.notify_email] if settings.notify_email else [],
        subject="[FBE] Test-Mail · SMTP funktioniert",
        plain_body=plain,
    )
    return ok, err


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


# ---------------------------------------------------------------------------
def send_tender_mail(
    tender: Tender,
    to: list[str],
    cc: list[str] | None = None,
    subject: str | None = None,
    custom_message: str = "",
    sender_name: str | None = None,
    sender_signature: str | None = None,
) -> bool:
    """Versendet eine Mail mit den Daten einer Ausschreibung.

    Layout: FBE-Branding-Header, Custom-Nachricht des Users (Plain mit
    line breaks), strukturierte Tender-Card, Original-Link, Firmen-Footer.

    Args:
        tender: ORM-Objekt
        to: Empfaengerliste (mind. einer noetig)
        cc: optional CC
        subject: optional Subject (default: portal-praefix + tender title)
        custom_message: persoenlicher Text vom User
        sender_name: Anzeigename oben (default: company_name aus settings)
        sender_signature: optionale Signatur unten (Plain-Text)
    """
    if not to:
        log.info("send_tender_mail: kein Empfaenger angegeben.")
        return False, "Kein Empfaenger"
    if subject is None:
        subject = "Ausschreibung: {}".format(tender.title or "(ohne Titel)")
    plain = _format_tender_plain(
        tender, custom_message=custom_message,
        sender_signature=sender_signature,
    )
    html = _format_tender_html(
        tender, custom_message=custom_message,
        sender_name=sender_name or settings.company_name,
        sender_signature=sender_signature,
    )
    return _send_to(
        to=to, cc=cc, subject=subject,
        plain_body=plain, html_body=html,
        reply_to=settings.company_email or None,
    )


def _format_tender_plain(
    tender: Tender, custom_message: str = "", sender_signature: str | None = None,
) -> str:
    lines = []
    if custom_message and custom_message.strip():
        lines.append(custom_message.strip())
        lines.append("")
        lines.append("---")
        lines.append("")
    lines.append("Ausschreibung")
    lines.append("=" * 50)
    lines.append("")
    lines.append("Titel:    {}".format(tender.title or "(ohne Titel)"))
    if tender.contracting_authority:
        lines.append("Auftraggeber: {}".format(tender.contracting_authority))
    if tender.location:
        lines.append("Ort:      {}".format(tender.location))
    if tender.region:
        lines.append("Bundesland: {}".format(tender.region))
    if tender.deadline:
        lines.append("Frist:    {}".format(tender.deadline.strftime("%d.%m.%Y")))
    if tender.publication_date:
        lines.append("Veröffentlicht: {}".format(
            tender.publication_date.strftime("%d.%m.%Y")))
    if tender.portal:
        lines.append("Quelle:   {}".format(tender.portal))
    if tender.cpv_codes:
        lines.append("CPV:      {}".format(tender.cpv_codes))
    if tender.relevance_score is not None:
        lines.append("Relevanz: {:.0f} / 100".format(tender.relevance_score))
    lines.append("")
    if tender.description:
        lines.append("Beschreibung:")
        lines.append((tender.description or "")[:1000])
        lines.append("")
    if tender.url:
        lines.append("Original: {}".format(tender.url))
    lines.append("")
    if sender_signature and sender_signature.strip():
        lines.append("---")
        lines.append(sender_signature.strip())
        lines.append("")
    # Footer
    foot_parts = []
    if settings.company_name:
        foot_parts.append(settings.company_name)
    if settings.company_address:
        foot_parts.append(settings.company_address)
    if settings.company_phone:
        foot_parts.append("Tel: {}".format(settings.company_phone))
    if settings.company_email:
        foot_parts.append(settings.company_email)
    if settings.company_web:
        foot_parts.append(settings.company_web)
    if foot_parts:
        lines.append("---")
        lines.append(" · ".join(foot_parts))
    return "\n".join(lines)


def _esc(s) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;"))


def _format_tender_html(
    tender: Tender, custom_message: str = "",
    sender_name: str | None = None,
    sender_signature: str | None = None,
) -> str:
    score = tender.relevance_score or 0
    score_color = "#16a34a" if score >= 70 else "#d97706" if score >= 50 else "#71717a"

    rows_html = []
    def add_row(label, value):
        if not value:
            return
        rows_html.append(
            "<tr>"
            "<td style='padding:6px 12px;color:#71717a;font-size:13px;width:130px;vertical-align:top;'>{label}</td>"
            "<td style='padding:6px 12px;color:#27272a;font-size:14px;'>{value}</td>"
            "</tr>".format(label=_esc(label), value=_esc(value))
        )

    add_row("Auftraggeber", tender.contracting_authority)
    add_row("Ort", tender.location)
    add_row("Bundesland", tender.region)
    if tender.deadline:
        add_row("Frist", tender.deadline.strftime("%d.%m.%Y"))
    if tender.publication_date:
        add_row("Veröffentlicht", tender.publication_date.strftime("%d.%m.%Y"))
    add_row("Quelle", tender.portal)
    if tender.cpv_codes:
        add_row("CPV-Codes", tender.cpv_codes)

    # Custom message: Newline -> <br>
    custom_html = ""
    if custom_message and custom_message.strip():
        body_text = _esc(custom_message.strip()).replace("\n", "<br>")
        custom_html = (
            "<div style='padding:20px 24px;background:#f8f7f3;border-left:4px solid #5a8d3a;"
            "color:#1f2937;font-size:14px;line-height:1.55;'>{}</div>"
        ).format(body_text)

    description_html = ""
    if tender.description:
        desc = _esc((tender.description or "")[:1500]).replace("\n", "<br>")
        description_html = (
            "<div style='padding:14px 16px;background:#fafafa;border:1px solid #e4e4e7;"
            "border-radius:6px;color:#3f3f46;font-size:13px;line-height:1.5;margin:10px 16px;'>{}</div>"
        ).format(desc)

    signature_html = ""
    if sender_signature and sender_signature.strip():
        sig = _esc(sender_signature.strip()).replace("\n", "<br>")
        signature_html = (
            "<div style='padding:16px 24px;color:#3f3f46;font-size:14px;"
            "border-top:1px solid #e4e4e7;'>{}</div>"
        ).format(sig)

    # Footer mit Firmendaten
    foot_lines = []
    if settings.company_name:
        foot_lines.append("<strong>{}</strong>".format(_esc(settings.company_name)))
    if settings.company_address:
        foot_lines.append(_esc(settings.company_address))
    contact_bits = []
    if settings.company_phone:
        contact_bits.append("Tel: {}".format(_esc(settings.company_phone)))
    if settings.company_email:
        contact_bits.append('<a href="mailto:{0}" style="color:#5a8d3a;text-decoration:none;">{0}</a>'.format(
            _esc(settings.company_email)))
    if settings.company_web:
        contact_bits.append('<a href="{0}" style="color:#5a8d3a;text-decoration:none;">{0}</a>'.format(
            _esc(settings.company_web)))
    if contact_bits:
        foot_lines.append(" · ".join(contact_bits))
    footer_html = "<br>".join(foot_lines)

    logo_html = ""
    if settings.company_logo_url:
        logo_html = (
            '<img src="{}" alt="{}" '
            'style="height:42px;display:block;margin:0 auto;">'
        ).format(_esc(settings.company_logo_url), _esc(settings.company_name or "Logo"))

    return """\
<html><body style="margin:0;padding:24px;background:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#27272a;">
  <table style="max-width:680px;margin:0 auto;background:white;border-collapse:collapse;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08);">
    <tr><td style="padding:20px 24px;background:white;border-bottom:1px solid #e4e4e7;text-align:center;">
      {logo}
    </td></tr>
    {custom_block}
    <tr><td style="padding:20px 24px 8px 24px;">
      <div style="font-size:12px;color:#71717a;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">Ausschreibung</div>
      <h1 style="margin:0 0 12px 0;font-size:20px;font-weight:600;color:#1f2937;line-height:1.35;">{title}</h1>
      <div style="display:inline-block;padding:3px 10px;background:{score_color}22;color:{score_color};border-radius:6px;font-size:12px;font-weight:600;">
        Relevanz {score:.0f} / 100
      </div>
    </td></tr>
    <tr><td style="padding:0 12px;">
      <table style="width:100%;border-collapse:collapse;">{rows}</table>
    </td></tr>
    {description_block}
    <tr><td style="padding:14px 24px 24px 24px;">
      <a href="{url}" style="display:inline-block;padding:10px 18px;background:#5a8d3a;color:white;text-decoration:none;border-radius:6px;font-weight:600;font-size:14px;">
        Zur Ausschreibung →
      </a>
    </td></tr>
    {signature_block}
    <tr><td style="padding:18px 24px;background:#fafafa;color:#71717a;font-size:12px;line-height:1.5;border-top:1px solid #e4e4e7;text-align:center;">
      {footer}
    </td></tr>
  </table>
</body></html>""".format(
        logo=logo_html,
        custom_block=("<tr><td>{}</td></tr>".format(custom_html) if custom_html else ""),
        title=_esc(tender.title or "(ohne Titel)"),
        score=score,
        score_color=score_color,
        rows="".join(rows_html),
        description_block=(
            "<tr><td>{}</td></tr>".format(description_html) if description_html else ""),
        url=_esc(tender.url or "#"),
        signature_block=("<tr><td>{}</td></tr>".format(signature_html) if signature_html else ""),
        footer=footer_html or "Versendet via FBE-Ausschreibungsplattform.",
    )


# ============================================================================
# User-Invite + Password-Reset Mails
# ============================================================================

def send_invite_mail(to_email: str, username: str, link: str) -> tuple[bool, str | None]:
    plain = (
        f"Hallo {username},\n\n"
        f"du wurdest zur FBE-Ausschreibungsplattform eingeladen.\n\n"
        f"Setze dein Passwort hier (Link 24 Stunden gültig):\n{link}\n\n"
    )
    html = f"""<html><body style="font-family:Inter,system-ui,sans-serif;line-height:1.55;color:#222;max-width:600px;margin:24px auto;">
<h2 style="color:#92c57a;">Willkommen bei FBE Ausschreibungen</h2>
<p>Hallo <strong>{username}</strong>,</p>
<p>du wurdest zur FBE-Ausschreibungsplattform eingeladen. Bitte setze dein Passwort über den folgenden Button. Der Link ist <strong>24 Stunden</strong> gültig.</p>
<p style="margin:24px 0;"><a href="{link}" style="background:#92c57a;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">Passwort jetzt setzen</a></p>
<p style="font-size:12px;color:#666;">Falls der Button nicht geht: <a href="{link}">{link}</a></p>
</body></html>"""
    return _send_to([to_email], "Einladung zur FBE-Ausschreibungsplattform", plain, html)


def send_password_reset_mail(to_email: str, username: str, link: str) -> tuple[bool, str | None]:
    plain = (
        f"Hallo {username},\n\n"
        f"jemand (vermutlich du) hat ein Passwort-Reset angefordert.\n"
        f"Link 1 Stunde gültig:\n{link}\n\n"
        f"Falls nicht von dir angefordert: ignorieren — Passwort bleibt unverändert.\n"
    )
    html = f"""<html><body style="font-family:Inter,system-ui,sans-serif;line-height:1.55;color:#222;max-width:600px;margin:24px auto;">
<h2 style="color:#92c57a;">Passwort zurücksetzen</h2>
<p>Hallo <strong>{username}</strong>,</p>
<p>jemand hat einen Passwort-Reset angefordert. Falls du das warst, klicke auf den Button. Der Link ist <strong>1 Stunde</strong> gültig.</p>
<p style="margin:24px 0;"><a href="{link}" style="background:#92c57a;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">Neues Passwort setzen</a></p>
<p style="font-size:12px;color:#666;">Falls nicht von dir: einfach ignorieren.<br>Falls Button nicht geht: <a href="{link}">{link}</a></p>
</body></html>"""
    return _send_to([to_email], "Passwort zurücksetzen – FBE Ausschreibungen", plain, html)
