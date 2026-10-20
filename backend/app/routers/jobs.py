import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.database import get_db
from app.deps import get_current_user, user_can_access_org
from app.dispatch import dispatch_job
from app.models import Job, JobExecution, JobStatus, Organization, Project, Queue, RetryPolicy, User
from app.schemas import DAGGraphOut, JobBatchCreate, JobCreate, JobDetailOut, JobOut

router = APIRouter(tags=["jobs"])


def _get_owned_queue(db: Session, project_id: str, queue_id: str, user: User) -> Queue:
    queue = (
        db.query(Queue)
        .join(Project, Queue.project_id == Project.id)
        .filter(Queue.id == queue_id, Project.id == project_id)
        .first()
    )
    if not queue or not user_can_access_org(db, user, queue.project.org_id):
        raise HTTPException(status_code=404, detail="Queue not found")
    return queue


def _get_owned_job(db: Session, job_id: str, user: User) -> Job:
    job = (
        db.query(Job)
        .join(Queue, Job.queue_id == Queue.id)
        .join(Project, Queue.project_id == Project.id)
        .filter(Job.id == job_id)
        .first()
    )
    if not job or not user_can_access_org(db, user, job.queue.project.org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return job




@router.post("/projects/{project_id}/queues/{queue_id}/jobs", response_model=JobOut, status_code=201)
def create_job(
    project_id: str,
    queue_id: str,
    payload: JobCreate,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    queue = _get_owned_queue(db, project_id, queue_id, user)
    if queue.is_paused:
        raise HTTPException(status_code=409, detail="Queue is paused")

    # Tenant-scoped job creation rate limiting (120 submissions/min per organization)
    from app.rate_limiter import check_rate_limit
    org_id = queue.project.org_id
    allowed, remaining, retry_after = check_rate_limit(
        f"pulse:ratelimit:org:{org_id}:job_create",
        limit=120,
        window_seconds=60,
    )
    response.headers["X-RateLimit-Limit"] = "120"
    response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
    response.headers["X-RateLimit-Reset"] = str(retry_after)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Job submission rate limit exceeded for this organization. Maximum 120 jobs per minute. Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    if payload.idempotency_key:
        existing = (
            db.query(Job)
            .filter(Job.queue_id == queue.id, Job.idempotency_key == payload.idempotency_key)
            .first()
        )
        if existing:
            response.status_code = 200
            return existing

    if payload.retry_policy_id:
        policy = db.query(RetryPolicy).filter(
            RetryPolicy.id == payload.retry_policy_id,
            RetryPolicy.queue_id == queue.id,
        ).first()
        if not policy:
            raise HTTPException(
                status_code=400,
                detail=f"Retry policy '{payload.retry_policy_id}' not found or does not belong to this queue",
            )

    # Collect all parent dependency IDs (both legacy single field and multi-parent array)
    raw_parent_ids = (payload.depends_on_job_ids or []) + ([payload.depends_on_job_id] if payload.depends_on_job_id else [])
    parent_job_ids = list(dict.fromkeys(pid for pid in raw_parent_ids if pid))

    for pid in parent_job_ids:
        _get_owned_job(db, pid, user)

    job_data = payload.model_dump(exclude={"depends_on_job_ids"})
    # Queue.priority is the default; an explicit Job.priority overrides it.
    job_data["priority"] = payload.priority if payload.priority is not None else queue.priority
    job = Job(queue_id=queue.id, **job_data)
    db.add(job)

    # Link DAG dependencies and evaluate initial state (BLOCKED, DEAD_LETTER, or QUEUED)
    if parent_job_ids:
        try:
            from app.dag import link_dag_dependencies
            initial_status = link_dag_dependencies(db, job, parent_job_ids)
            job.status = initial_status
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc))

    try:
        db.flush()
    except IntegrityError:
        # Race: another request with the same idempotency_key committed
        # between our check above and this flush. Return that one instead
        # of erroring.
        db.rollback()
        existing = (
            db.query(Job)
            .filter(Job.queue_id == queue.id, Job.idempotency_key == payload.idempotency_key)
            .first()
        )
        if existing:
            response.status_code = 200
            return existing
        raise

    # Only stage dispatch outbox if runnable (QUEUED or SCHEDULED), not BLOCKED by dependencies or DEAD_LETTER
    if job.status not in (JobStatus.BLOCKED, JobStatus.DEAD_LETTER):
        dispatch_job(job, db=db)

    # Commit state change and outbox records atomically at the outer transaction boundary
    db.commit()
    db.refresh(job)

    return job


@router.post("/projects/{project_id}/queues/{queue_id}/jobs/batch", response_model=list[JobOut], status_code=201)
def create_jobs_batch(
    project_id: str,
    queue_id: str,
    payload: JobBatchCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    queue = _get_owned_queue(db, project_id, queue_id, user)
    if queue.is_paused:
        raise HTTPException(status_code=409, detail="Queue is paused")

    batch_id = str(uuid.uuid4())
    jobs = []
    for item in payload.jobs:
        if item.retry_policy_id:
            policy = db.query(RetryPolicy).filter(
                RetryPolicy.id == item.retry_policy_id,
                RetryPolicy.queue_id == queue.id,
            ).first()
            if not policy:
                raise HTTPException(
                    status_code=400,
                    detail=f"Retry policy '{item.retry_policy_id}' not found or does not belong to this queue",
                )
        item_data = item.model_dump()
        item_data["priority"] = item.priority if item.priority is not None else queue.priority
        job = Job(queue_id=queue.id, batch_id=batch_id, **item_data)
        db.add(job)
        jobs.append(job)
        dispatch_job(job, db=db)
    db.commit()  # all jobs and their outbox events committed in a single atomic transaction
    for job in jobs:
        db.refresh(job)
    return jobs


@router.get("/projects/{project_id}/queues/{queue_id}/jobs", response_model=list[JobOut])
def list_jobs(
    project_id: str,
    queue_id: str,
    response: Response,
    status_filter: JobStatus | None = Query(default=None, alias="status"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, description="Cursor for timestamp-based keyset pagination"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    queue = _get_owned_queue(db, project_id, queue_id, user)
    q = db.query(Job).filter(Job.queue_id == queue.id)
    if status_filter:
        q = q.filter(Job.status == status_filter)
    if cursor:
        try:
            from datetime import datetime
            cursor_dt = datetime.fromisoformat(cursor)
            q = q.filter(Job.created_at < cursor_dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor format; ISO 8601 datetime expected")
    
    fetch_limit = min(limit, 100)
    items = q.order_by(Job.created_at.desc()).offset(skip).limit(fetch_limit + 1).all()
    if len(items) > fetch_limit:
        has_more = True
        items = items[:fetch_limit]
        next_cursor = items[-1].created_at.isoformat()
    else:
        has_more = False
        next_cursor = None

    response.headers["X-Has-More"] = "true" if has_more else "false"
    if next_cursor:
        response.headers["X-Next-Cursor"] = next_cursor

    return items


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
def get_job(job_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    job = (
        db.query(Job)
        .options(
            selectinload(Job.executions).selectinload(JobExecution.logs),
            joinedload(Job.queue).joinedload(Queue.project),
        )
        .filter(Job.id == job_id)
        .first()
    )
    if not job or not user_can_access_org(db, user, job.queue.project.org_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Manually requeue a failed or dead-lettered job, resetting its retry count."""
    job = _get_owned_job(db, job_id, user)
    if job.status not in (JobStatus.FAILED, JobStatus.DEAD_LETTER):
        raise HTTPException(status_code=409, detail="Only failed or dead-lettered jobs can be manually retried")

    # Tenant-scoped retry rate limiting (30 retries/min per organization)
    from app.rate_limiter import check_rate_limit
    allowed, remaining, retry_after = check_rate_limit(
        f"pulse:ratelimit:org:{job.queue.project.org_id}:job_retry",
        limit=30,
        window_seconds=60,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Job retry rate limit exceeded for this organization. Maximum 30 retries per minute. Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    # DeadLetterJob represents CURRENT dead-letter state, not a historical
    # log (JobExecution is already the audit trail for that). Without this,
    # a job retried from the DLQ that fails again a second time would hit
    # DeadLetterJob.job_id's unique constraint when tasks.py tries to
    # insert a new row for an already-existing one.
    if job.dead_letter_entry is not None:
        db.delete(job.dead_letter_entry)

    job.current_retry_count = 0
    job.dispatch_generation = (getattr(job, "dispatch_generation", None) or 1) + 1
    job.run_at = None
    from app.audit import log_audit_event
    log_audit_event(
        db=db,
        action="job.retry",
        resource_type="job",
        resource_id=job.id,
        user=user,
        org_id=job.queue.project.org_id,
        details={
            "job_name": job.name,
            "queue_id": job.queue_id,
            "dispatch_generation": job.dispatch_generation,
        },
    )
    dispatch_job(job, db=db)
    db.commit()
    db.refresh(job)
    return job


@router.get("/jobs/{job_id}/dag", response_model=DAGGraphOut)
def get_job_dag(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Retrieve the connected DAG topology (ancestors and descendants) for a specific job."""
    _get_owned_job(db, job_id, user)
    from app.dag import build_dag_graph
    return build_dag_graph(db, root_job_id=job_id)


@router.get("/projects/{project_id}/dag", response_model=DAGGraphOut)
def get_project_dag(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Retrieve all DAG workflow jobs and dependency edges for an entire project."""
    project = db.get(Project, project_id)
    if not project or not user_can_access_org(db, user, project.org_id):
        raise HTTPException(status_code=404, detail="Project not found")
    from app.dag import build_dag_graph
    return build_dag_graph(db, project_id=project_id)

