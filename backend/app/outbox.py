"""Transactional Outbox Engine for Pulse.

Provides atomic, durable state transitions and guaranteed at-least-once external dispatch:
- State changes (job creations, status transitions, DAG releases, webhooks, and SSE notifications)
  write OutboxEvent records in the SAME database transaction as application state.
- External publication (to Celery queues or Redis Pub/Sub) only occurs after transaction commit.
- Row-level locking (`with_for_update(skip_locked=True)`) guarantees concurrent publisher workers
  never double-claim or double-dispatch rows.
- Unique fencing keys eliminate duplicate dispatch under races.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import Job, JobStatus, OutboxEvent, OutboxStatus
from app.priority import priority_to_celery_queue
from app.timeutils import utcnow

logger = logging.getLogger(__name__)

MAX_OUTBOX_RETRIES = 5
DEFAULT_BATCH_SIZE = 50


def record_job_dispatch_outbox(
    db: Session,
    job: Job,
    fencing_key: Optional[str] = None,
) -> OutboxEvent:
    """Records a durable job dispatch event in the current DB transaction.

    The task will be published to Celery only after the calling transaction commits.
    """
    from app.logging_config import get_correlation_id

    corr_id = get_correlation_id() or str(job.id)
    celery_queue = priority_to_celery_queue(job.priority)
    eta = job.run_at if (job.run_at and job.run_at > utcnow()) else None

    # Use unique fencing key to prevent multiple dispatch events for the exact same job state
    # e.g., "job:dispatch:<job_id>:<retry_count>"
    actual_fencing_key = fencing_key or f"job:dispatch:{job.id}:{job.current_retry_count}"

    payload = {
        "task_name": "app.tasks.execute_job",
        "args": [str(job.id)],
        "kwargs": {},
        "queue": celery_queue,
        "eta": eta.isoformat() if eta else None,
        "headers": {"correlation_id": corr_id},
    }

    import uuid

    event = OutboxEvent(
        id=str(uuid.uuid4()),
        event_type="job.dispatch",
        aggregate_type="job",
        aggregate_id=str(job.id),
        payload=payload,
        status=OutboxStatus.PENDING,
        fencing_key=actual_fencing_key,
        created_at=utcnow(),
    )
    db.add(event)
    return event


def record_event_outbox(
    db: Session,
    org_id: str,
    event_name: str,
    data: Dict[str, Any],
    fencing_key: Optional[str] = None,
) -> OutboxEvent:
    """Records an SSE real-time notification event in the current DB transaction."""
    import uuid

    event = OutboxEvent(
        id=str(uuid.uuid4()),
        event_type="event.publish",
        aggregate_type="organization",
        aggregate_id=str(org_id),
        payload={"org_id": str(org_id), "event": event_name, "data": data},
        status=OutboxStatus.PENDING,
        fencing_key=fencing_key,
        created_at=utcnow(),
    )
    db.add(event)
    return event


def record_webhook_dispatch_outbox(
    db: Session,
    delivery_id: str,
    fencing_key: Optional[str] = None,
) -> OutboxEvent:
    """Records an outbound webhook delivery dispatch in the current DB transaction."""
    actual_fencing_key = fencing_key or f"webhook:dispatch:{delivery_id}"
    payload = {
        "task_name": "app.tasks.deliver_webhook",
        "args": [str(delivery_id)],
        "kwargs": {},
        "queue": "pulse.internal",
        "headers": {},
    }
    import uuid

    event = OutboxEvent(
        id=str(uuid.uuid4()),
        event_type="webhook.dispatch",
        aggregate_type="webhook_delivery",
        aggregate_id=str(delivery_id),
        payload=payload,
        status=OutboxStatus.PENDING,
        fencing_key=actual_fencing_key,
        created_at=utcnow(),
    )
    db.add(event)
    return event


def publish_single_outbox_event(event: OutboxEvent) -> bool:
    """Executes the external network publication for a claimed outbox event.
    
    Returns True if successfully dispatched, False otherwise.
    """
    payload = event.payload or {}
    event_type = event.event_type

    if event_type in ("job.dispatch", "webhook.dispatch"):
        from app.celery_app import celery_app
        task_name = payload.get("task_name")
        args = payload.get("args", [])
        kwargs = payload.get("kwargs", {})
        queue = payload.get("queue")
        eta_str = payload.get("eta")
        headers = payload.get("headers", {})

        from datetime import datetime
        eta = datetime.fromisoformat(eta_str) if eta_str else None

        celery_app.send_task(
            name=task_name,
            args=args,
            kwargs=kwargs,
            queue=queue,
            eta=eta,
            headers=headers,
        )
        return True

    elif event_type == "event.publish":
        from app.events import publish_event
        org_id = payload.get("org_id")
        evt_name = payload.get("event")
        data = payload.get("data", {})
        if org_id and evt_name:
            publish_event(org_id, evt_name, data)
        return True

    else:
        logger.warning("Unrecognized outbox event type: %s", event_type)
        return False


def publish_pending_outbox_events(db: Session, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    """Safely claims and publishes pending outbox events.

    Uses SELECT ... FOR UPDATE SKIP LOCKED in PostgreSQL to eliminate concurrency races
    between multiple publisher workers.
    """
    query = (
        db.query(OutboxEvent)
        .filter(OutboxEvent.status == OutboxStatus.PENDING)
        .order_by(OutboxEvent.created_at.asc())
        .limit(batch_size)
    )

    # Use row locking if supported by dialect (PostgreSQL)
    try:
        bind = db.get_bind()
        if bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
    except Exception:
        pass

    events: List[OutboxEvent] = query.all()
    if not events:
        return 0

    dispatched_count = 0
    now = utcnow()

    for event in events:
        try:
            success = publish_single_outbox_event(event)
            if success:
                event.status = OutboxStatus.DISPATCHED
                event.published_at = now
                dispatched_count += 1
            else:
                event.retry_count += 1
                event.last_error = "Unknown dispatch failure"
                if event.retry_count >= MAX_OUTBOX_RETRIES:
                    event.status = OutboxStatus.FAILED
        except Exception as exc:
            logger.error("Failed to publish outbox event %s: %s", event.id, exc)
            event.retry_count += 1
            event.last_error = str(exc)
            if event.retry_count >= MAX_OUTBOX_RETRIES:
                event.status = OutboxStatus.FAILED

    db.commit()
    return dispatched_count
