from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, get_user_org
from app.models import DeadLetterJob, Job, Project, Queue, User, UserRole
from app.schemas import DeadLetterJobOut

router = APIRouter(tags=["dead-letter-queue"])


@router.get("/dead-letter-jobs", response_model=list[DeadLetterJobOut])
def list_dead_letter_jobs(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = (
        db.query(DeadLetterJob)
        .join(Job, DeadLetterJob.job_id == Job.id)
        .join(Queue, Job.queue_id == Queue.id)
        .join(Project, Queue.project_id == Project.id)
    )
    if user.role != UserRole.SYSTEM_ADMIN:
        org = get_user_org(db, user)
        q = q.filter(Project.org_id == org.id)
    return q.order_by(DeadLetterJob.failed_at.desc()).all()
