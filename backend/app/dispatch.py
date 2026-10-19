"""Single place that knows how to hand a Job to the execution pipeline.

Records a durable Transactional Outbox event in PostgreSQL before triggering Celery,
guaranteeing state changes are committed first, preventing duplicate dispatch
caused by internal races or network partitions.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Job, JobStatus
from app.timeutils import utcnow

logger = logging.getLogger(__name__)


def dispatch_job(job: Job, db: Optional[Session] = None) -> None:
    """Dispatches a job by recording an OutboxEvent and publishing it.

    If a database session is provided, records in that session; otherwise uses SessionLocal.
    Guarantees the outbox record is committed to PostgreSQL before attempting Celery publish.
    """
    from app.database import SessionLocal
    from app.outbox import publish_pending_outbox_events, record_event_outbox, record_job_dispatch_outbox

    if job.run_at and job.run_at > utcnow():
        job.status = JobStatus.SCHEDULED
    else:
        job.status = JobStatus.QUEUED

    session = db or SessionLocal()
    should_close = (db is None)

    try:
        record_job_dispatch_outbox(session, job)
        if job.queue and job.queue.project:
            record_event_outbox(
                session,
                job.queue.project.org_id,
                "job.queued" if job.status == JobStatus.QUEUED else "job.scheduled",
                {
                    "job_id": job.id,
                    "queue_id": job.queue_id,
                    "name": job.name,
                    "status": job.status.value,
                    "priority": job.priority,
                },
            )
        session.commit()

        # Immediate outbox publication attempt for minimal latency
        try:
            publish_pending_outbox_events(session)
        except Exception as exc:
            logger.debug("Immediate outbox publish deferred to background worker: %s", exc)

    finally:
        if should_close:
            session.close()
