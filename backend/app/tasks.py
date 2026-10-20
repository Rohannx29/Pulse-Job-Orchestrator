import json
import random
import time
from datetime import datetime, timedelta
from typing import Any

import redis
try:
    from celery.utils.log import get_task_logger
    logger = get_task_logger(__name__)
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
try:
    from croniter import croniter
except ImportError:
    croniter = None

from app.celery_app import celery_app
from app.concurrency import acquire_queue_slot, release_queue_slot
from app.timeutils import utcnow
from app.config import settings
from app.database import SessionLocal
from app.dispatch import dispatch_job
import uuid
from app.events import get_job_org_id, publish_event
from app.models import (
    DeadLetterJob,
    Job,
    JobExecution,
    JobLog,
    JobSchedule,
    JobStatus,
    LogLevel,
    OutboxEvent,
    Queue,
    RetryPolicy,
    RetryStrategy,
    ScheduleOccurrence,
    Worker,
    WorkerHeartbeat,
    WorkerStatus,
)
from app.outbox import record_event_outbox
from app.worker_registry import get_or_create_worker
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
    surface, so this executes through defense-in-depth execute_safe_http_request:
    validates against private/internal/metadata address space before connecting,
    restricts to a safe method allowlist, caps the timeout regardless of what's
    requested, strictly disables unvalidated redirects (follow_redirects=False),
    and enforces bounded response body read limits."""
    from app.url_safety import execute_safe_http_request

    if "url" not in payload:
        raise ValueError("http_request job payload requires a 'url' field")
    url = payload["url"]

    method = payload.get("method", "GET").upper()
    if method not in ALLOWED_HTTP_METHODS:
        raise ValueError(f"HTTP method {method!r} not allowed. Allowed: {sorted(ALLOWED_HTTP_METHODS)}")

    timeout = min(float(payload.get("timeout_seconds", 10)), MAX_HTTP_TIMEOUT_SECONDS)

    body_obj = payload.get("body")
    json_data = body_obj if isinstance(body_obj, (dict, list)) else None
    content = body_obj.encode("utf-8") if isinstance(body_obj, str) else None

    status_code, content_bytes, _ = execute_safe_http_request(
        method=method,
        url=url,
        headers=payload.get("headers", {}),
        json_data=json_data,
        content=content,
        timeout_seconds=timeout,
        max_bytes=MAX_HTTP_RESPONSE_BYTES,
    )

    if status_code >= 400:
        raise RuntimeError(f"HTTP {status_code} from {url}: {content_bytes[:300]!r}")

    try:
        body = json.loads(content_bytes)
    except ValueError:
        body = content_bytes[:2000].decode("utf-8", errors="replace")
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
    """When a job finishes successfully, unblock downstream DAG dependents
    whose full set of prerequisites have all finished."""
    from app.dag import release_dag_dependents
    return release_dag_dependents(db, completed_job, dispatcher=dispatch_job)


def _cascade_fail_dependents(db, dead_job: Job, reason: str):
    """When a job permanently fails (DLQ), recursively dead-letter all
    downstream DAG dependents that rely on it."""
    from app.dag import cascade_fail_dag_dependents
    return cascade_fail_dag_dependents(db, dead_job, reason)


@celery_app.task(bind=True, name="app.tasks.execute_job")
def execute_job(self, job_id: str, **kwargs):
    db = SessionLocal()
    try:
        job: Job | None = db.get(Job, job_id)
        if job is None:
            logger.warning("Job %s no longer exists, skipping", job_id)
            return

        # Durable state guard: a task cannot execute while persisted as BLOCKED or terminal
        if job.status == JobStatus.BLOCKED:
            logger.warning("Job %s is still BLOCKED in database; refusing premature execution", job_id)
            return

        if job.status in (JobStatus.COMPLETED, JobStatus.DEAD_LETTER):
            logger.info("Job %s already in terminal state %s; skipping execution", job_id, job.status.value)
            return

        # Fencing guard: if dispatch_generation was provided in task kwargs,
        # ensure it matches the current persisted dispatch_generation.
        # Stale dispatches from previous generations are dropped.
        dispatch_gen = kwargs.get("dispatch_generation")
        if dispatch_gen is not None and getattr(job, "dispatch_generation", None) is not None:
            if job.dispatch_generation != dispatch_gen:
                logger.warning(
                    "Stale task execution for job %s: payload generation %s != DB generation %s. Dropping.",
                    job.id,
                    dispatch_gen,
                    job.dispatch_generation,
                )
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

        org_id = get_job_org_id(db, job)
        if org_id:
            record_event_outbox(db, org_id, "job.claimed", {
                "job_id": job.id,
                "queue_id": queue.id,
                "worker_id": worker.id,
                "worker_hostname": self.request.hostname,
                "status": JobStatus.CLAIMED.value,
            })
        db.commit()

        try:
            _execute_job_inner(self, db, job, worker, org_id)
        finally:
            release_queue_slot(queue.id, lease_token)
    finally:
        db.close()


def _execute_job_inner(self, db, job: Job, worker: Worker, org_id: str | None = None):
    try:
        if org_id is None:
            org_id = get_job_org_id(db, job)

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

        if org_id:
            record_event_outbox(db, org_id, "job.running", {
                "job_id": job.id,
                "queue_id": job.queue_id,
                "worker_id": worker.id,
                "attempt_number": attempt_number,
                "status": JobStatus.RUNNING.value,
            })
        db.commit()

        result = _run_handler(job.payload)

        execution.status = JobStatus.COMPLETED
        execution.finished_at = utcnow()
        execution.result = result
        _log(db, execution, LogLevel.INFO, "Completed successfully")

        job.status = JobStatus.COMPLETED
        if org_id:
            record_event_outbox(db, org_id, "job.completed", {
                "job_id": job.id,
                "queue_id": job.queue_id,
                "status": JobStatus.COMPLETED.value,
                "result": result,
            })
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
            job.dispatch_generation = (getattr(job, "dispatch_generation", None) or 1) + 1
            job.status = JobStatus.QUEUED
            delay = _compute_backoff_seconds(policy, job.current_retry_count)
            logger.info("Retrying job %s in %ss (attempt %s/%s, generation %s)", job.id, delay, job.current_retry_count, job.max_retries, job.dispatch_generation)
            if org_id:
                record_event_outbox(db, org_id, "job.failed", {
                    "job_id": job.id,
                    "queue_id": job.queue_id,
                    "attempt_number": job.current_retry_count,
                    "max_retries": job.max_retries,
                    "retry_delay_seconds": delay,
                    "error_message": str(exc),
                    "status": JobStatus.QUEUED.value,
                })
            db.commit()
            raise self.retry(exc=exc, countdown=delay, max_retries=job.max_retries, kwargs={"dispatch_generation": job.dispatch_generation})

        # Retries exhausted -> Dead Letter Queue
        job.status = JobStatus.DEAD_LETTER
        job.dispatch_generation = (getattr(job, "dispatch_generation", None) or 1) + 1
        db.add(
            DeadLetterJob(
                job_id=job.id,
                reason=str(exc),
                original_payload=job.payload,
                retry_count_at_failure=job.current_retry_count,
            )
        )
        if org_id:
            record_event_outbox(db, org_id, "job.dead_letter", {
                "job_id": job.id,
                "queue_id": job.queue_id,
                "reason": str(exc),
                "status": JobStatus.DEAD_LETTER.value,
            })
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
        stale_query = db.query(Worker)
        if seen_hostnames:
            stale_query = stale_query.filter(Worker.hostname.notin_(seen_hostnames))
        for worker in stale_query.all():
            if worker.last_heartbeat_at and worker.last_heartbeat_at < stale_cutoff:
                worker.status = WorkerStatus.OFFLINE

        db.commit()
    finally:
        db.close()


LUA_SAFE_RELEASE_LOCK = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


@celery_app.task(name="app.tasks.dispatch_due_schedules")
def dispatch_due_schedules():
    """Runs on a Beat interval. Evaluates active JobSchedule cron expressions and
    creates a new Job row (+ dispatches it via Transactional Outbox) when due.

    Hardened guarantees:
    - Owned Redis lock with random UUID token and safe atomic Lua compare-and-delete.
    - Unique ScheduleOccurrence record keyed by (schedule_id, scheduled_fire_time) prevents
      duplicate jobs across overlapping Beat runs or worker crashes.
    - Fails closed per-schedule on malformed cron syntax (disables schedule + logs audit event),
      allowing healthy schedules to continue processing.
    """
    owner_token = str(uuid.uuid4())
    have_lock = False
    try:
        have_lock = bool(_redis.set("pulse:lock:dispatch_due_schedules", owner_token, nx=True, ex=30))
    except Exception as exc:
        logger.warning("Redis connection error acquiring schedule lock: %s", exc)
        have_lock = False

    if not have_lock:
        logger.info("dispatch_due_schedules already running or lock held, skipping this tick")
        return

    db = SessionLocal()
    try:
        now = utcnow()
        schedules = db.query(JobSchedule).filter(JobSchedule.is_active.is_(True)).all()
        for schedule in schedules:
            try:
                # 1. Fail closed on malformed legacy schedules
                if not croniter or not croniter.is_valid(schedule.cron_expression):
                    logger.error(
                        "Schedule %s has invalid cron expression '%s'. Disabling schedule.",
                        schedule.id,
                        schedule.cron_expression,
                    )
                    schedule.is_active = False
                    from app.audit import log_audit_event
                    log_audit_event(
                        db=db,
                        action="schedule.invalid_disabled",
                        resource_type="job_schedule",
                        resource_id=schedule.id,
                        user=None,
                        org_id=(
                            schedule.queue.project.org_id
                            if (schedule.queue and schedule.queue.project)
                            else None
                        ),
                        details={
                            "cron_expression": schedule.cron_expression,
                            "error": "Invalid cron syntax",
                        },
                    )
                    db.commit()
                    continue

                # 2. Base reference time and next scheduled fire time in UTC
                base = schedule.last_dispatched_at or (now - timedelta(minutes=5))
                cron = croniter(schedule.cron_expression, base)
                next_fire = cron.get_next(datetime)

                if next_fire > now:
                    continue  # Not due yet

                # 3. Persist ScheduleOccurrence to guarantee exactly-once execution per fire time
                occurrence = ScheduleOccurrence(
                    schedule_id=schedule.id,
                    scheduled_fire_time=next_fire,
                    dispatched_at=now,
                )
                db.add(occurrence)
                try:
                    db.flush()
                except Exception:
                    # Duplicate occurrence for this fire time already dispatched
                    db.rollback()
                    logger.info(
                        "Schedule %s fire time %s already claimed by concurrent runner. Skipping.",
                        schedule.id,
                        next_fire,
                    )
                    continue

                # 4. Create Job row atomically linked to the occurrence
                job = Job(
                    queue_id=schedule.queue_id,
                    schedule_id=schedule.id,
                    name=f"{schedule.name} ({next_fire.isoformat()})",
                    payload=dict(schedule.payload_template),
                    status=JobStatus.QUEUED,
                    priority=schedule.queue.priority if schedule.queue else 5,
                )
                db.add(job)
                db.flush()

                occurrence.dispatched_job_id = job.id
                schedule.last_dispatched_at = now

                # 5. Dispatch via Transactional Outbox in the SAME transaction
                dispatch_job(job, db=db)
                db.commit()
                logger.info(
                    "Dispatched scheduled job %s from schedule %s for fire time %s",
                    job.id,
                    schedule.id,
                    next_fire,
                )

            except Exception as sched_exc:
                logger.error("Error processing schedule %s: %s", schedule.id, sched_exc)
                db.rollback()
                continue
    finally:
        db.close()
        try:
            _redis.eval(LUA_SAFE_RELEASE_LOCK, 1, "pulse:lock:dispatch_due_schedules", owner_token)
        except Exception:
            pass


@celery_app.task(name="app.tasks.process_outbox_events")
def process_outbox_events():
    """Runs on a Beat interval. Reconciles stranded jobs and publishes pending Transactional Outbox events."""
    from app.outbox import publish_pending_outbox_events, reconcile_stranded_jobs

    db = SessionLocal()
    try:
        reconciled = reconcile_stranded_jobs(db, max_age_seconds=30, limit=50)
        if reconciled > 0:
            logger.info("Outbox publisher reconciled %d stranded jobs", reconciled)
        dispatched = publish_pending_outbox_events(db, batch_size=50)
        if dispatched > 0:
            logger.info("Outbox publisher dispatched %d pending events", dispatched)
    finally:
        db.close()


@celery_app.task(name="app.tasks.prune_stale_data")
def prune_stale_data():
    """Runs on a Beat interval. Deletes stale execution logs, worker heartbeats,
    and ancient terminal jobs in accordance with retention policy.
    Guarded by Redis lock to ensure single-worker execution."""
    have_lock = _redis.set("pulse:lock:prune_stale_data", "1", nx=True, ex=300)
    if not have_lock:
        logger.info("prune_stale_data already in progress, skipping this tick")
        return {"status": "skipped", "reason": "lock_acquired"}

    from app.retention import run_retention_prune
    db = SessionLocal()
    try:
        return run_retention_prune(db=db)
    finally:
        db.close()
        _redis.delete("pulse:lock:prune_stale_data")


@celery_app.task(bind=True, name="app.tasks.deliver_webhook", max_retries=3)
def deliver_webhook(self, delivery_id: str):
    """Executes outbound webhook delivery for the given WebhookDelivery ID.
    Performs retries with exponential backoff on transient errors (timeouts, 5xx)."""
    from app.webhooks import execute_delivery

    db = SessionLocal()
    try:
        success, status_code, error = execute_delivery(db, delivery_id)
        if not success:
            # Retry on network/DNS error (status_code is None) or 5xx server errors
            # Do NOT retry on 4xx client errors or SSRF security blocks
            is_ssrf = "SSRF validation blocked" in (error or "")
            is_transient = (status_code is None and not is_ssrf) or (status_code is not None and status_code >= 500)
            if is_transient and self.request.retries < self.max_retries:
                # Exponential backoff: 5s, 25s, 125s
                countdown = 5 * (5 ** self.request.retries)
                logger.warning(
                    "Webhook delivery %s failed (status=%s, err=%s). Retrying in %ss (attempt %d/%d)",
                    delivery_id,
                    status_code,
                    error,
                    countdown,
                    self.request.retries + 1,
                    self.max_retries,
                )
                raise self.retry(countdown=countdown)
        return {"delivery_id": delivery_id, "success": success, "status_code": status_code, "error": error}
    finally:
        db.close()

