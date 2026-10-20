"""Comprehensive tests for Outbound Webhook Delivery Engine:
1. Secret generation & cryptographic uniqueness
2. HMAC-SHA256 signing and signature verification with replay attack protection
3. SSRF protection on invalid/private/cloud-metadata URLs
4. Webhook dispatch filtering (exact matches, wildcards, inactive subscriptions)
5. Delivery execution with HTTP response handling and duration tracking
6. Celery deliver_webhook task retry logic (transient vs non-transient)
7. Full REST API endpoints with authentication and permission checks
"""

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.database import Base
from app.models import Organization, OrganizationMember, OrgMemberRole, User, UserRole, WebhookDelivery, WebhookSubscription
from app.webhooks import (
    DEFAULT_TIMEOUT_SECONDS,
    dispatch_webhooks_for_event,
    execute_delivery,
    generate_webhook_secret,
    sign_webhook_payload,
    verify_webhook_signature,
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


def test_generate_webhook_secret():
    """Verify secrets are prefixed with whsec_ and cryptographically unique."""
    s1 = generate_webhook_secret()
    s2 = generate_webhook_secret()
    assert s1.startswith("whsec_")
    assert s2.startswith("whsec_")
    assert len(s1) == 54  # whsec_ (6) + 48 hex chars (24 bytes)
    assert s1 != s2


def test_sign_and_verify_webhook_signature():
    """Verify HMAC signature generation, successful verification, and tampering rejection."""
    secret = "whsec_0123456789abcdef0123456789abcdef"
    payload = json.dumps({"event": "job.completed", "job_id": "job-123"})
    timestamp = int(datetime.now(timezone.utc).timestamp())

    sig = sign_webhook_payload(secret, payload, timestamp)
    assert sig.startswith("v1=")

    # Authentic payload and timestamp
    assert verify_webhook_signature(secret, sig, timestamp, payload) is True

    # Tampered payload fails
    tampered_payload = json.dumps({"event": "job.completed", "job_id": "job-999"})
    assert verify_webhook_signature(secret, sig, timestamp, tampered_payload) is False

    # Tampered timestamp fails
    assert verify_webhook_signature(secret, sig, timestamp + 10, payload) is False

    # Wrong secret fails
    assert verify_webhook_signature("whsec_wrongsecret", sig, timestamp, payload) is False

    # Replay attack prevention: timestamp older than tolerance window (300s)
    old_timestamp = timestamp - 301
    old_sig = sign_webhook_payload(secret, payload, old_timestamp)
    assert verify_webhook_signature(secret, old_sig, old_timestamp, payload, tolerance_seconds=300) is False


def test_ssrf_blocked_on_delivery(memory_db):
    """Verify SSRF targets (loopback, metadata IP) are blocked and never make network requests."""
    org_id = str(uuid.uuid4())
    sub = WebhookSubscription(
        org_id=org_id,
        url="http://169.254.169.254/latest/meta-data",
        secret=generate_webhook_secret(),
        events=["*"],
    )
    memory_db.add(sub)
    memory_db.flush()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event="job.created",
        payload={"job_id": "test"},
    )
    memory_db.add(delivery)
    memory_db.commit()

    with patch("app.webhooks.urlopen") as mock_urlopen:
        success, code, err = execute_delivery(memory_db, delivery.id)
        assert success is False
        assert code is None
        assert "SSRF validation blocked" in (err or "")
        mock_urlopen.assert_not_called()

    # Delivery row updated with failure
    memory_db.refresh(delivery)
    assert delivery.is_success is False
    assert "SSRF" in delivery.error_message


def test_execute_delivery_success(memory_db):
    """Verify successful outbound delivery sets response headers, duration, and status."""
    org_id = str(uuid.uuid4())
    sub = WebhookSubscription(
        org_id=org_id,
        url="https://api.external-customer.com/webhooks",
        secret="whsec_testsecret123",
        events=["*"],
    )
    memory_db.add(sub)
    memory_db.flush()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event="job.completed",
        payload={"id": "job-abc", "status": "completed"},
    )
    memory_db.add(delivery)
    memory_db.commit()

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'{"received": true}'
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False

    with patch("app.webhooks.validate_public_url"), patch("app.webhooks.urlopen", return_value=mock_resp) as mock_urlopen:
        success, code, err = execute_delivery(memory_db, delivery.id)
        assert success is True
        assert code == 200
        assert err is None

        # Check request sent to urlopen
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "POST"
        assert req.headers["Content-type"] == "application/json"
        assert req.headers["X-pulse-event"] == "job.completed"
        assert req.headers["X-pulse-signature"].startswith("v1=")
        assert req.headers["X-pulse-timestamp"] is not None

    memory_db.refresh(delivery)
    assert delivery.is_success is True
    assert delivery.status_code == 200
    assert delivery.response_body == '{"received": true}'
    assert delivery.duration_ms is not None


def test_dispatch_webhooks_for_event(memory_db):
    """Verify event matching: wildcards, specific event matching, and inactive filtering."""
    org1_id = str(uuid.uuid4())
    org2_id = str(uuid.uuid4())

    sub1 = WebhookSubscription(org_id=org1_id, url="https://cust1.com/hook", secret=generate_webhook_secret(), events=["*"], is_active=True)
    sub2 = WebhookSubscription(org_id=org1_id, url="https://cust2.com/hook", secret=generate_webhook_secret(), events=["job.failed"], is_active=True)
    sub3 = WebhookSubscription(org_id=org1_id, url="https://cust3.com/hook", secret=generate_webhook_secret(), events=["job.completed"], is_active=True)
    sub_inactive = WebhookSubscription(org_id=org1_id, url="https://cust4.com/hook", secret=generate_webhook_secret(), events=["*"], is_active=False)
    sub_other_org = WebhookSubscription(org_id=org2_id, url="https://other.com/hook", secret=generate_webhook_secret(), events=["*"], is_active=True)

    memory_db.add_all([sub1, sub2, sub3, sub_inactive, sub_other_org])
    memory_db.commit()

    # Event: job.completed
    ids = dispatch_webhooks_for_event(memory_db, org1_id, "job.completed", {"id": "1"})
    # Should match sub1 (wildcard) and sub3 (exact), NOT sub2, sub_inactive, or sub_other_org
    assert len(ids) == 2

    # 2 outbox events recorded atomically for transactional delivery
    from app.models import OutboxEvent
    outbox_events = memory_db.query(OutboxEvent).filter(OutboxEvent.aggregate_id.in_(ids)).all()
    assert len(outbox_events) == 2

    with patch("app.celery_app.celery_app.send_task") as mock_send:
        from app.outbox import publish_pending_outbox_events
        publish_pending_outbox_events(memory_db)
        assert mock_send.call_count == 2
        for call in mock_send.call_args_list:
            assert call[1]["name"] == "app.tasks.deliver_webhook"
            assert call[1]["queue"] == "pulse.internal"


def test_deliver_webhook_retry_logic():
    """Verify Celery deliver_webhook task retries transient 5xx/network errors, but not 4xx or SSRF."""
    from app.tasks import deliver_webhook

    # Test 1: SSRF error should NOT retry
    task = deliver_webhook
    task.request.retries = 0
    task.retry = Mock(return_value=RuntimeError("Retry"))
    with patch("app.database.SessionLocal"), patch("app.webhooks.execute_delivery", return_value=(False, None, "SSRF validation blocked: loopback")):
        res = task("deliv-1")
        assert res["success"] is False
        task.retry.assert_not_called()

    # Test 2: HTTP 400 Bad Request should NOT retry
    task.retry.reset_mock()
    with patch("app.database.SessionLocal"), patch("app.webhooks.execute_delivery", return_value=(False, 400, "HTTP 400: Bad Request")):
        res = task("deliv-2")
        assert res["success"] is False
        task.retry.assert_not_called()

    # Test 3: HTTP 500 Internal Error SHOULD trigger retry
    task.retry.reset_mock()
    task.retry.side_effect = RuntimeError("Retry triggered")
    with patch("app.database.SessionLocal"), patch("app.webhooks.execute_delivery", return_value=(False, 500, "HTTP 500: Internal Server Error")):
        with pytest.raises(RuntimeError, match="Retry triggered"):
            task("deliv-3")
        task.retry.assert_called_once()
        assert task.retry.call_args[1]["countdown"] == 5

    # Test 4: Network timeout (status_code=None) SHOULD trigger retry with exponential backoff
    task.retry.reset_mock()
    task.request.retries = 1
    task.retry.side_effect = RuntimeError("Retry triggered")
    with patch("app.database.SessionLocal"), patch("app.webhooks.execute_delivery", return_value=(False, None, "Connection timed out")):
        with pytest.raises(RuntimeError, match="Retry triggered"):
            task("deliv-4")
        task.retry.assert_called_once()
        assert task.retry.call_args[1]["countdown"] == 25  # 5 * 5^1 = 25s


def test_webhook_api_routes(memory_db):
    """Verify all /orgs/me/webhooks REST API endpoints with authentication and permission checks."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.deps import get_current_user, require_org_admin, get_db

    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    user = User(
        id=user_id,
        email="webhook-admin@test.com",
        hashed_password="some_hashed_password",
        full_name="Admin",
        role=UserRole.ORG_ADMIN,
    )
    org = Organization(id=org_id, name="Webhook Org", owner_id=user.id)
    memory_db.add_all([user, org])
    memory_db.commit()

    app.dependency_overrides[get_db] = lambda: memory_db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_org_admin] = lambda: (user, org)

    try:
        with TestClient(app) as client:
            # 1. Reject invalid / SSRF target
            resp = client.post("/orgs/me/webhooks", json={"url": "http://127.0.0.1:8000/bad"})
            assert resp.status_code == 400
            assert "Invalid webhook URL" in resp.json()["detail"]

            # 2. Register valid webhook
            with patch("app.routers.webhooks.validate_public_url"):
                resp = client.post("/orgs/me/webhooks", json={
                    "url": "https://example.com/pulse-hook",
                    "events": ["job.completed", "job.failed"],
                    "description": "Production webhook",
                })
                assert resp.status_code == 201
                data = resp.json()
                assert data["url"] == "https://example.com/pulse-hook"
                assert data["secret"].startswith("whsec_")
                assert data["events"] == ["job.completed", "job.failed"]
                sub_id = data["id"]

            # 3. List webhooks
            resp = client.get("/orgs/me/webhooks")
            assert resp.status_code == 200
            subs = resp.json()
            assert len(subs) == 1
            assert subs[0]["id"] == sub_id

            # 4. Get webhook detail
            resp = client.get(f"/orgs/me/webhooks/{sub_id}")
            assert resp.status_code == 200
            assert resp.json()["id"] == sub_id

            # 5. Patch webhook
            resp = client.patch(f"/orgs/me/webhooks/{sub_id}", json={"is_active": False})
            assert resp.status_code == 200
            assert resp.json()["is_active"] is False

            # 6. Test ping webhook
            with patch("app.routers.webhooks.execute_delivery", return_value=(True, 200, None)):
                resp = client.post(f"/orgs/me/webhooks/{sub_id}/test")
                assert resp.status_code == 200
                test_res = resp.json()
                assert test_res["status"] == "success"
                assert test_res["is_success"] is True

            # 7. List delivery history
            resp = client.get(f"/orgs/me/webhooks/{sub_id}/deliveries")
            assert resp.status_code == 200
            deliveries = resp.json()
            assert len(deliveries) >= 1
            assert deliveries[0]["event"] == "webhook.test"

            # 8. Delete webhook
            resp = client.delete(f"/orgs/me/webhooks/{sub_id}")
            assert resp.status_code == 200
            assert resp.json()["status"] == "deleted"

            # 9. Verify 404 after deletion
            resp = client.get(f"/orgs/me/webhooks/{sub_id}")
            assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()
