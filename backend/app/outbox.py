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
    celery_queue = priority_to_celery_queue(job.priority if job.priority is not None else 5)
    eta = job.run_at if (job.run_at and job.run_at > utcnow()) else None

    generation = getattr(job, "dispatch_generation", None) or 1
    actual_fencing_key = fencing_key or f"job:dispatch:{job.id}:{generation}"

    payload = {
        "task_name": "app.tasks.execute_job",
        "args": [str(job.id)],
        "kwargs": {"dispatch_generation": generation},
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


def reconcile_stranded_jobs(db: Session, max_age_seconds: int = 30, limit: int = 50) -> int:
    """Detects and reconciles dispatchable jobs missing an active dispatch outbox record.

    Ensures committed jobs are never stranded if a crash occurred between job creation
    and outbox generation, or if outbox records were exhausted/lost.
    """
    from datetime import timedelta
    cutoff = utcnow() - timedelta(seconds=max_age_seconds)
    now = utcnow()

    # Query dispatchable jobs: QUEUED or SCHEDULED where run_at is due
    candidate_jobs = (
        db.query(Job)
        .filter(
            Job.status.in_([JobStatus.QUEUED, JobStatus.SCHEDULED]),
            Job.created_at <= cutoff,
        )
        .limit(limit)
        .all()
    )

    reconciled_count = 0
    for job in candidate_jobs:
        # If SCHEDULED and run_at is still in the future, it is waiting on schedule, not stranded
        if job.status == JobStatus.SCHEDULED and job.run_at and job.run_at > now:
            continue

        gen = getattr(job, "dispatch_generation", None) or 1
        expected_fencing = f"job:dispatch:{job.id}:{gen}"

        # Check if an active (PENDING or DISPATCHED) outbox record exists
        has_outbox = (
            db.query(OutboxEvent.id)
            .filter(
                OutboxEvent.aggregate_id == str(job.id),
                OutboxEvent.aggregate_type == "job",
                OutboxEvent.fencing_key == expected_fencing,
                OutboxEvent.status.in_([OutboxStatus.PENDING, OutboxStatus.DISPATCHED]),
            )
            .first()
        )

        if not has_outbox:
            # Job is stranded! Bump generation to avoid any stale duplicate collision and record outbox
            job.dispatch_generation = gen + 1
            record_job_dispatch_outbox(db, job)
            logger.warning(
                "Reconciled stranded job %s: advanced to generation %d and staged outbox dispatch",
                job.id,
                job.dispatch_generation,
            )
            reconciled_count += 1
            try:
                from app.metrics import pulse_outbox_reconciled_total
                pulse_outbox_reconciled_total.inc()
            except Exception:
                pass

    if reconciled_count > 0:
        db.commit()

    return reconciled_count


def get_outbox_stats(db: Session) -> Dict[str, Any]:
    """Returns operational observability metrics for the outbox table."""
    from sqlalchemy import func

    pending_count = db.query(func.count(OutboxEvent.id)).filter(OutboxEvent.status == OutboxStatus.PENDING).scalar() or 0
    failed_count = db.query(func.count(OutboxEvent.id)).filter(OutboxEvent.status == OutboxStatus.FAILED).scalar() or 0

    oldest_pending = (
        db.query(OutboxEvent.created_at)
        .filter(OutboxEvent.status == OutboxStatus.PENDING)
        .order_by(OutboxEvent.created_at.asc())
        .first()
    )
    oldest_pending_age_seconds = None
    if oldest_pending and oldest_pending[0]:
        oldest_pending_age_seconds = max(0.0, (utcnow() - oldest_pending[0]).total_seconds())

    return {
        "pending_count": pending_count,
        "failed_count": failed_count,
        "oldest_pending_age_seconds": oldest_pending_age_seconds,
    }


def replay_failed_outbox_event(db: Session, event_id: str) -> Optional[OutboxEvent]:
    """Idempotently resets a FAILED outbox event back to PENDING for retry."""
    event = db.get(OutboxEvent, event_id)
    if not event:
        return None

    if event.status == OutboxStatus.FAILED:
        event.status = OutboxStatus.PENDING
        event.retry_count = 0
        event.last_error = None
        db.commit()
        db.refresh(event)
    return event
