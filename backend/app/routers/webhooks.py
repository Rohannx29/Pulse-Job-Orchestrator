"""REST API Router for managing Outbound Webhooks, secret rotation, and viewing delivery logs."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.audit import log_audit_event
from app.database import get_db
from app.deps import require_org_admin
from app.kms import encrypt_webhook_secret, mask_webhook_secret
from app.models import Organization, User, WebhookDelivery, WebhookSubscription
from app.schemas import (
    WebhookDeliveryOut,
    WebhookSecretRotateRequest,
    WebhookSecretRotateOut,
    WebhookSubscriptionCreate,
    WebhookSubscriptionCreateOut,
    WebhookSubscriptionOut,
    WebhookSubscriptionUpdate,
    WebhookTestResponse,
)
from app.timeutils import utcnow
from app.url_safety import validate_public_url
from app.webhooks import execute_delivery, generate_webhook_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orgs/me/webhooks", tags=["webhooks"])


@router.post("", response_model=WebhookSubscriptionCreateOut, status_code=status.HTTP_201_CREATED)
def create_webhook_subscription(
    payload: WebhookSubscriptionCreate,
    db: Session = Depends(get_db),
    auth: tuple[User, Organization] = Depends(require_org_admin),
):
    """Register a new outbound webhook endpoint for the organization.

    Generates an HMAC-SHA256 signing secret, encrypts it at rest via KMS,
    and returns the raw secret exactly once in the response.
    """
    user, org = auth

    # 1. Enforce strict SSRF check on user-supplied URL
    try:
        validate_public_url(payload.url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid webhook URL: {exc}",
        )

    # 2. Generate cryptographic HMAC signing secret and encrypt at rest
    raw_secret = generate_webhook_secret()
    encrypted_secret = encrypt_webhook_secret(raw_secret)

    subscription = WebhookSubscription(
        org_id=org.id,
        url=payload.url,
        secret=encrypted_secret,
        events=payload.events or ["*"],
        description=payload.description,
        is_active=True,
    )
    db.add(subscription)
    db.flush()

    log_audit_event(
        db=db,
        action="webhook.create",
        resource_type="webhook_subscription",
        resource_id=subscription.id,
        user=user,
        org_id=org.id,
        details={"url": payload.url, "events": subscription.events},
    )
    db.commit()
    db.refresh(subscription)

    return WebhookSubscriptionCreateOut(
        id=subscription.id,
        org_id=subscription.org_id,
        url=subscription.url,
        secret=raw_secret,
        secret_preview=subscription.secret_preview,
        events=subscription.events,
        is_active=subscription.is_active,
        description=subscription.description,
        created_at=subscription.created_at,
    )


@router.get("", response_model=List[WebhookSubscriptionOut])
def list_webhook_subscriptions(
    db: Session = Depends(get_db),
    auth: tuple[User, Organization] = Depends(require_org_admin),
):
    """List all webhook subscriptions for the organization. Never exposes signing secrets."""
    _, org = auth
    return (
        db.query(WebhookSubscription)
        .filter(WebhookSubscription.org_id == org.id)
        .order_by(WebhookSubscription.created_at.desc())
        .all()
    )


@router.get("/{subscription_id}", response_model=WebhookSubscriptionOut)
def get_webhook_subscription(
    subscription_id: str,
    db: Session = Depends(get_db),
    auth: tuple[User, Organization] = Depends(require_org_admin),
):
    """Get details of a specific webhook subscription. Never exposes signing secrets."""
    _, org = auth
    subscription = db.get(WebhookSubscription, subscription_id)
    if not subscription or subscription.org_id != org.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook subscription not found")
    return subscription


@router.patch("/{subscription_id}", response_model=WebhookSubscriptionOut)
def update_webhook_subscription(
    subscription_id: str,
    payload: WebhookSubscriptionUpdate,
    db: Session = Depends(get_db),
    auth: tuple[User, Organization] = Depends(require_org_admin),
):
    """Update a webhook subscription URL, event filter, active status, or description."""
    user, org = auth
    subscription = db.get(WebhookSubscription, subscription_id)
    if not subscription or subscription.org_id != org.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook subscription not found")

    if payload.url is not None:
        try:
            validate_public_url(payload.url)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid webhook URL: {exc}",
            )
        subscription.url = payload.url

    if payload.events is not None:
        subscription.events = payload.events

    if payload.is_active is not None:
        subscription.is_active = payload.is_active

    if payload.description is not None:
        subscription.description = payload.description

    log_audit_event(
        db=db,
        action="webhook.update",
        resource_type="webhook_subscription",
        resource_id=subscription.id,
        user=user,
        org_id=org.id,
        details={
            "url": subscription.url,
            "events": subscription.events,
            "is_active": subscription.is_active,
        },
    )
    db.commit()
    db.refresh(subscription)

    return subscription


@router.post("/{subscription_id}/rotate-secret", response_model=WebhookSecretRotateOut)
def rotate_webhook_secret(
    subscription_id: str,
    payload: Optional[WebhookSecretRotateRequest] = None,
    db: Session = Depends(get_db),
    auth: tuple[User, Organization] = Depends(require_org_admin),
):
    """Rotates the signing secret for an existing webhook subscription.

    Non-destructively retains the previous secret for a configurable grace period,
    allowing clients to transition credentials without downtime. The new raw secret
    is returned exactly once.
    """
    user, org = auth
    subscription = db.get(WebhookSubscription, subscription_id)
    if not subscription or subscription.org_id != org.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook subscription not found")

    grace_seconds = payload.grace_period_seconds if payload else 3600
    new_raw_secret = generate_webhook_secret()
    new_encrypted = encrypt_webhook_secret(new_raw_secret)

    # Retain old secret during grace period for dual signing / validation
    subscription.previous_secret = subscription.secret
    subscription.previous_secret_expires_at = (
        utcnow() + timedelta(seconds=grace_seconds) if grace_seconds > 0 else None
    )
    subscription.secret = new_encrypted

    log_audit_event(
        db=db,
        action="webhook.rotate_secret",
        resource_type="webhook_subscription",
        resource_id=subscription.id,
        user=user,
        org_id=org.id,
        details={
            "url": subscription.url,
            "grace_period_seconds": grace_seconds,
            "previous_secret_valid_until": (
                subscription.previous_secret_expires_at.isoformat()
                if subscription.previous_secret_expires_at
                else None
            ),
        },
    )
    db.commit()
    db.refresh(subscription)

    return WebhookSecretRotateOut(
        id=subscription.id,
        secret=new_raw_secret,
        secret_preview=mask_webhook_secret(new_raw_secret),
        previous_secret_valid_until=subscription.previous_secret_expires_at,
    )


@router.delete("/{subscription_id}")
def delete_webhook_subscription(
    subscription_id: str,
    db: Session = Depends(get_db),
    auth: tuple[User, Organization] = Depends(require_org_admin),
):
    """Permanently delete a webhook subscription and its delivery logs."""
    user, org = auth
    subscription = db.get(WebhookSubscription, subscription_id)
    if not subscription or subscription.org_id != org.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook subscription not found")

    log_audit_event(
        db=db,
        action="webhook.delete",
        resource_type="webhook_subscription",
        resource_id=subscription.id,
        user=user,
        org_id=org.id,
        details={"url": subscription.url},
    )
    db.delete(subscription)
    db.commit()

    return {"status": "deleted", "id": subscription_id}


@router.post("/{subscription_id}/test", response_model=WebhookTestResponse)
def test_webhook_subscription(
    subscription_id: str,
    db: Session = Depends(get_db),
    auth: tuple[User, Organization] = Depends(require_org_admin),
):
    """Trigger an immediate test delivery ('webhook.test') to the subscription endpoint."""
    user, org = auth
    subscription = db.get(WebhookSubscription, subscription_id)
    if not subscription or subscription.org_id != org.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook subscription not found")

    delivery = WebhookDelivery(
        subscription_id=subscription.id,
        event="webhook.test",
        payload={
            "message": "Test ping from Pulse Webhook Delivery Engine",
            "subscription_id": subscription.id,
            "org_id": org.id,
            "triggered_by": user.email,
        },
        attempt_number=1,
        is_success=False,
        delivered_at=utcnow(),
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)

    # Execute synchronous test delivery
    is_success, status_code, error_message = execute_delivery(db, delivery.id)
    db.refresh(delivery)

    return WebhookTestResponse(
        status="success" if is_success else "failed",
        delivery_id=delivery.id,
        status_code=status_code,
        is_success=is_success,
        duration_ms=delivery.duration_ms,
    )


@router.get("/{subscription_id}/deliveries", response_model=List[WebhookDeliveryOut])
def list_webhook_deliveries(
    subscription_id: str,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    auth: tuple[User, Organization] = Depends(require_org_admin),
):
    """List recent delivery attempts for a webhook subscription with redacted payload/response bodies."""
    _, org = auth
    subscription = db.get(WebhookSubscription, subscription_id)
    if not subscription or subscription.org_id != org.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook subscription not found")

    return (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.subscription_id == subscription.id)
        .order_by(WebhookDelivery.delivered_at.desc())
        .limit(limit)
        .all()
    )
