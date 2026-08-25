"""Fast, isolated unit tests for cookie-based auth using a mocked DB
session — good for things that don't need a real database at all (cookie
attributes, precedence between a cookie and a stray Bearer header).

This file's mocked `db` cannot verify anything that depends on a real
table actually existing or a real constraint actually holding — a Mock's
`.add()`/`.commit()` succeed unconditionally regardless of what they're
pretending to write. See tests/test_cookie_auth_integration.py for the
real-Postgres coverage (including the test that would have caught this
project's missing-migration bug, which this file's approach could not
have): that file is the one to extend when testing anything that touches
an actual row.
"""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import User
from app.security import hash_password


@pytest.fixture
def client():
    user = User(
        id="123e4567-e89b-12d3-a456-426614174000",
        email="user@example.com",
        full_name="Test User",
        hashed_password=hash_password("correct-horse-battery-staple"),
    )
    db = Mock()
    db.query.return_value.filter.return_value.first.return_value = user
    db.get.return_value = user

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_login_sets_an_httponly_access_cookie(client):
    response = client.post(
        "/auth/login",
        data={"username": "user@example.com", "password": "correct-horse-battery-staple"},
    )

    assert response.status_code == 204
    set_cookie = response.headers["set-cookie"]
    assert "pulse_access_token=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "access_token" not in response.text


def test_cookie_authenticates_me_and_bearer_header_does_not(client):
    login_response = client.post(
        "/auth/login",
        data={"username": "user@example.com", "password": "correct-horse-battery-staple"},
    )
    assert login_response.status_code == 204

    me_response = client.get("/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["email"] == "user@example.com"

    client.cookies.clear()
    bearer_only_response = client.get(
        "/auth/me", headers={"Authorization": "Bearer not-a-cookie"}
    )
    assert bearer_only_response.status_code == 401


def test_logout_expires_the_access_cookie(client):
    response = client.post("/auth/logout")

    assert response.status_code == 204
    set_cookie = response.headers["set-cookie"]
    assert "pulse_access_token=" in set_cookie
    assert "Max-Age=0" in set_cookie
