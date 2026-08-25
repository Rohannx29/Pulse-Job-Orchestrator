import json
import random
import time
from datetime import datetime, timedelta
from typing import Any

import redis
from celery.utils.log import get_task_logger
from croniter import croniter

from app.celery_app import celery_app
from app.concurrency import acquire_queue_slot, release_queue_slot
from app.timeutils import utcnow
from app.config import settings
from app.database import SessionLocal
from app.dispatch import dispatch_job
from app.models import (
    DeadLetterJob,
    Job,
    JobExecution,
    JobLog,
    JobSchedule,
    JobStatus,
    LogLevel,
    Queue,
    RetryPolicy,
    RetryStrategy,
    Worker,
    WorkerHeartbeat,
    WorkerStatus,
)
from app.worker_registry import get_or_create_worker
logger = get_task_logger(__name__)
_redis = redis.from_url(settings.REDIS_URL, decode_responses=True)


# ---------------------------------------------------------------------------
# Job type handler registry. A job's payload carries an optional "type" key;
# handlers here do the actual work and either return a JSON-serializable
# result or raise, which the retry/DLQ machinery in execute_job reacts to.
# Adding a new job type never requires touching retry/backoff/DLQ logic.
# ---------------------------------------------------------------------------

def _handle_simulate(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministic fake work, useful for testing retry/backoff/DLQ
    behavior without needing a real downstream dependency."""
    duration = float(payload.get("duration_seconds", 1))
    fail_rate = float(payload.get("fail_rate", 0))
    time.sleep(min(duration, 30))
    if random.random() < fail_rate:
        raise RuntimeError(f"Simulated failure (fail_rate={fail_rate})")
    return {"echo": payload, "processed_at": utcnow().isoformat()}


ALLOWED_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
MAX_HTTP_TIMEOUT_SECONDS = 30
MAX_HTTP_RESPONSE_BYTES = 1_000_000  # 1MB — generous for a typical webhook/API response


def _handle_http_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Real work: make an HTTP call. Non-2xx or a timeout raises, so it
    goes through the same retry/backoff/DLQ path as any other failure.

    A caller-supplied URL making a server-side request is a classic SSRF
    surface, so this validates against private/internal address space
    before connecting (see app/url_safety.py), restricts to a safe method
    allowlist, caps the timeout regardless of what's requested, never
    follows redirects (an unvalidated redirect target would reintroduce
    the exact thing being blocked), and caps how much response body it
    will read into memory."""
    import httpx

    from app.url_safety import validate_public_url

    if "url" not in payload:
        raise ValueError("http_request job payload requires a 'url' field")
    url = payload["url"]
    validate_public_url(url)

    method = payload.get("method", "GET").upper()
    if method not in ALLOWED_HTTP_METHODS:
        raise ValueError(f"HTTP method {method!r} not allowed. Allowed: {sorted(ALLOWED_HTTP_METHODS)}")

    timeout = min(float(payload.get("timeout_seconds", 10)), MAX_HTTP_TIMEOUT_SECONDS)

    with httpx.stream(
        method,
        url,
        json=payload.get("body"),
        headers=payload.get("headers", {}),
        timeout=timeout,
        follow_redirects=False,
    ) as resp:
        chunks = []
        total = 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > MAX_HTTP_RESPONSE_BYTES:
                raise RuntimeError(f"Response from {url} exceeded {MAX_HTTP_RESPONSE_BYTES}-byte limit")
            chunks.append(chunk)
        content = b"".join(chunks)
        status_code = resp.status_code

    if status_code >= 400:
        raise RuntimeError(f"HTTP {status_code} from {url}: {content[:300]!r}")

    try:
        body = json.loads(content)
    except ValueError:
        body = content[:2000].decode("utf-8", errors="replace")
    return {"status_code": status_code, "body": body}


HANDLERS = {
    "simulate": _handle_simulate,
    "http_request": _handle_http_request,
}


def _run_handler(payload: dict[str, Any]) -> dict[str, Any]:
    job_type = payload.get("type", "simulate")
    handler = HANDLERS.get(job_type)
    if handler is None:
        raise ValueError(f"Unknown job type {job_type!r}. Available: {list(HANDLERS)}")
    return handler(payload)


# ---------------------------------------------------------------------------


def _compute_backoff_seconds(policy: RetryPolicy | None, attempt: int, default_base: int = 10) -> int:
    """attempt is 1-indexed (this is the Nth retry)."""
    if policy is None:
        strategy, base = RetryStrategy.EXPONENTIAL, default_base
    else:
        strategy, base = policy.strategy, policy.base_delay_seconds

    if strategy == RetryStrategy.FIXED:
        return base
    if strategy == RetryStrategy.LINEAR:
        return base * attempt
    # exponential
    return base * (2 ** (attempt - 1))


def _log(db, execution: JobExecution, level: LogLevel, message: str):
    db.add(JobLog(job_execution_id=execution.id, level=level, message=message))


def _release_dependents(db, completed_job: Job):
    """A job finishing successfully may unblock others waiting on it."""
    blocked = (
        db.query(Job)
        .filter(Job.depends_on_job_id == completed_job.id, Job.status == JobStatus.BLOCKED)
        .all()
    )
    for dependent in blocked:
        dispatch_job(dependent)
        logger.info("Dependency %s completed, releasing dependent job %s", completed_job.id, dependent.id)
    if blocked:
        db.commit()


def _cascade_fail_dependents(db, dead_job: Job, reason: str):
    """A job that permanently failed (DLQ) can never satisfy jobs waiting
    on it — recursively dead-letter its BLOCKED dependents too, rather
    than leaving them blocked forever with no way to ever run."""
    blocked = (
        db.query(Job)
        .filter(Job.depends_on_job_id == dead_job.id, Job.status == JobStatus.BLOCKED)
        .all()
    )
    for dependent in blocked:
        dependent.status = JobStatus.DEAD_LETTER
        db.add(
            DeadLetterJob(
                job_id=dependent.id,
                reason=f"Upstream dependency {dead_job.id} failed: {reason}",
                original_payload=dependent.payload,
                retry_count_at_failure=0,
            )
        )
        logger.warning("Cascading DLQ from %s to blocked dependent %s", dead_job.id, dependent.id)
        db.commit()
        _cascade_fail_dependents(db, dependent, f"upstream dependency {dead_job.id} failed")


@celery_app.task(bind=True, name="app.tasks.execute_job")
def execute_job(self, job_id: str):
    db = SessionLocal()
    try:
        job: Job | None = db.get(Job, job_id)
        if job is None:
            logger.warning("Job %s no longer exists, skipping", job_id)
            return

        queue: Queue = db.get(Queue, job.queue_id)

        # A queue paused mid-flight should stop jobs already dispatched to
        # Celery from running too, not just block new job creation. Defer
        # rather than fail — this isn't a job failure, so it must not
        # touch current_retry_count.
        if queue.is_paused:
            logger.info("Queue %s is paused, deferring job %s", queue.id, job_id)
            raise self.retry(countdown=5, max_retries=None)

        # Per-queue concurrency limit, enforced via a Redis semaphore since
        # Celery's own --concurrency is per worker process, not per queue.
        lease_token = acquire_queue_slot(queue.id, queue.concurrency_limit)
        if lease_token is None:
            logger.info("Queue %s at concurrency limit (%s), deferring job %s", queue.id, queue.concurrency_limit, job_id)
            raise self.retry(countdown=2, max_retries=None)

        # CLAIMED is a real, queryable state, not just a log line: the
        # worker that will run this job is resolved and recorded on the
        # job (claimed_by_worker_id) before any work starts, so "which
        # worker executed this job" and "what's assigned to this worker
        # right now" are both answerable from the database, not inferred
        # from Celery's own internal state.
        worker = get_or_create_worker(db, self.request.hostname)
        job.status = JobStatus.CLAIMED
        job.claimed_by_worker_id = worker.id
        db.commit()

        try:
            _execute_job_inner(self, db, job, worker)
        finally:
            release_queue_slot(queue.id, lease_token)
    finally:
        db.close()


def _execute_job_inner(self, db, job: Job, worker: Worker):
    try:
        attempt_number = job.current_retry_count + 1
        job.status = JobStatus.RUNNING
        job.celery_task_id = self.request.id
        db.flush()

        execution = JobExecution(
            job_id=job.id,
            worker_id=worker.id,
            attempt_number=attempt_number,
            status=JobStatus.RUNNING,
            started_at=utcnow(),
        )
        db.add(execution)
        db.flush()
        _log(db, execution, LogLevel.INFO, f"Attempt {attempt_number} started on worker {self.request.hostname}")
        db.commit()

        result = _run_handler(job.payload)

        execution.status = JobStatus.COMPLETED
        execution.finished_at = utcnow()
        execution.result = result
        _log(db, execution, LogLevel.INFO, "Completed successfully")

        job.status = JobStatus.COMPLETED
        db.commit()

        _release_dependents(db, job)

    except Exception as exc:  # noqa: BLE001
        db.rollback()
        job = db.get(Job, job.id)
        execution = (
            db.query(JobExecution)
            .filter(JobExecution.job_id == job.id)
            .order_by(JobExecution.started_at.desc())
            .first()
        )
        policy = db.get(RetryPolicy, job.retry_policy_id) if job.retry_policy_id else None

        if execution:
            execution.status = JobStatus.FAILED
            execution.finished_at = utcnow()
            execution.error_message = str(exc)
            _log(db, execution, LogLevel.ERROR, f"Attempt failed: {exc}")

        if job.current_retry_count < job.max_retries:
            job.current_retry_count += 1
            job.status = JobStatus.QUEUED
            db.commit()
            delay = _compute_backoff_seconds(policy, job.current_retry_count)
            logger.info("Retrying job %s in %ss (attempt %s/%s)", job.id, delay, job.current_retry_count, job.max_retries)
            raise self.retry(exc=exc, countdown=delay, max_retries=job.max_retries)

        # Retries exhausted -> Dead Letter Queue
        job.status = JobStatus.DEAD_LETTER
        db.add(
            DeadLetterJob(
                job_id=job.id,
                reason=str(exc),
                original_payload=job.payload,
                retry_count_at_failure=job.current_retry_count,
            )
        )
        db.commit()
        logger.warning("Job %s exhausted retries, moved to DLQ", job.id)

        _cascade_fail_dependents(db, job, str(exc))


@celery_app.task(name="app.tasks.sync_worker_heartbeats")
def sync_worker_heartbeats():
    """Runs on a Beat interval. Uses Celery's control/inspect API to
    discover live workers cluster-wide and persists heartbeat rows so the
    dashboard can show worker health from Postgres."""
    db = SessionLocal()
    try:
        inspect = celery_app.control.inspect(timeout=3)
        stats = inspect.stats() or {}
        active = inspect.active() or {}

        seen_hostnames = set()
        for hostname, _ in stats.items():
            seen_hostnames.add(hostname)
            worker = get_or_create_worker(db, hostname)
            worker.status = WorkerStatus.ONLINE
            worker.last_heartbeat_at = utcnow()
            db.add(
                WorkerHeartbeat(
                    worker_id=worker.id,
                    active_jobs_count=len(active.get(hostname, [])),
                )
            )

        # Mark workers that didn't respond this cycle as offline if stale
        stale_cutoff = utcnow() - timedelta(seconds=60)
        for worker in db.query(Worker).filter(Worker.hostname.notin_(seen_hostnames)).all():
            if worker.last_heartbeat_at and worker.last_heartbeat_at < stale_cutoff:
                worker.status = WorkerStatus.OFFLINE

        db.commit()
    finally:
        db.close()


@celery_app.task(name="app.tasks.dispatch_due_schedules")
def dispatch_due_schedules():
    """Runs on a Beat interval. Evaluates each active JobSchedule's cron
    expression and creates a new Job row (+ dispatches it) whenever a
    scheduled tick has been crossed since the last check.

    Guarded by a short-lived Redis lock: if a run takes longer than the
    30s Beat interval (e.g. many schedules, slow DB), Beat will still fire
    the next tick on schedule regardless of whether the first run
    finished. Without the lock, two overlapping runs could both see the
    same schedule as "due" (neither has updated last_dispatched_at yet)
    and both create a Job for it — a real duplicate-dispatch risk, not a
    hypothetical one, given Beat doesn't wait for task completion."""
    have_lock = _redis.set("pulse:lock:dispatch_due_schedules", "1", nx=True, ex=25)
    if not have_lock:
        logger.info("dispatch_due_schedules already running, skipping this tick")
        return

    db = SessionLocal()
    try:
        now = utcnow()
        schedules = db.query(JobSchedule).filter(JobSchedule.is_active.is_(True)).all()
        for schedule in schedules:
            base = schedule.last_dispatched_at or (now - timedelta(minutes=5))
            cron = croniter(schedule.cron_expression, base)
            next_fire = cron.get_next(datetime)
            if next_fire > now:
                continue  # not due yet

            job = Job(
                queue_id=schedule.queue_id,
                schedule_id=schedule.id,
                name=f"{schedule.name} ({next_fire.isoformat()})",
                payload=dict(schedule.payload_template),
                status=JobStatus.QUEUED,
                priority=schedule.queue.priority,
            )
            db.add(job)
            db.flush()
            schedule.last_dispatched_at = now
            db.commit()  # job row visible before it can possibly be dispatched

            dispatch_job(job)
            db.commit()
            logger.info("Dispatched scheduled job %s from schedule %s", job.id, schedule.id)
    finally:
        db.close()
        _redis.delete("pulse:lock:dispatch_due_schedules")
