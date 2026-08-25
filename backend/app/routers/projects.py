from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import Organization, Project, User
from app.schemas import ProjectCreate, ProjectOut

router = APIRouter(prefix="/projects", tags=["projects"])


def _get_user_org(db: Session, user: User) -> Organization:
    org = db.query(Organization).filter(Organization.owner_id == user.id).first()
    if org is None:
        raise HTTPException(status_code=400, detail="User has no organization")
    return org


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    payload: ProjectCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    org = _get_user_org(db, user)
    project = Project(org_id=org.id, name=payload.name, description=payload.description)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    org = _get_user_org(db, user)
    return db.query(Project).filter(Project.org_id == org.id).all()


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    org = _get_user_org(db, user)
    project = db.query(Project).filter(Project.id == project_id, Project.org_id == org.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project
