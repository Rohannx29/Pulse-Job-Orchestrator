"""Single place that stages a Job for the transactional outbox pipeline.

Guarantees:
- Never creates an independent database session.
- Never commits the caller's transaction.
- Never performs external publication (Celery/Redis/webhooks) inside the request/mutation path.
- State changes (Job status, dispatch outbox row, and event outbox row) are staged
  in the SAME caller-owned session and committed atomically at the outer transaction boundary.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session, object_session

from app.models import Job, JobStatus
from app.timeutils import utcnow

logger = logging.getLogger(__name__)


def dispatch_job(job: Job, db: Optional[Session] = None) -> None:
    """Stages a Job for dispatch by recording an OutboxEvent in the caller's session.

    Does NOT commit the session and does NOT publish to Celery/Redis immediately.
    The caller must commit the transaction at the outer boundary.
    """
    from app.outbox import record_event_outbox, record_job_dispatch_outbox

    session = db or object_session(job)
    if session is None:
        raise ValueError("A valid database session or bound Job instance is required to stage outbox dispatch.")

    # Durable status assignment: SCHEDULED for future runs, QUEUED for runnable jobs
    if job.run_at and job.run_at > utcnow():
        job.status = JobStatus.SCHEDULED
    else:
        job.status = JobStatus.QUEUED

    if getattr(job, "dispatch_generation", None) is None or job.dispatch_generation < 1:
        job.dispatch_generation = 1

    # Stage the dispatch outbox record
    record_job_dispatch_outbox(session, job)

    # Stage the real-time event notification in the same session
    org_id = None
    if job.queue and getattr(job.queue, "project", None):
        org_id = job.queue.project.org_id
    elif getattr(job, "queue_id", None):
        from app.models import Queue
        q = session.get(Queue, job.queue_id)
        if q and q.project:
            org_id = q.project.org_id

    if org_id:
        record_event_outbox(
            session,
            org_id,
            "job.queued" if job.status == JobStatus.QUEUED else "job.scheduled",
            {
                "job_id": job.id,
                "queue_id": job.queue_id,
                "name": job.name,
                "status": job.status.value,
                "priority": job.priority,
            },
        )
