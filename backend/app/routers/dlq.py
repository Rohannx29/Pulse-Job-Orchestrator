from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import DeadLetterJob, Job, Organization, Project, Queue, User
from app.schemas import DeadLetterJobOut

router = APIRouter(tags=["dead-letter-queue"])


@router.get("/dead-letter-jobs", response_model=list[DeadLetterJobOut])
def list_dead_letter_jobs(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return (
        db.query(DeadLetterJob)
        .join(Job, DeadLetterJob.job_id == Job.id)
        .join(Queue, Job.queue_id == Queue.id)
        .join(Project, Queue.project_id == Project.id)
        .join(Organization, Project.org_id == Organization.id)
        .filter(Organization.owner_id == user.id)
        .order_by(DeadLetterJob.failed_at.desc())
        .all()
    )
