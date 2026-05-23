"""Restricted-User (Rolle 'user'): Profil-Enforcement und Bypass-Sicherheit."""
import json
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def app_client():
    from backend.api import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def setup_restricted(db_session):
    """Legt einen Restricted-User + 1 Profil + 2 Tender an, liefert die IDs."""
    from backend.models import User, SearchProfile, Tender
    from backend.auth import hash_password

    u = User(username="restricted_bob",
             password_hash=hash_password("bob-pw-123"),
             role="user", email="bob@x.de", is_active=True)
    db_session.add(u); db_session.commit()

    p = SearchProfile(
        name="Fernwaerme-Test",
        description="Nur Waermenetz",
        keywords=json.dumps(["Fernwärme", "Nahwärme"]),
    )
    db_session.add(p); db_session.commit()
    p.assigned_users = [u]; db_session.commit()

    t_match = Tender(title="Fernwärme Köln", portal="X", url="u1-r",
                     fingerprint="fp-r1", relevance_score=80)
    t_other = Tender(title="Photovoltaik Anlage", portal="X", url="u2-r",
                     fingerprint="fp-r2", relevance_score=80)
    db_session.add_all([t_match, t_other]); db_session.commit()

    yield {
        "user_id": u.id, "profile_id": p.id,
        "match_id": t_match.id, "other_id": t_other.id,
    }

    db_session.query(Tender).filter(Tender.fingerprint.in_(["fp-r1", "fp-r2"])).delete()
    db_session.query(User).filter(User.id == u.id).delete()
    db_session.query(SearchProfile).filter(SearchProfile.id == p.id).delete()
    db_session.commit()


@pytest.fixture
def restricted_client(app_client, setup_restricted):
    """TestClient mit Session als Restricted-User via /login."""
    app_client.cookies.clear()
    r = app_client.post(
        "/login",
        data={"username": "restricted_bob", "password": "bob-pw-123"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303), (r.status_code, r.text[:200])
    yield app_client
    app_client.get("/logout")


def _db_session():
    from backend.database import SessionLocal
    return SessionLocal()


@pytest.fixture
def db_session():
    """Local fixture, weil test_comments.py das auch lokal definiert."""
    s = _db_session()
    try:
        yield s
    finally:
        s.close()


def test_profile_keywords_match(setup_restricted):
    from backend.models import SearchProfile, Tender
    from backend.api import _profile_filter_expr

    db = _db_session()
    try:
        p = db.get(SearchProfile, setup_restricted["profile_id"])
        matches = db.query(Tender).filter(_profile_filter_expr(p)).all()
        ids = [t.id for t in matches]
        assert setup_restricted["match_id"] in ids
        assert setup_restricted["other_id"] not in ids
    finally:
        db.close()


def test_dashboard_blocks_url_query_bypass(restricted_client):
    """Restricted-User darf nicht via ?q=... fremde Tender sehen."""
    r = restricted_client.get("/?q=Photovoltaik")
    assert r.status_code == 200
    assert "Photovoltaik Anlage" not in r.text
    assert "Fernwärme Köln" in r.text
    # Kein Suchfeld im HTML
    assert 'name="q"' not in r.text


def test_api_tenders_blocks_url_query_bypass(restricted_client, setup_restricted):
    r = restricted_client.get("/api/tenders?q=Photovoltaik")
    assert r.status_code == 200
    titles = [t.get("title") for t in r.json()]
    assert "Photovoltaik Anlage" not in titles
    assert "Fernwärme Köln" in titles


def test_detail_blocks_unassigned_tender(restricted_client, setup_restricted):
    """Direktaufruf eines Tenders ausserhalb der zugewiesenen Profile -> 404."""
    r = restricted_client.get(f"/tender/{setup_restricted['other_id']}")
    assert r.status_code == 404
    r2 = restricted_client.get(f"/tender/{setup_restricted['match_id']}")
    assert r2.status_code == 200


def test_profile_admin_routes_blocked_for_restricted(restricted_client):
    """Restricted-User darf /admin/profiles nicht oeffnen."""
    r = restricted_client.get("/admin/profiles", follow_redirects=False)
    assert r.status_code == 303  # Middleware redirects with error flash
