from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.concurrency import current_usage
from app.database import get_db
from app.deps import get_current_user, user_can_access_org
from app.models import Job, JobExecution, JobStatus, Project, Queue, RetryPolicy, User
from app.schemas import QueueCreate, QueueOut, QueueStatsOut, QueueUpdate, RetryPolicyCreate, RetryPolicyOut
from app.timeutils import utcnow

router = APIRouter(prefix="/projects/{project_id}/queues", tags=["queues"])


def _get_project(db: Session, project_id: str, user: User) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project or not user_can_access_org(db, user, project.org_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return project



def _get_queue(db: Session, project_id: str, queue_id: str, user: User) -> Queue:
    _get_project(db, project_id, user)
    queue = db.query(Queue).filter(Queue.id == queue_id, Queue.project_id == project_id).first()
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")
    return queue


@router.post("", response_model=QueueOut, status_code=201)
def create_queue(project_id: str, payload: QueueCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _get_project(db, project_id, user)
    queue = Queue(project_id=project_id, **payload.model_dump())
    db.add(queue)
    db.commit()
    db.refresh(queue)
    return queue


@router.get("", response_model=list[QueueOut])
def list_queues(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _get_project(db, project_id, user)
    return db.query(Queue).filter(Queue.project_id == project_id).all()


from app.audit import log_audit_event


@router.patch("/{queue_id}", response_model=QueueOut)
def update_queue(project_id: str, queue_id: str, payload: QueueUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    queue = _get_queue(db, project_id, queue_id, user)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(queue, field, value)
    log_audit_event(
        db=db,
        action="queue.update",
        resource_type="queue",
        resource_id=queue.id,
        user=user,
        org_id=queue.project.org_id,
        details={"queue_name": queue.name, "changes": changes},
    )
    from app.outbox import record_event_outbox
    record_event_outbox(
        db,
        queue.project.org_id,
        "queue.updated",
        {
            "queue_id": queue.id,
            "concurrency_limit": queue.concurrency_limit,
            "priority": queue.priority,
        },
    )
    db.commit()
    db.refresh(queue)
    return queue


@router.post("/{queue_id}/pause", response_model=QueueOut)
def pause_queue(project_id: str, queue_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    queue = _get_queue(db, project_id, queue_id, user)
    queue.is_paused = True
    log_audit_event(
        db=db,
        action="queue.pause",
        resource_type="queue",
        resource_id=queue.id,
        user=user,
        org_id=queue.project.org_id,
        details={"queue_name": queue.name},
    )
    from app.outbox import record_event_outbox
    record_event_outbox(
        db,
        queue.project.org_id,
        "queue.paused",
        {
            "queue_id": queue.id,
            "is_paused": True,
        },
    )
    db.commit()
    db.refresh(queue)
    return queue


@router.post("/{queue_id}/resume", response_model=QueueOut)
def resume_queue(project_id: str, queue_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    queue = _get_queue(db, project_id, queue_id, user)
    queue.is_paused = False
    log_audit_event(
        db=db,
        action="queue.resume",
        resource_type="queue",
        resource_id=queue.id,
        user=user,
        org_id=queue.project.org_id,
        details={"queue_name": queue.name},
    )
    from app.outbox import record_event_outbox
    record_event_outbox(
        db,
        queue.project.org_id,
        "queue.resumed",
        {
            "queue_id": queue.id,
            "is_paused": False,
        },
    )
    db.commit()
    db.refresh(queue)
    return queue



@router.post("/{queue_id}/retry-policies", response_model=RetryPolicyOut, status_code=201)
def create_retry_policy(project_id: str, queue_id: str, payload: RetryPolicyCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _get_queue(db, project_id, queue_id, user)
    policy = RetryPolicy(queue_id=queue_id, **payload.model_dump())
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


@router.get("/{queue_id}/retry-policies", response_model=list[RetryPolicyOut])
def list_retry_policies(project_id: str, queue_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _get_queue(db, project_id, queue_id, user)
    return db.query(RetryPolicy).filter(RetryPolicy.queue_id == queue_id).all()


@router.get("/{queue_id}/stats", response_model=QueueStatsOut)
def get_queue_stats(project_id: str, queue_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Operational stats for one queue: status breakdown, success rate,
    average execution time, and rolling throughput — the numbers an
    operator actually needs to answer "is this queue healthy right now",
    not just its static configuration."""
    queue = _get_queue(db, project_id, queue_id, user)

    status_rows = (
        db.query(Job.status, func.count(Job.id))
        .filter(Job.queue_id == queue.id)
        .group_by(Job.status)
        .all()
    )
    counts_by_status = {status.value: count for status, count in status_rows}
    total_jobs = sum(counts_by_status.values())

    completed = counts_by_status.get(JobStatus.COMPLETED.value, 0)
    dead_lettered = counts_by_status.get(JobStatus.DEAD_LETTER.value, 0)
    terminal = completed + dead_lettered
    success_rate = (completed / terminal) if terminal > 0 else None

    avg_seconds = (
        db.query(func.avg(func.extract("epoch", JobExecution.finished_at - JobExecution.started_at)))
        .join(Job, JobExecution.job_id == Job.id)
        .filter(Job.queue_id == queue.id, JobExecution.status == JobStatus.COMPLETED)
        .scalar()
    )

    one_hour_ago = utcnow() - timedelta(hours=1)
    throughput = (
        db.query(func.count(Job.id))
        .filter(Job.queue_id == queue.id, Job.status == JobStatus.COMPLETED, Job.updated_at >= one_hour_ago)
        .scalar()
    )

    return QueueStatsOut(
        queue_id=queue.id,
        counts_by_status=counts_by_status,
        total_jobs=total_jobs,
        success_rate=success_rate,
        avg_execution_seconds=float(avg_seconds) if avg_seconds is not None else None,
        throughput_last_hour=throughput or 0,
        current_concurrency_usage=current_usage(queue.id),
        concurrency_limit=queue.concurrency_limit,
    )
