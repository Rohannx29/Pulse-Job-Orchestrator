"""Integration test suite for Phase 3 Network and Session Security hardening:
1. Rejection of query-string JWT tokens on general API endpoints.
2. Short-lived, single-use SSE connection ticket lifecycle.
3. IP spoofing rejection and trusted proxy handling in rate limiting.
4. Fail-closed rate limiting policy on authentication routes during Redis outage.
5. Defense-in-depth SSRF protection across IPv4, IPv6, cloud metadata, and mapped addresses.
6. CSRF protection on cookie-authenticated mutations.
7. /metrics endpoint token and internal network protection.
"""

from __future__ import annotations

import ipaddress
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Organization, User, UserRole
from app.rate_limiter import get_client_ip, is_trusted_proxy
from app.security import create_access_token, hash_password
from app.url_safety import (
    CLOUD_METADATA_IPS,
    execute_safe_http_request,
    is_ip_disallowed,
    resolve_and_validate_public_url,
    validate_public_url,
)


@pytest.fixture
def memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(memory_db):
    def override_get_db():
        yield memory_db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, base_url="http://localhost:3000") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def test_user(memory_db):
    from app.models import OrganizationMember, OrgMemberRole

    user = User(
        email="netsec_user@example.com",
        hashed_password=hash_password("ValidPassword123!"),
        full_name="NetSec User",
        role=UserRole.ORG_ADMIN,
    )
    memory_db.add(user)
    memory_db.flush()

    org = Organization(name="NetSec Test Org", owner_id=user.id)
    memory_db.add(org)
    memory_db.flush()

    membership = OrganizationMember(
        org_id=org.id,
        user_id=user.id,
        role=OrgMemberRole.ADMIN,
    )
    memory_db.add(membership)
    memory_db.commit()
    memory_db.refresh(user)
    user.org_id = org.id
    return user



# ============================================================================
# 1. Query-String JWT Removal & Cookie Authentication
# ============================================================================

def test_query_string_jwt_rejected_on_api(client, test_user):
    token = create_access_token(subject=test_user.id)

    # Query string token MUST be rejected
    res = client.get(f"/orgs/me?token={token}")
    assert res.status_code == 401

    # Cookie auth MUST succeed
    client.cookies.set(settings.ACCESS_TOKEN_COOKIE_NAME, token)
    res = client.get("/orgs/me")
    assert res.status_code == 200
    assert res.json()["name"] == "NetSec Test Org"


# ============================================================================
# 2. SSE Ticket Generation & Single-Use Consumption
# ============================================================================

def test_sse_ticket_lifecycle(client, test_user):
    token = create_access_token(subject=test_user.id)
    client.cookies.set(settings.ACCESS_TOKEN_COOKIE_NAME, token)

    # 1. Issue short-lived ticket (with trusted Origin for CSRF compliance)
    res = client.post("/auth/sse-ticket", headers={"Origin": "http://localhost:3000"})
    assert res.status_code == 200
    ticket = res.json().get("ticket")
    assert ticket is not None
    assert ticket.startswith("sset_")

    # 2. Ticket cannot be used on general API endpoints
    client.cookies.clear()
    res = client.get(f"/orgs/me?token={ticket}")
    assert res.status_code == 401

    # 3. First consumption of ticket on /events succeeds (authenticates user)
    with client.stream("GET", f"/events?ticket={ticket}") as stream_res:
        assert stream_res.status_code == 200

    # 4. Replay fails: ticket was already consumed atomically
    res_replay = client.get(f"/events?ticket={ticket}")
    assert res_replay.status_code == 401




# ============================================================================
# 3. Client IP Spoofing & Trusted Proxy Identity
# ============================================================================

def test_client_ip_spoofing_defense():
    req = MagicMock()
    req.client.host = "198.51.100.5"  # Direct client is an untrusted public IP
    req.headers = Headers({"x-forwarded-for": "10.0.0.1, 8.8.8.8"})

    with patch.object(settings, "TRUSTED_PROXIES", ["127.0.0.1", "10.0.0.0/8"]):
        # Untrusted direct client attempting to spoof X-Forwarded-For must be ignored
        assert not is_trusted_proxy("198.51.100.5")
        resolved_ip = get_client_ip(req)
        assert resolved_ip == "198.51.100.5"


def test_client_ip_from_trusted_proxy():
    req = MagicMock()
    req.client.host = "10.0.2.15"  # Direct client is inside trusted proxy range
    req.headers = Headers({"x-forwarded-for": "203.0.113.195, 10.0.2.15"})

    with patch.object(settings, "TRUSTED_PROXIES", ["127.0.0.1", "10.0.0.0/8"]):
        assert is_trusted_proxy("10.0.2.15")
        resolved_ip = get_client_ip(req)
        # Real client IP from leftmost untrusted header entry
        assert resolved_ip == "203.0.113.195"


# ============================================================================
# 4. Fail-Closed Rate Limiter on Authentication Routes
# ============================================================================

def test_fail_closed_rate_limiter_on_redis_outage(client):
    # Simulate Redis connection unavailable during login
    with patch("app.rate_limiter.get_redis_client", return_value=None):
        res = client.post(
            "/auth/login",
            data={"username": "netsec_user@example.com", "password": "ValidPassword123!"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # Fail-closed policy on auth endpoint must reject with 503 rather than allowing brute force
        assert res.status_code == 503
        assert "Rate limiter unavailable" in res.json()["detail"]


# ============================================================================
# 5. Defense-in-Depth SSRF Protection
# ============================================================================

def test_ssrf_disallowed_addresses():
    # Loopback IPv4 and IPv6
    assert is_ip_disallowed(ipaddress.ip_address("127.0.0.1"))
    assert is_ip_disallowed(ipaddress.ip_address("127.0.0.254"))
    assert is_ip_disallowed(ipaddress.ip_address("::1"))

    # Cloud metadata addresses
    assert is_ip_disallowed(ipaddress.ip_address("169.254.169.254"))
    assert is_ip_disallowed(ipaddress.ip_address("100.100.100.200"))
    assert is_ip_disallowed(ipaddress.ip_address("169.254.10.1"))

    # Private networks
    assert is_ip_disallowed(ipaddress.ip_address("10.0.0.1"))
    assert is_ip_disallowed(ipaddress.ip_address("172.16.0.1"))
    assert is_ip_disallowed(ipaddress.ip_address("192.168.1.1"))
    assert is_ip_disallowed(ipaddress.ip_address("fc00::1"))

    # IPv4-mapped IPv6 loopback and metadata
    assert is_ip_disallowed(ipaddress.ip_address("::ffff:127.0.0.1"))
    assert is_ip_disallowed(ipaddress.ip_address("::ffff:169.254.169.254"))


def test_ssrf_url_validation_rejection():
    # Loopback
    with pytest.raises(ValueError, match="blocked to prevent SSRF"):
        validate_public_url("http://127.0.0.1:8080/admin")

    # Cloud metadata
    with pytest.raises(ValueError, match="blocked to prevent SSRF"):
        validate_public_url("http://169.254.169.254/latest/meta-data/")

    # Disallowed scheme
    with pytest.raises(ValueError, match="URL scheme 'ftp' not allowed"):
        validate_public_url("ftp://example.com/file.txt")


# ============================================================================
# 6. CSRF Protection Middleware
# ============================================================================

def test_csrf_missing_origin_referer_with_cookie(client, test_user):
    token = create_access_token(subject=test_user.id)
    client.cookies.set(settings.ACCESS_TOKEN_COOKIE_NAME, token)


    # State-modifying POST with cookie but without Origin or Referer
    res = client.post("/auth/logout", headers={})
    assert res.status_code == 403
    assert "missing Origin/Referer" in res.json()["detail"]


def test_csrf_untrusted_origin_rejected(client, test_user):
    token = create_access_token(subject=test_user.id)
    client.cookies.set(settings.ACCESS_TOKEN_COOKIE_NAME, token)

    # State-modifying POST from malicious attacker origin
    res = client.post(
        "/auth/logout",
        headers={"Origin": "https://malicious-attacker.com"},
    )
    assert res.status_code == 403
    assert "untrusted origin" in res.json()["detail"]


def test_csrf_trusted_origin_accepted(client, test_user):
    token = create_access_token(subject=test_user.id)
    client.cookies.set(settings.ACCESS_TOKEN_COOKIE_NAME, token)

    # State-modifying POST from configured trusted origin (http://localhost:3000)
    res = client.post(
        "/auth/logout",
        headers={"Origin": "http://localhost:3000"},
    )
    assert res.status_code == 204


def test_csrf_safe_methods_bypass(client, test_user):
    token = create_access_token(subject=test_user.id)
    client.cookies.set(settings.ACCESS_TOKEN_COOKIE_NAME, token)

    # Safe GET method with foreign origin is not blocked by CSRF
    res = client.get("/health/live", headers={"Origin": "https://external-monitor.com"})
    assert res.status_code == 200



# ============================================================================
# 7. /metrics Protection & Authentication
# ============================================================================

def test_metrics_token_protection(client, monkeypatch):
    monkeypatch.setattr(settings, "METRICS_TOKEN", "scrapesec-998877")
    # Without token -> 401
    res = client.get("/metrics")
    assert res.status_code == 401
    assert "WWW-Authenticate" in res.headers

    # With wrong token -> 401
    res = client.get("/metrics", headers={"Authorization": "Bearer wrong-token"})
    assert res.status_code == 401

    # With valid Bearer token -> 200
    res = client.get("/metrics", headers={"Authorization": "Bearer scrapesec-998877"})
    assert res.status_code == 200
    assert "pulse_up" in res.text

    # With valid X-Metrics-Token -> 200
    res = client.get("/metrics", headers={"X-Metrics-Token": "scrapesec-998877"})
    assert res.status_code == 200


def test_metrics_internal_network_protection(client, monkeypatch):
    monkeypatch.setattr(settings, "METRICS_TOKEN", None)
    monkeypatch.setattr(settings, "METRICS_PROTECT", True)

    # From loopback / testclient -> allowed
    res = client.get("/metrics")
    assert res.status_code == 200

    # From internal private network IP -> allowed
    res_private = client.get("/metrics", headers={"X-Forwarded-For": "10.1.2.3"})
    assert res_private.status_code == 200

    # From external public IP -> 403 Forbidden
    res_external = client.get("/metrics", headers={"X-Forwarded-For": "8.8.8.8"})
    assert res_external.status_code == 403
    assert "restricted to internal network" in res_external.json()["detail"]




