from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, user_can_access_org
from app.models import JobSchedule, Organization, Project, Queue, User
from app.routers.jobs import _get_owned_queue
from app.schemas import JobScheduleCreate, JobScheduleOut

router = APIRouter(tags=["schedules"])


def _get_owned_schedule(db: Session, schedule_id: str, user: User) -> JobSchedule:
    schedule = (
        db.query(JobSchedule)
        .join(Queue, JobSchedule.queue_id == Queue.id)
        .join(Project, Queue.project_id == Project.id)
        .filter(JobSchedule.id == schedule_id)
        .first()
    )
    if not schedule or not user_can_access_org(db, user, schedule.queue.project.org_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule



@router.post("/projects/{project_id}/queues/{queue_id}/schedules", response_model=JobScheduleOut, status_code=201)
def create_schedule(
    project_id: str,
    queue_id: str,
    payload: JobScheduleCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    queue = _get_owned_queue(db, project_id, queue_id, user)
    schedule = JobSchedule(queue_id=queue.id, **payload.model_dump())
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


@router.get("/projects/{project_id}/queues/{queue_id}/schedules", response_model=list[JobScheduleOut])
def list_schedules(project_id: str, queue_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    queue = _get_owned_queue(db, project_id, queue_id, user)
    return db.query(JobSchedule).filter(JobSchedule.queue_id == queue.id).all()


@router.post("/schedules/{schedule_id}/toggle", response_model=JobScheduleOut)
def toggle_schedule(schedule_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    schedule = _get_owned_schedule(db, schedule_id, user)
    schedule.is_active = not schedule.is_active
    from app.audit import log_audit_event
    log_audit_event(
        db=db,
        action="schedule.toggle",
        resource_type="schedule",
        resource_id=schedule.id,
        user=user,
        org_id=schedule.queue.project.org_id,
        details={"schedule_name": schedule.name, "is_active": schedule.is_active},
    )
    from app.outbox import record_event_outbox
    record_event_outbox(
        db,
        schedule.queue.project.org_id,
        "schedule.toggled",
        {
            "schedule_id": schedule.id,
            "is_active": schedule.is_active,
        },
    )
    db.commit()
    db.refresh(schedule)
    return schedule

