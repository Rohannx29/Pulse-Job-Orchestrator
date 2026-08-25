import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_user
from app.dispatch import dispatch_job
from app.models import Job, JobStatus, Organization, Project, Queue, User
from app.schemas import JobBatchCreate, JobCreate, JobDetailOut, JobOut

router = APIRouter(tags=["jobs"])


def _get_owned_queue(db: Session, project_id: str, queue_id: str, user: User) -> Queue:
    queue = (
        db.query(Queue)
        .join(Project, Queue.project_id == Project.id)
        .join(Organization, Project.org_id == Organization.id)
        .filter(Queue.id == queue_id, Project.id == project_id, Organization.owner_id == user.id)
        .first()
    )
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")
    return queue


def _get_owned_job(db: Session, job_id: str, user: User) -> Job:
    job = (
        db.query(Job)
        .join(Queue, Job.queue_id == Queue.id)
        .join(Project, Queue.project_id == Project.id)
        .join(Organization, Project.org_id == Organization.id)
        .filter(Job.id == job_id, Organization.owner_id == user.id)
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _dispatch(job: Job):
    dispatch_job(job)


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

    if payload.idempotency_key:
        existing = (
            db.query(Job)
            .filter(Job.queue_id == queue.id, Job.idempotency_key == payload.idempotency_key)
            .first()
        )
        if existing:
            response.status_code = 200
            return existing

    dependency: Job | None = None
    if payload.depends_on_job_id:
        dependency = _get_owned_job(db, payload.depends_on_job_id, user)
        # Note: a dependency cycle is structurally impossible here — a job
        # can only depend on an already-existing job, and depends_on_job_id
        # is set once at creation and never updated, so the dependency
        # graph can only ever point "backward" in time.

    job_data = payload.model_dump()
    # Queue.priority is the default; an explicit Job.priority overrides it.
    # Materialized onto the Job row at creation time (rather than resolved
    # at dispatch/read time) so routing and display never need a Queue
    # join just to know a job's effective priority.
    job_data["priority"] = payload.priority if payload.priority is not None else queue.priority
    job = Job(queue_id=queue.id, **job_data)
    if dependency is not None and dependency.status != JobStatus.COMPLETED:
        # Held back until the dependency finishes — see tasks.py, which
        # dispatches BLOCKED dependents once their parent completes (or
        # cascades them to dead_letter if the parent does instead).
        job.status = JobStatus.BLOCKED
        db.add(job)
        try:
            db.flush()
        except IntegrityError:
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
        db.commit()
        db.refresh(job)
        return job

    db.add(job)
    try:
        db.flush()
    except IntegrityError:
        # Race: another request with the same idempotency_key committed
        # between our check above and this flush. Return that one instead
        # of erroring — that's the whole point of an idempotency key.
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
    # Commit BEFORE dispatching to Celery — otherwise a worker could in
    # theory pop the task and query for this job before its row is even
    # visible in Postgres. Small window, real bug if it hit.
    db.commit()
    _dispatch(job)
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
        item_data = item.model_dump()
        item_data["priority"] = item.priority if item.priority is not None else queue.priority
        job = Job(queue_id=queue.id, batch_id=batch_id, **item_data)
        db.add(job)
        jobs.append(job)
    db.flush()
    db.commit()  # all rows visible before any of them can be dispatched
    for job in jobs:
        _dispatch(job)
    db.commit()
    for job in jobs:
        db.refresh(job)
    return jobs


@router.get("/projects/{project_id}/queues/{queue_id}/jobs", response_model=list[JobOut])
def list_jobs(
    project_id: str,
    queue_id: str,
    status_filter: JobStatus | None = Query(default=None, alias="status"),
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    queue = _get_owned_queue(db, project_id, queue_id, user)
    q = db.query(Job).filter(Job.queue_id == queue.id)
    if status_filter:
        q = q.filter(Job.status == status_filter)
    return q.order_by(Job.created_at.desc()).offset(skip).limit(min(limit, 200)).all()


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
def get_job(job_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    job = (
        db.query(Job)
        .options(joinedload(Job.executions))
        .filter(Job.id == job_id)
        .first()
    )
    # ownership re-check (joinedload query above doesn't filter by org)
    _get_owned_job(db, job_id, user)
    return job


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Manually requeue a failed or dead-lettered job, resetting its retry count."""
    job = _get_owned_job(db, job_id, user)
    if job.status not in (JobStatus.FAILED, JobStatus.DEAD_LETTER):
        raise HTTPException(status_code=409, detail="Only failed or dead-lettered jobs can be manually retried")

    # DeadLetterJob represents CURRENT dead-letter state, not a historical
    # log (JobExecution is already the audit trail for that). Without this,
    # a job retried from the DLQ that fails again a second time would hit
    # DeadLetterJob.job_id's unique constraint when tasks.py tries to
    # insert a new row for an already-existing one.
    if job.dead_letter_entry is not None:
        db.delete(job.dead_letter_entry)

    job.current_retry_count = 0
    job.run_at = None
    db.commit()
    _dispatch(job)
    db.commit()
    db.refresh(job)
    return job
