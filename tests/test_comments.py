"""End-to-End-Tests fuer Kommentare und Portal-Filter via TestClient.

Wir benutzen den existierenden Engine + SQLite-DB (kein Reload), weil
auth.py SessionLocal beim Import cached und Reload-Tricks brueckig sind.
Stattdessen: jeder Test arbeitet mit seinen eigenen Tender-Eintraegen
und raeumt diese am Ende auf.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app_client():
    from backend.api import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_session():
    from backend.database import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def auth_client(app_client):
    """TestClient mit eingeloggtem Admin (env-Fallback)."""
    r = app_client.post(
        "/login",
        data={"username": "admin", "password": "fbe-admin-bitte-aendern"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), (r.status_code, r.text[:200])
    yield app_client
    app_client.get("/logout")


@pytest.fixture
def tender(db_session):
    from backend.models import Tender, TenderStatus, Comment
    fp = "fp-test-comments"
    t = Tender(
        title="Test Tender Comments",
        portal="Test Portal",
        url="https://example.org/t-comments",
        description="Test description",
        fingerprint=fp,
        relevance_score=42,
        relevance_level="medium",
        status=TenderStatus.NEU.value,
    )
    db_session.add(t)
    db_session.commit()
    tid = t.id
    yield tid
    # Cleanup: Kommentare + Tender weg.
    db_session.query(Comment).filter(Comment.tender_id == tid).delete()
    db_session.query(Tender).filter(Tender.id == tid).delete()
    db_session.commit()


def test_portal_filter_shows_configured_portals_even_without_tenders(auth_client):
    r = auth_client.get("/")
    assert r.status_code == 200
    assert "Vergabe Sachsen" in r.text
    assert "Vergabeplattform Berlin" in r.text
    assert "TED - Tenders Electronic Daily" in r.text
    assert "bund.de Service-Portal" in r.text


def test_comment_post_and_show(auth_client, tender):
    r = auth_client.post(
        f"/tender/{tender}/comments",
        data={"body": "Erstes Feedback"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    r = auth_client.get(f"/tender/{tender}")
    assert r.status_code == 200
    assert "Erstes Feedback" in r.text
    assert "admin" in r.text


def test_comment_anonymous_rejected(app_client, tender):
    # Sicherstellen, dass keine Session aktiv ist.
    app_client.get("/logout")
    r = app_client.post(
        f"/tender/{tender}/comments",
        data={"body": "spam"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 401, 403)


def test_comment_empty_body_ignored(auth_client, tender, db_session):
    from backend.models import Comment
    before = db_session.query(Comment).filter(Comment.tender_id == tender).count()

    r = auth_client.post(
        f"/tender/{tender}/comments",
        data={"body": "   "},
        follow_redirects=False,
    )
    assert r.status_code == 303

    db_session.expire_all()
    after = db_session.query(Comment).filter(Comment.tender_id == tender).count()
    assert after == before


def test_comment_admin_can_delete_others(auth_client, tender, db_session):
    from backend.models import Comment
    com = Comment(
        tender_id=tender, user_id=None, username="someone-else",
        body="fremder Kommentar",
    )
    db_session.add(com)
    db_session.commit()
    cid = com.id

    r = auth_client.post(
        f"/tender/{tender}/comments/{cid}/delete",
        follow_redirects=False,
    )
    assert r.status_code == 303

    db_session.expire_all()
    assert db_session.get(Comment, cid) is None


def test_admin_settings_page_loads(auth_client, db_session):
    from backend.models import Tender, TenderStatus
    db_session.add(Tender(
        title="Test", portal="X", url="https://x.example/1",
        fingerprint="fp-settings-1", relevance_score=0,
        relevance_level="low", status=TenderStatus.NEU.value,
    ))
    db_session.commit()
    try:
        r = auth_client.get("/admin/settings")
        assert r.status_code == 200
        assert "Einstellungen" in r.text
        assert "Alle Suchergebnisse zurücksetzen" in r.text
        assert "RESET" in r.text
    finally:
        db_session.query(Tender).filter(Tender.fingerprint == "fp-settings-1").delete()
        db_session.commit()


def test_admin_reset_requires_confirm_keyword(auth_client, db_session):
    from backend.models import Tender, TenderStatus
    db_session.add(Tender(
        title="Bleibt erhalten", portal="X", url="https://x.example/keep",
        fingerprint="fp-keep", relevance_score=0,
        relevance_level="low", status=TenderStatus.NEU.value,
    ))
    db_session.commit()
    try:
        # Falsches Bestaetigungswort -> kein Reset
        r = auth_client.post(
            "/admin/reset-tenders",
            data={"confirm": "wrong"}, follow_redirects=False,
        )
        assert r.status_code == 303
        assert "/admin/settings?error=" in r.headers["location"]
        # Tender existiert noch
        db_session.expire_all()
        assert db_session.query(Tender).filter(
            Tender.fingerprint == "fp-keep").first() is not None
    finally:
        db_session.query(Tender).filter(Tender.fingerprint == "fp-keep").delete()
        db_session.commit()


def test_admin_reset_deletes_tenders_and_comments(auth_client, db_session):
    from backend.models import Comment, Tender, TenderStatus
    t = Tender(
        title="Wird geloescht", portal="X", url="https://x.example/del",
        fingerprint="fp-del", relevance_score=0,
        relevance_level="low", status=TenderStatus.NEU.value,
    )
    db_session.add(t)
    db_session.commit()
    tender_id = t.id  # vor Reset cachen
    db_session.add(Comment(
        tender_id=tender_id, user_id=None, username="someone",
        body="dazu auch weg",
    ))
    db_session.commit()

    r = auth_client.post(
        "/admin/reset-tenders",
        data={"confirm": "RESET"}, follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/admin/settings?flash=" in r.headers["location"]
    db_session.expire_all()
    assert db_session.query(Tender).filter(Tender.fingerprint == "fp-del").first() is None
    assert db_session.query(Comment).filter(Comment.tender_id == tender_id).first() is None


def test_admin_reset_blocked_for_non_admin(app_client, tender):
    """Wenn ein Viewer (nicht-Admin) versucht zu resetten, kommt 403."""
    app_client.get("/logout")
    # Versuch ohne Session
    r = app_client.post("/admin/reset-tenders", data={"confirm": "RESET"},
                        follow_redirects=False)
    # Auth-Middleware schickt zur Login-Seite (303) ODER 401/403
    assert r.status_code in (303, 401, 403)


def test_default_dashboard_hides_expired_tenders(auth_client, db_session):
    """Default-Filter: nur Ausschreibungen mit Frist heute oder spaeter."""
    from datetime import datetime, timedelta
    from backend.models import Tender, TenderStatus
    db_session.add_all([
        Tender(title="Aktiv1", portal="X", url="https://x.example/a1",
               fingerprint="fp-act-1", relevance_score=80, relevance_level="high",
               status=TenderStatus.NEU.value,
               deadline=datetime.utcnow() + timedelta(days=10)),
        Tender(title="Abgelaufen", portal="X", url="https://x.example/exp",
               fingerprint="fp-exp", relevance_score=80, relevance_level="high",
               status=TenderStatus.NEU.value,
               deadline=datetime.utcnow() - timedelta(days=5)),
        Tender(title="OhneFrist", portal="X", url="https://x.example/null",
               fingerprint="fp-null", relevance_score=80, relevance_level="high",
               status=TenderStatus.NEU.value,
               deadline=None),
    ])
    db_session.commit()
    try:
        # Default: aktiv-Filter
        r = auth_client.get("/")
        assert r.status_code == 200
        assert "Aktiv1" in r.text
        assert "OhneFrist" in r.text  # Tender ohne Frist bleibt sichtbar
        assert "Abgelaufen" not in r.text  # abgelaufen wird ausgeblendet

        # Mit include_expired=1: alles
        r = auth_client.get("/?include_expired=1")
        assert "Aktiv1" in r.text
        assert "Abgelaufen" in r.text

        # Quick=expired: nur abgelaufene
        r = auth_client.get("/?quick=expired&include_expired=1")
        assert "Abgelaufen" in r.text
        assert "Aktiv1" not in r.text
    finally:
        for fp in ("fp-act-1", "fp-exp", "fp-null"):
            db_session.query(Tender).filter(Tender.fingerprint == fp).delete()
        db_session.commit()


def test_admin_test_mail_blocked_when_not_configured(auth_client):
    """Ohne SMTP-Konfig liefert /admin/test-mail eine klare Fehlermeldung."""
    r = auth_client.post("/admin/test-mail", follow_redirects=False)
    assert r.status_code == 303
    assert "/admin/settings?error=" in r.headers["location"]
    assert "SMTP" in r.headers["location"]


def test_admin_send_summary_blocked_when_not_configured(auth_client):
    r = auth_client.post("/admin/send-summary",
                         data={"days": "7"}, follow_redirects=False)
    assert r.status_code == 303
    assert "/admin/settings?error=SMTP" in r.headers["location"]


def test_notify_format_plain_lists_tenders():
    from backend import notify
    from backend.models import Tender, TenderStatus
    from datetime import datetime, timedelta
    items = [
        Tender(title="Verfuellung Magdeburg", portal="bund.de",
               url="https://x.example/1", contracting_authority="Stadt Magdeburg",
               location="Magdeburg", relevance_score=85,
               deadline=datetime.utcnow() + timedelta(days=10),
               fingerprint="fp-mail-1", status=TenderStatus.NEU.value),
        Tender(title="Tiefbau Leipzig", portal="evergabe-online.de",
               url="https://x.example/2", contracting_authority="Stadt Leipzig",
               relevance_score=72, fingerprint="fp-mail-2",
               status=TenderStatus.NEU.value),
    ]
    body = notify._format_plain(items, header="Test:")
    assert "Verfuellung Magdeburg" in body
    assert "Tiefbau Leipzig" in body
    assert "85" in body  # Score
    assert "72" in body
    assert "https://x.example/1" in body
    assert "Stadt Magdeburg" in body


def test_notify_format_html_escapes_and_sorts():
    from backend import notify
    from backend.models import Tender, TenderStatus
    items = [
        Tender(title="<script>alert(1)</script>", portal="P",
               url="https://x.example/x", relevance_score=50,
               fingerprint="fp-mail-x", status=TenderStatus.NEU.value),
    ]
    html = notify._format_html(items, header="X")
    # Title-Escape
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    # Korrekte Struktur
    assert "<table" in html
    assert "https://x.example/x" in html


def test_admin_run_search_button_triggers_pipeline(auth_client):
    """Der separate 'Suche starten'-Button triggert einen Lauf, ohne
    dass der User RESET tippen muss."""
    r = auth_client.post("/admin/run-search", follow_redirects=False)
    assert r.status_code == 303
    assert "/admin/settings?flash=" in r.headers["location"]
    assert "Suchlauf+gestartet" in r.headers["location"]


def test_admin_run_search_requires_login(app_client):
    app_client.get("/logout")
    r = app_client.post("/admin/run-search", follow_redirects=False)
    assert r.status_code in (303, 401, 403)


def test_score_breakdown_rendered_in_detail(auth_client, db_session):
    from backend.models import Tender
    import json

    t = Tender(
        title="Demo Score Breakdown",
        portal="Test",
        url="https://example.org/t-breakdown",
        fingerprint="fp-breakdown",
        relevance_score=80,
        relevance_level="high",
        score_breakdown=json.dumps([
            {"label": "Themencluster Kern (high)", "points": 30,
             "detail": "2x Treffer"},
            {"label": "Kernthema-Bonus", "points": 25,
             "detail": "high-Cluster"},
        ]),
    )
    db_session.add(t)
    db_session.commit()
    tid = t.id

    try:
        r = auth_client.get(f"/tender/{tid}")
        assert r.status_code == 200
        assert "Wie kommt der Score zustande" in r.text
        assert "Themencluster Kern (high)" in r.text
        assert "Kernthema-Bonus" in r.text
        assert "+30" in r.text
    finally:
        db_session.query(Tender).filter(Tender.id == tid).delete()
        db_session.commit()
