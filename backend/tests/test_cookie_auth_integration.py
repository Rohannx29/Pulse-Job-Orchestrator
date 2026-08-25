"""Integration tests for the full cookie/refresh-token auth flow against a
REAL Postgres connection — not a mocked db.

This file exists specifically because the mocked-session test
(test_cookie_auth.py) could not have caught, and did not catch, a real
regression: the RefreshToken model shipped without its Alembic migration,
so `db.add(RefreshToken(...))` failed with `UndefinedTable` against any
real database while passing every test that mocks `db`. A Mock's
`.add()`/`.commit()` succeed unconditionally regardless of whether the
table they're pretending to write to actually exists — so a fully mocked
auth test suite can go green while login is completely broken end to end.
These tests exercise the real INSERT/SELECT path that only a real
database enforces.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Organization, RefreshToken, User
from app.security import hash_password


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def real_user(client):
    """Creates a real user via the actual /auth/register endpoint (which
    itself only touches User + Organization, both of which already had
    working migrations) so this test's login step is exercising a
    genuine, previously-committed row — not test-only setup magic."""
    email = f"cookie-integration-{uuid.uuid4().hex[:8]}@test.com"
    password = "testpass123"
    resp = client.post(
        "/auth/register",
        json={"email": email, "password": password, "organization_name": "CookieIntegrationOrg"},
    )
    assert resp.status_code == 201, resp.text
    yield email, password

    # cleanup
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user:
            db.query(RefreshToken).filter(RefreshToken.user_id == user.id).delete()
            db.query(Organization).filter(Organization.owner_id == user.id).delete()
            db.delete(user)
            db.commit()
    finally:
        db.close()


def test_login_actually_persists_a_refresh_token_row(client, real_user):
    """This is the test that would have caught the missing migration: it
    asserts a real row exists in a real table after login, which requires
    the table to actually exist."""
    email, password = real_user
    resp = client.post("/auth/login", data={"username": email, "password": password})
    assert resp.status_code == 204, resp.text
    assert "pulse_access_token" in resp.cookies
    assert "pulse_refresh_token" in resp.cookies

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        tokens = db.query(RefreshToken).filter(RefreshToken.user_id == user.id).all()
        assert len(tokens) == 1
        assert tokens[0].revoked_at is None
    finally:
        db.close()


def test_full_login_refresh_logout_cycle_against_real_db(client, real_user):
    email, password = real_user

    login_resp = client.post("/auth/login", data={"username": email, "password": password})
    assert login_resp.status_code == 204

    me_resp = client.get("/auth/me")
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == email

    refresh_resp = client.post("/auth/refresh")
    assert refresh_resp.status_code == 204

    me_after_refresh = client.get("/auth/me")
    assert me_after_refresh.status_code == 200

    logout_resp = client.post("/auth/logout")
    assert logout_resp.status_code == 204


def test_replayed_rotated_refresh_token_revokes_the_whole_family(client, real_user):
    email, password = real_user
    login_resp = client.post("/auth/login", data={"username": email, "password": password})
    old_refresh_cookie = login_resp.cookies.get("pulse_refresh_token")
    assert old_refresh_cookie

    # Rotate once — the old token is now revoked server-side.
    refresh_resp = client.post("/auth/refresh")
    assert refresh_resp.status_code == 204

    # Replay the OLD refresh token (simulating a stolen/leaked token being
    # reused after the legitimate client already rotated past it).
    client.cookies.set("pulse_refresh_token", old_refresh_cookie)
    replay_resp = client.post("/auth/refresh")
    assert replay_resp.status_code == 401

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        tokens = db.query(RefreshToken).filter(RefreshToken.user_id == user.id).all()
        # every token in the family must be revoked after a replay is detected
        assert all(t.revoked_at is not None for t in tokens)
    finally:
        db.close()
