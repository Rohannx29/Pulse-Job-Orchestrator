from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import Job, JobStatus, Organization, Project, Queue, User, Worker
from app.schemas import JobOut, WorkerOut

router = APIRouter(tags=["workers"])


@router.get("/workers", response_model=list[WorkerOut])
def list_workers(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return db.query(Worker).order_by(Worker.hostname).all()


@router.get("/workers/{worker_id}/jobs", response_model=list[JobOut])
def worker_assigned_jobs(worker_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Jobs currently claimed by or running on this worker, scoped to the
    caller's own organization. Workers are shared global infrastructure
    (see docs/design-decisions.md) — any worker may pick up any org's
    job — but the job details returned here stay tenant-scoped like every
    other job-data endpoint."""
    return (
        db.query(Job)
        .join(Queue, Job.queue_id == Queue.id)
        .join(Project, Queue.project_id == Project.id)
        .join(Organization, Project.org_id == Organization.id)
        .filter(
            Job.claimed_by_worker_id == worker_id,
            Job.status.in_([JobStatus.CLAIMED, JobStatus.RUNNING]),
            Organization.owner_id == user.id,
        )
        .all()
    )
