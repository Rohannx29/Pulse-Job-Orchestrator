"""Outbound Webhook Delivery Engine for Pulse.

Provides reliable, cryptographic HMAC-SHA256 signed webhook delivery to external client endpoints:
- Automatic SSRF defense with validate_public_url before every dispatch.
- Replay and tampering prevention via X-Pulse-Signature and X-Pulse-Timestamp headers.
- Persistent delivery attempt auditing in PostgreSQL (webhook_deliveries).
- Background execution on queue="pulse.internal" with exponential backoff retries.
- Prometheus delivery telemetry and status tracking.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request as URLRequest, urlopen

from sqlalchemy.orm import Session

from app.kms import decrypt_webhook_secret
from app.redaction import redact_sensitive_text
from app.timeutils import utcnow
from app.url_safety import validate_public_url

logger = logging.getLogger(__name__)

# Delivery constants
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 2048
MAX_DELIVERY_ATTEMPTS = 3


def generate_webhook_secret() -> str:
    """Generates a cryptographically random, prefixed HMAC signing secret."""
    return f"whsec_{secrets.token_hex(24)}"


def sign_webhook_payload(secret: str, payload_str: str, timestamp: int) -> str:
    """Computes an HMAC-SHA256 signature for the given payload and timestamp.
    
    Signed string format: "{timestamp}.{payload_str}"
    Signature output format: "v1={hex_digest}"
    """
    signature_input = f"{timestamp}.{payload_str}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signature_input, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def verify_webhook_signature(
    secret: str,
    signature_header: str,
    timestamp_header: int,
    payload_str: str,
    tolerance_seconds: int = 300,
) -> bool:
    """Verifies that an incoming webhook signature is authentic and within time tolerance.

    Supports dual-signed headers (comma-separated signatures) during secret rotation.
    """
    now = int(datetime.now(timezone.utc).timestamp())
    if abs(now - timestamp_header) > tolerance_seconds:
        return False

    expected_sig = sign_webhook_payload(secret, payload_str, timestamp_header)
    candidate_sigs = [s.strip() for s in signature_header.split(",") if s.strip()]
    return any(hmac.compare_digest(expected_sig, cand) for cand in candidate_sigs)


def dispatch_webhooks_for_event(
    db: Session,
    org_id: str,
    event_type: str,
    data: Dict[str, Any],
) -> list[str]:
    """Finds all active webhook subscriptions for an organization matching the event,
    creates WebhookDelivery records, and enqueues Celery delivery tasks on pulse.internal."""
    from app.models import WebhookSubscription, WebhookDelivery

    subscriptions = (
        db.query(WebhookSubscription)
        .filter(
            WebhookSubscription.org_id == org_id,
            WebhookSubscription.is_active.is_(True),
        )
        .all()
    )

    delivery_ids = []
    for sub in subscriptions:
        sub_events = sub.events or []
        # Match wildcard '*' or exact event_type
        if "*" in sub_events or event_type in sub_events:
            delivery = WebhookDelivery(
                subscription_id=sub.id,
                event=event_type,
                payload=data,
                attempt_number=1,
                is_success=False,
                delivered_at=utcnow(),
            )
            db.add(delivery)
            db.flush()
            delivery_ids.append(delivery.id)

            try:
                from app.tasks import deliver_webhook
                deliver_webhook.apply_async(args=[delivery.id], queue="pulse.internal")
            except Exception as exc:
                logger.debug("Failed to enqueue deliver_webhook for %s: %s", delivery.id, exc)

    if delivery_ids:
        db.commit()

    return delivery_ids


def execute_delivery(
    db: Session,
    delivery_id: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Tuple[bool, Optional[int], Optional[str]]:
    """Performs the actual outbound HTTP POST to the webhook endpoint,
    validates SSRF safety, signs headers, and updates the delivery record.
    
    Returns (is_success, status_code, error_message).
    """
    from app.models import WebhookDelivery, WebhookSubscription

    delivery: Optional[WebhookDelivery] = db.get(WebhookDelivery, delivery_id)
    if delivery is None:
        return False, None, "Delivery record not found"

    subscription: Optional[WebhookSubscription] = db.get(WebhookSubscription, delivery.subscription_id)
    if subscription is None:
        return False, None, "Subscription record not found"

    # Increment attempt counter on retries
    if delivery.status_code is not None or delivery.error_message is not None:
        delivery.attempt_number += 1

    # 1. Enforce strict SSRF validation
    try:
        validate_public_url(subscription.url)
    except ValueError as exc:
        err_msg = f"SSRF validation blocked delivery: {exc}"
        logger.warning(err_msg)
        delivery.is_success = False
        delivery.error_message = err_msg
        delivery.duration_ms = 0.0
        db.commit()
        return False, None, err_msg

    # 2. Build structured envelope
    now_ts = int(datetime.now(timezone.utc).timestamp())
    now_iso = utcnow().isoformat()
    envelope = {
        "id": delivery.id,
        "event": delivery.event,
        "org_id": subscription.org_id,
        "timestamp": now_iso,
        "data": delivery.payload,
    }
    payload_json = json.dumps(envelope, separators=(",", ":"), default=str)
    plain_secret = decrypt_webhook_secret(subscription.secret)
    signature = sign_webhook_payload(plain_secret, payload_json, now_ts)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Pulse-Webhook-Deliverer/1.0",
        "X-Pulse-Signature": signature,
        "X-Pulse-Timestamp": str(now_ts),
        "X-Pulse-Event": delivery.event,
        "X-Pulse-Delivery-ID": delivery.id,
    }

    # If previous secret exists and grace period is active, dual-sign for non-destructive rotation
    if subscription.previous_secret and subscription.previous_secret_expires_at:
        if subscription.previous_secret_expires_at > utcnow():
            try:
                prev_plain = decrypt_webhook_secret(subscription.previous_secret)
                prev_sig = sign_webhook_payload(prev_plain, payload_json, now_ts)
                headers["X-Pulse-Signature-Previous"] = prev_sig
                headers["X-Pulse-Signature"] = f"{signature},{prev_sig}"
            except Exception as exc:
                logger.warning("Failed to sign with previous secret during grace window: %s", exc)

    req = URLRequest(
        url=subscription.url,
        data=payload_json.encode("utf-8"),
        method="POST",
        headers=headers,
    )

    t0 = time.perf_counter()
    status_code = None
    response_body = None
    error_message = None
    is_success = False

    try:
        with urlopen(req, timeout=timeout_seconds) as resp:
            status_code = resp.status
            raw_data = resp.read(MAX_RESPONSE_BYTES)
            response_body = raw_data.decode("utf-8", errors="replace")[:MAX_RESPONSE_BYTES]
            is_success = (200 <= status_code < 300)
    except HTTPError as exc:
        status_code = exc.code
        raw_data = exc.read(MAX_RESPONSE_BYTES)
        response_body = raw_data.decode("utf-8", errors="replace")[:MAX_RESPONSE_BYTES]
        error_message = f"HTTP {exc.code}: {exc.reason}"
        is_success = False
    except URLError as exc:
        error_message = f"Connection error: {exc.reason}"
        is_success = False
    except Exception as exc:
        error_message = f"Unexpected delivery error: {exc}"
        is_success = False

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

    delivery.status_code = status_code
    delivery.response_body = redact_sensitive_text(response_body)
    delivery.error_message = redact_sensitive_text(error_message)
    delivery.duration_ms = elapsed_ms
    delivery.is_success = is_success
    delivery.delivered_at = utcnow()
    db.commit()

    # Record Prometheus metrics
    try:
        from app.metrics import pulse_webhook_deliveries_total, pulse_webhook_delivery_duration_seconds
        pulse_webhook_deliveries_total.inc(event=delivery.event, status="success" if is_success else "failure")
        pulse_webhook_delivery_duration_seconds.observe(elapsed_ms / 1000.0, event=delivery.event)
    except Exception:
        pass

    return is_success, status_code, error_message
