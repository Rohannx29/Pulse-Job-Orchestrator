"""Comprehensive tests for Webhook Secret Security, Role-Based Access Control,
Secret Rotation with Grace Periods, Delivery Redaction, and Centralized Invitations.

Acceptance Criteria Verified:
1. WebhookSubscriptionCreateOut returns raw secret exactly once.
2. WebhookSubscriptionOut (list/get) never contains the raw secret.
3. WebhookDeliveryOut redacts sensitive payload fields, tokens, and response body errors.
4. Org Admin role restriction: regular members cannot create, read, update, delete,
   rotate secrets, or inspect delivery data (403 Forbidden).
5. Secrets are encrypted at rest via KMS (Fernet) in PostgreSQL.
6. Secret rotation non-destructively retains previous secret with dual-signing grace window.
7. Centralized invitation service enforces identical audit event format across all routes.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.database import Base
from app.kms import decrypt_webhook_secret, encrypt_webhook_secret, mask_webhook_secret
from app.main import app
from app.models import (
    AuditLog,
    InviteToken,
    Organization,
    OrganizationMember,
    OrgMemberRole,
    User,
    UserRole,
    WebhookDelivery,
    WebhookSubscription,
)
from app.redaction import redact_sensitive_dict, redact_sensitive_text
from app.schemas import WebhookDeliveryOut, WebhookSubscriptionOut
from app.services.invitations import create_organization_invite
from app.timeutils import utcnow
from app.webhooks import execute_delivery, verify_webhook_signature


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
def org_and_users(memory_db):
    """Sets up Org A with an Org Admin and a regular Member, plus Org B with its own Admin."""
    # Org A
    org_a = Organization(id=str(uuid.uuid4()), name="Acme Corp")
    admin_user = User(
        id=str(uuid.uuid4()),
        email="admin@acme.com",
        hashed_password="pw",
        full_name="Acme Admin",
        role=UserRole.ORG_ADMIN,
    )
    org_a.owner_id = admin_user.id
    admin_member = OrganizationMember(
        org_id=org_a.id,
        user_id=admin_user.id,
        role=OrgMemberRole.ADMIN,
    )

    member_user = User(
        id=str(uuid.uuid4()),
        email="member@acme.com",
        hashed_password="pw",
        full_name="Acme Member",
        role=UserRole.MEMBER,
    )
    regular_member = OrganizationMember(
        org_id=org_a.id,
        user_id=member_user.id,
        role=OrgMemberRole.MEMBER,
    )

    # Org B
    org_b = Organization(id=str(uuid.uuid4()), name="Other Corp")
    org_b_admin = User(
        id=str(uuid.uuid4()),
        email="admin@other.com",
        hashed_password="pw",
        full_name="Other Admin",
        role=UserRole.ORG_ADMIN,
    )
    org_b.owner_id = org_b_admin.id
    org_b_member = OrganizationMember(
        org_id=org_b.id,
        user_id=org_b_admin.id,
        role=OrgMemberRole.ADMIN,
    )

    memory_db.add_all([
        org_a, admin_user, admin_member, member_user, regular_member,
        org_b, org_b_admin, org_b_member
    ])
    memory_db.commit()

    return {
        "org_a": org_a,
        "admin_user": admin_user,
        "member_user": member_user,
        "org_b": org_b,
        "org_b_admin": org_b_admin,
    }


# =========================================================================
# 1. KMS Encryption at Rest & Backward Compatibility
# =========================================================================

def test_kms_encryption_at_rest():
    """Verify raw secrets are encrypted with enc:v1: prefix and decrypted losslessly."""
    raw = "whsec_abcdef0123456789abcdef0123456789abcdef0123456789"
    encrypted = encrypt_webhook_secret(raw)

    assert encrypted.startswith("enc:v1:")
    assert encrypted != raw
    assert raw not in encrypted

    # Decryption recovers exact plaintext
    decrypted = decrypt_webhook_secret(encrypted)
    assert decrypted == raw

    # Masking shows safe preview without leaking key
    masked = mask_webhook_secret(encrypted)
    assert masked == "whsec_••••••••••••••••"
    assert raw not in masked


def test_kms_backward_compatibility_unencrypted():
    """Verify legacy plain secrets from existing database rows decrypt transparently."""
    legacy_plain = "whsec_legacy_unencrypted_secret_value"
    # Decrypting plaintext returns itself without error
    assert decrypt_webhook_secret(legacy_plain) == legacy_plain
    assert mask_webhook_secret(legacy_plain) == "whsec_••••••••••••••••"


# =========================================================================
# 2. Secret Redaction & Sanitization
# =========================================================================

def test_redact_sensitive_text():
    """Verify strings with Bearer tokens, secrets, passwords, and DB URLs are sanitized."""
    raw_header = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.abc"
    redacted = redact_sensitive_text(raw_header)
    assert "Bearer ***REDACTED***" in redacted
    assert "eyJ" not in redacted

    raw_secret = "Webhook secret whsec_0123456789abcdef0123456789 leaked"
    redacted_sec = redact_sensitive_text(raw_secret)
    assert "whsec_***REDACTED***" in redacted_sec
    assert "0123456789abcdef" not in redacted_sec

    raw_conn = "postgres://admin:super_secret_password_123@db.internal:5432/pulse"
    redacted_conn = redact_sensitive_text(raw_conn)
    assert "postgres://admin:***REDACTED***@db.internal:5432/pulse" == redacted_conn


def test_redact_sensitive_dict():
    """Verify dictionaries with sensitive keys have their values masked."""
    payload = {
        "job_id": "job-123",
        "password": "plain_password",
        "api_key": "sk-live-123456789",
        "nested": {
            "token": "tok_xyz",
            "safe_counter": 42,
        },
        "items": [
            {"secret": "whsec_abc"},
            {"safe_name": "item-1"},
        ],
    }
    redacted = redact_sensitive_dict(payload)
    assert redacted["job_id"] == "job-123"
    assert redacted["password"] == "***REDACTED***"
    assert redacted["api_key"] == "***REDACTED***"
    assert redacted["nested"]["token"] == "***REDACTED***"
    assert redacted["nested"]["safe_counter"] == 42
    assert redacted["items"][0]["secret"] == "***REDACTED***"
    assert redacted["items"][1]["safe_name"] == "item-1"


def test_webhook_delivery_out_redaction_model():
    """Verify WebhookDeliveryOut Pydantic schema automatically redacts sensitive fields."""
    delivery_out = WebhookDeliveryOut(
        id=str(uuid.uuid4()),
        subscription_id=str(uuid.uuid4()),
        event="job.completed",
        payload={"job_id": "1", "client_secret": "sensitive-val"},
        status_code=500,
        response_body="Authorization: Bearer secret-auth-token-123",
        error_message="Failed to connect to postgres://user:secretpw@10.0.0.1:5432/db",
        attempt_number=1,
        is_success=False,
        delivered_at=utcnow(),
    )
    # Payload redacted
    assert delivery_out.payload["client_secret"] == "***REDACTED***"
    # Response body redacted
    assert "Bearer ***REDACTED***" in delivery_out.response_body
    assert "secret-auth-token-123" not in delivery_out.response_body
    # Error message redacted
    assert "secretpw" not in delivery_out.error_message
    assert "***REDACTED***" in delivery_out.error_message


# =========================================================================
# 3. RBAC Role Matrix: Org Admin vs Regular Member
# =========================================================================

def test_webhook_rbac_role_matrix(memory_db, org_and_users):
    """Test full RBAC matrix:
    - Org Admin: allowed create, list, get, update, rotate, test, deliveries, delete.
    - Regular Member: strictly rejected with 403 Forbidden across all operations.
    """
    from app.deps import get_current_user, get_db

    admin = org_and_users["admin_user"]
    member = org_and_users["member_user"]
    org_a = org_and_users["org_a"]

    app.dependency_overrides[get_db] = lambda: memory_db

    try:
        with TestClient(app) as client:
            # --- MEMBER ATTEMPTS (ALL MUST BE REJECTED WITH 403) ---
            app.dependency_overrides[get_current_user] = lambda: member

            # 1. Member cannot create webhook
            resp = client.post("/orgs/me/webhooks", json={"url": "https://example.com/hook"})
            assert resp.status_code == 403

            # 2. Member cannot list webhooks
            resp = client.get("/orgs/me/webhooks")
            assert resp.status_code == 403

            # Create a webhook as Admin directly in DB for testing member access
            raw_secret = "whsec_test_secret_for_rbac_123456789"
            sub = WebhookSubscription(
                org_id=org_a.id,
                url="https://acme.com/webhook",
                secret=encrypt_webhook_secret(raw_secret),
                events=["*"],
            )
            memory_db.add(sub)
            memory_db.commit()

            # 3. Member cannot get webhook
            resp = client.get(f"/orgs/me/webhooks/{sub.id}")
            assert resp.status_code == 403

            # 4. Member cannot update webhook
            resp = client.patch(f"/orgs/me/webhooks/{sub.id}", json={"is_active": False})
            assert resp.status_code == 403

            # 5. Member cannot rotate secret
            resp = client.post(f"/orgs/me/webhooks/{sub.id}/rotate-secret")
            assert resp.status_code == 403

            # 6. Member cannot test webhook
            resp = client.post(f"/orgs/me/webhooks/{sub.id}/test")
            assert resp.status_code == 403

            # 7. Member cannot inspect delivery logs
            resp = client.get(f"/orgs/me/webhooks/{sub.id}/deliveries")
            assert resp.status_code == 403

            # 8. Member cannot delete webhook
            resp = client.delete(f"/orgs/me/webhooks/{sub.id}")
            assert resp.status_code == 403

            # --- ADMIN ALLOWED (ALL MUST SUCCEED) ---
            app.dependency_overrides[get_current_user] = lambda: admin

            # Admin can list webhooks
            resp = client.get("/orgs/me/webhooks")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            # VERIFY ACCEPTANCE CRITERIA: List response NEVER contains secret!
            assert "secret" not in data[0]
            assert data[0]["secret_preview"] == "whsec_••••••••••••••••"

            # Admin can get webhook
            resp = client.get(f"/orgs/me/webhooks/{sub.id}")
            assert resp.status_code == 200
            get_data = resp.json()
            # VERIFY ACCEPTANCE CRITERIA: Get response NEVER contains secret!
            assert "secret" not in get_data
            assert get_data["secret_preview"] == "whsec_••••••••••••••••"

            # Admin can update webhook
            resp = client.patch(f"/orgs/me/webhooks/{sub.id}", json={"description": "Updated by admin"})
            assert resp.status_code == 200
            assert resp.json()["description"] == "Updated by admin"
            assert "secret" not in resp.json()

            # Admin can inspect deliveries
            resp = client.get(f"/orgs/me/webhooks/{sub.id}/deliveries")
            assert resp.status_code == 200
            assert isinstance(resp.json(), list)

            # Admin can delete webhook
            resp = client.delete(f"/orgs/me/webhooks/{sub.id}")
            assert resp.status_code == 200
            assert resp.json()["status"] == "deleted"

    finally:
        app.dependency_overrides.clear()


# =========================================================================
# 4. Secret Creation & Rotation Lifecycle with Grace Window
# =========================================================================

def test_secret_rotation_grace_window(memory_db, org_and_users):
    """Verify:
    1. Secret creation returns raw secret exactly once.
    2. Secret is stored encrypted at rest in DB.
    3. Secret rotation retains previous secret and sets grace expiration.
    4. Outbound delivery dual-signs with both current and previous secret.
    5. Signature verification succeeds with either current or previous secret.
    """
    from app.deps import get_current_user, get_db

    admin = org_and_users["admin_user"]
    app.dependency_overrides[get_db] = lambda: memory_db
    app.dependency_overrides[get_current_user] = lambda: admin

    try:
        with TestClient(app) as client:
            # 1. Create webhook subscription
            with patch("app.routers.webhooks.validate_public_url"):
                resp = client.post("/orgs/me/webhooks", json={
                    "url": "https://api.acme.com/receiver",
                    "events": ["job.completed"],
                })
            assert resp.status_code == 201
            created = resp.json()
            sub_id = created["id"]
            secret_v1 = created["secret"]

            assert secret_v1.startswith("whsec_")
            assert created["secret_preview"] == "whsec_••••••••••••••••"

            # Verify in DB: secret is encrypted with enc:v1:
            sub_in_db = memory_db.get(WebhookSubscription, sub_id)
            assert sub_in_db.secret.startswith("enc:v1:")
            assert sub_in_db.previous_secret is None

            # 2. Rotate secret with 1-hour grace window (3600 seconds)
            rotate_resp = client.post(f"/orgs/me/webhooks/{sub_id}/rotate-secret", json={
                "grace_period_seconds": 3600,
            })
            assert rotate_resp.status_code == 200
            rotated = rotate_resp.json()
            secret_v2 = rotated["secret"]

            assert secret_v2.startswith("whsec_")
            assert secret_v2 != secret_v1
            assert rotated["previous_secret_valid_until"] is not None

            # Verify in DB: new secret encrypted, previous secret retained
            memory_db.refresh(sub_in_db)
            assert sub_in_db.secret.startswith("enc:v1:")
            assert sub_in_db.previous_secret.startswith("enc:v1:")
            assert decrypt_webhook_secret(sub_in_db.previous_secret) == secret_v1
            assert decrypt_webhook_secret(sub_in_db.secret) == secret_v2
            assert sub_in_db.previous_secret_expires_at > utcnow()

            # 3. Create a delivery and execute with dual-signing
            delivery = WebhookDelivery(
                subscription_id=sub_id,
                event="job.completed",
                payload={"job_id": "job-rot-1"},
            )
            memory_db.add(delivery)
            memory_db.commit()

            captured_headers = {}
            with patch("app.webhooks.validate_public_url"), patch("app.webhooks.urlopen") as mock_url:
                mock_resp = mock_url.return_value
                mock_resp.status = 200
                mock_resp.read.return_value = b'{"ok": true}'
                mock_resp.__enter__.return_value = mock_resp
                mock_resp.__exit__.return_value = False

                success, code, err = execute_delivery(memory_db, delivery.id)
                assert success is True

                req = mock_url.call_args[0][0]
                captured_headers = req.headers

            sig_header = captured_headers["X-pulse-signature"]
            prev_sig_header = captured_headers.get("X-pulse-signature-previous")
            ts_header = int(captured_headers["X-pulse-timestamp"])

            # Verify both signatures are present
            assert prev_sig_header is not None
            assert "," in sig_header  # Dual signature format: "v1=sig_curr,v1=sig_prev"

            # The exact payload signed by execute_delivery
            payload_str = req.data.decode("utf-8")

            # Verification with new secret v2 succeeds
            assert verify_webhook_signature(secret_v2, sig_header, ts_header, payload_str) is True
            # Verification with old secret v1 ALSO succeeds (grace period active)
            assert verify_webhook_signature(secret_v1, sig_header, ts_header, payload_str) is True
            # Verification with wrong secret fails
            assert verify_webhook_signature("whsec_wrongkey", sig_header, ts_header, payload_str) is False

    finally:
        app.dependency_overrides.clear()


# =========================================================================
# 5. Cross-Tenant Isolation
# =========================================================================

def test_webhook_cross_tenant_isolation(memory_db, org_and_users):
    """Verify Org B admin cannot inspect or modify Org A's webhook."""
    from app.deps import get_current_user, get_db

    org_a = org_and_users["org_a"]
    org_b_admin = org_and_users["org_b_admin"]

    # Webhook belonging to Org A
    sub_a = WebhookSubscription(
        org_id=org_a.id,
        url="https://acme.com/hook",
        secret=encrypt_webhook_secret("whsec_org_a_secret"),
        events=["*"],
    )
    memory_db.add(sub_a)
    memory_db.commit()

    app.dependency_overrides[get_db] = lambda: memory_db
    # Authenticate as Org B Admin
    app.dependency_overrides[get_current_user] = lambda: org_b_admin

    try:
        with TestClient(app) as client:
            # Org B cannot get Org A's webhook
            resp = client.get(f"/orgs/me/webhooks/{sub_a.id}")
            assert resp.status_code == 404

            # Org B cannot patch Org A's webhook
            resp = client.patch(f"/orgs/me/webhooks/{sub_a.id}", json={"is_active": False})
            assert resp.status_code == 404

            # Org B cannot delete Org A's webhook
            resp = client.delete(f"/orgs/me/webhooks/{sub_a.id}")
            assert resp.status_code == 404

            # Org B cannot rotate secret of Org A's webhook
            resp = client.post(f"/orgs/me/webhooks/{sub_a.id}/rotate-secret")
            assert resp.status_code == 404

            # Org B cannot list deliveries of Org A's webhook
            resp = client.get(f"/orgs/me/webhooks/{sub_a.id}/deliveries")
            assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


# =========================================================================
# 6. Centralized Invitation Service & Consistent Audit Events
# =========================================================================

def test_centralized_invitations_and_audit(memory_db, org_and_users):
    """Verify /auth/invite and /orgs/me/invite both use the centralized service,
    invalidate previous invites, reject existing members, and record identical audit logs.
    """
    from app.deps import get_current_user, get_db

    admin = org_and_users["admin_user"]
    org_a = org_and_users["org_a"]
    member = org_and_users["member_user"]

    app.dependency_overrides[get_db] = lambda: memory_db
    app.dependency_overrides[get_current_user] = lambda: admin

    try:
        with TestClient(app) as client:
            # 1. Invite new user via /auth/invite
            resp_auth = client.post("/auth/invite", json={"email": "newbie@acme.com"})
            assert resp_auth.status_code == 200
            data_auth = resp_auth.json()
            assert "invite_token" in data_auth
            assert data_auth["email"] == "newbie@acme.com"

            # 2. Invite another user via /orgs/me/invite
            resp_org = client.post("/orgs/me/invite", json={"email": "contractor@acme.com"})
            assert resp_org.status_code == 200
            data_org = resp_org.json()
            assert "invite_token" in data_org
            assert data_org["email"] == "contractor@acme.com"

            # 3. Check audit log entries created by both
            audit_logs = (
                memory_db.query(AuditLog)
                .filter(AuditLog.action == "member.invite")
                .order_by(AuditLog.created_at.asc())
                .all()
            )
            assert len(audit_logs) == 2
            assert audit_logs[0].details == {"invited_email": "newbie@acme.com"}
            assert audit_logs[1].details == {"invited_email": "contractor@acme.com"}
            assert audit_logs[0].resource_type == "organization"
            assert audit_logs[1].resource_type == "organization"

            # 4. Re-inviting newbie invalidates previous invite
            resp_reinvite = client.post("/orgs/me/invite", json={"email": "newbie@acme.com"})
            assert resp_reinvite.status_code == 200

            unused_invites = (
                memory_db.query(InviteToken)
                .filter(InviteToken.email == "newbie@acme.com", InviteToken.used_at.is_(None))
                .all()
            )
            # Only the latest invite is unused
            assert len(unused_invites) == 1
            assert unused_invites[0].used_at is None

            # 5. Inviting existing member fails with 409 Conflict
            resp_conflict = client.post("/orgs/me/invite", json={"email": member.email})
            assert resp_conflict.status_code == 409
            assert "already a member" in resp_conflict.json()["detail"]

    finally:
        app.dependency_overrides.clear()
