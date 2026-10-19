"""Automated data retention and table pruning engine for Pulse.

Provides safe, batch-scoped pruning for high-growth operational tables:
- WorkerHeartbeat: pruned after HEARTBEAT_RETENTION_DAYS (default 7 days)
- JobLog: pruned after JOB_LOG_RETENTION_DAYS (default 30 days)
- JobExecution: finished executions pruned after EXECUTION_RETENTION_DAYS (default 30 days)
- Job: terminal jobs (COMPLETED, CANCELLED) pruned after COMPLETED_JOB_RETENTION_DAYS (default 60 days)
- AuditLog: compliance audit entries pruned after AUDIT_LOG_RETENTION_DAYS (default 365 days; 0 = keep forever)

Deletions are chunked in configurable batches (default 1,000) to avoid long table locks,
WAL bloat, and database timeouts.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.audit import log_audit_event
from app.config import settings
from app.metrics import record_data_pruned, record_prune_run
from app.models import (
    AuditLog,
    Job,
    JobExecution,
    JobLog,
    JobStatus,
    User,
    WorkerHeartbeat,
)
from app.timeutils import utcnow

logger = logging.getLogger(__name__)


def run_retention_prune(
    db: Session,
    user: Optional[User] = None,
    batch_size: Optional[int] = None,
) -> Dict[str, Any]:
    """Executes data retention pruning across operational and historical tables in safe batches."""
    t0 = time.perf_counter()
    now = utcnow()
    batch = batch_size or settings.PRUNE_BATCH_SIZE

    pruned_heartbeats = 0
    pruned_logs = 0
    pruned_executions = 0
    pruned_jobs = 0
    pruned_audit_logs = 0

    logger.info("Starting automated data retention prune (batch_size=%d)...", batch)

    # 1. Prune Worker Heartbeats
    if settings.HEARTBEAT_RETENTION_DAYS > 0:
        heartbeat_cutoff = now - timedelta(days=settings.HEARTBEAT_RETENTION_DAYS)
        while True:
            subq = (
                db.query(WorkerHeartbeat.id)
                .filter(WorkerHeartbeat.timestamp < heartbeat_cutoff)
                .limit(batch)
                .all()
            )
            ids = [r[0] for r in subq]
            if not ids:
                break
            db.query(WorkerHeartbeat).filter(WorkerHeartbeat.id.in_(ids)).delete(synchronize_session=False)
            db.commit()
            pruned_heartbeats += len(ids)
            if len(ids) < batch:
                break

    # 2. Prune Job Logs
    if settings.JOB_LOG_RETENTION_DAYS > 0:
        log_cutoff = now - timedelta(days=settings.JOB_LOG_RETENTION_DAYS)
        while True:
            subq = db.query(JobLog.id).filter(JobLog.timestamp < log_cutoff).limit(batch).all()
            ids = [r[0] for r in subq]
            if not ids:
                break
            db.query(JobLog).filter(JobLog.id.in_(ids)).delete(synchronize_session=False)
            db.commit()
            pruned_logs += len(ids)
            if len(ids) < batch:
                break

    # 3. Prune Finished Job Executions
    if settings.EXECUTION_RETENTION_DAYS > 0:
        exec_cutoff = now - timedelta(days=settings.EXECUTION_RETENTION_DAYS)
        while True:
            subq = (
                db.query(JobExecution.id)
                .filter(JobExecution.finished_at.isnot(None), JobExecution.finished_at < exec_cutoff)
                .limit(batch)
                .all()
            )
            ids = [r[0] for r in subq]
            if not ids:
                break
            db.query(JobExecution).filter(JobExecution.id.in_(ids)).delete(synchronize_session=False)
            db.commit()
            pruned_executions += len(ids)
            if len(ids) < batch:
                break

    # 4. Prune Terminal Jobs (COMPLETED, DEAD_LETTER)
    if settings.COMPLETED_JOB_RETENTION_DAYS > 0:
        job_cutoff = now - timedelta(days=settings.COMPLETED_JOB_RETENTION_DAYS)
        while True:
            subq = (
                db.query(Job.id)
                .filter(
                    Job.status.in_([JobStatus.COMPLETED, JobStatus.DEAD_LETTER]),
                    Job.updated_at < job_cutoff,
                )
                .limit(batch)
                .all()
            )
            ids = [r[0] for r in subq]
            if not ids:
                break
            db.query(Job).filter(Job.id.in_(ids)).delete(synchronize_session=False)
            db.commit()
            pruned_jobs += len(ids)
            if len(ids) < batch:
                break

    # 5. Prune Old Audit Logs (if retention > 0)
    if settings.AUDIT_LOG_RETENTION_DAYS > 0:
        audit_cutoff = now - timedelta(days=settings.AUDIT_LOG_RETENTION_DAYS)
        while True:
            subq = db.query(AuditLog.id).filter(AuditLog.created_at < audit_cutoff).limit(batch).all()
            ids = [r[0] for r in subq]
            if not ids:
                break
            db.query(AuditLog).filter(AuditLog.id.in_(ids)).delete(synchronize_session=False)
            db.commit()
            pruned_audit_logs += len(ids)
            if len(ids) < batch:
                break

    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    total_pruned = pruned_heartbeats + pruned_logs + pruned_executions + pruned_jobs + pruned_audit_logs

    # Record Prometheus metrics
    record_data_pruned("worker_heartbeats", pruned_heartbeats)
    record_data_pruned("job_logs", pruned_logs)
    record_data_pruned("job_executions", pruned_executions)
    record_data_pruned("jobs", pruned_jobs)
    record_data_pruned("audit_logs", pruned_audit_logs)
    record_prune_run()

    summary = {
        "status": "success",
        "total_pruned_rows": total_pruned,
        "pruned_heartbeats": pruned_heartbeats,
        "pruned_job_logs": pruned_logs,
        "pruned_job_executions": pruned_executions,
        "pruned_completed_jobs": pruned_jobs,
        "pruned_audit_logs": pruned_audit_logs,
        "duration_ms": duration_ms,
        "timestamp": now.isoformat(),
    }

    # Record audit log event if rows were deleted or if an administrator triggered this manually
    if total_pruned > 0 or user is not None:
        try:
            log_audit_event(
                db=db,
                action="system.data_prune",
                resource_type="system",
                resource_id="database",
                user=user,
                org_id=None,
                details=summary,
            )
            db.commit()
        except Exception as exc:
            logger.warning("Failed to record audit log for data prune: %s", exc)

    logger.info(
        "Data retention prune finished in %sms: %d rows deleted "
        "(heartbeats: %d, logs: %d, executions: %d, jobs: %d, audit: %d)",
        duration_ms,
        total_pruned,
        pruned_heartbeats,
        pruned_logs,
        pruned_executions,
        pruned_jobs,
        pruned_audit_logs,
    )

    return summary
