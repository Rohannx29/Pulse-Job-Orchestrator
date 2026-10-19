from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_system_admin
from app.models import (
    Job,
    Organization,
    OrganizationMember,
    Project,
    Queue,
    User,
    UserRole,
)
from app.schemas import AdminOrgOut, AdminStatsOut, AdminUserOut, UpdateUserRole

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats", response_model=AdminStatsOut)
def get_system_stats(
    db: Session = Depends(get_db),
    _: User = Depends(require_system_admin),
):
    """Platform-wide statistics for system administrators."""
    return AdminStatsOut(
        total_organizations=db.query(Organization).count(),
        total_users=db.query(User).count(),
        total_projects=db.query(Project).count(),
        total_queues=db.query(Queue).count(),
        total_jobs=db.query(Job).count(),
    )


@router.get("/organizations", response_model=list[AdminOrgOut])
def list_all_organizations(
    db: Session = Depends(get_db),
    _: User = Depends(require_system_admin),
):
    """List all organizations across the entire platform."""
    orgs = db.query(Organization).order_by(Organization.created_at.desc()).all()
    member_counts = dict(
        db.query(OrganizationMember.org_id, func.count(OrganizationMember.id))
        .group_by(OrganizationMember.org_id)
        .all()
    )
    return [
        AdminOrgOut(
            id=org.id,
            name=org.name,
            owner_id=org.owner_id,
            member_count=member_counts.get(org.id, 0),
            created_at=org.created_at,
        )
        for org in orgs
    ]


@router.get("/users", response_model=list[AdminUserOut])
def list_all_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_system_admin),
):
    """List all users across the entire platform."""
    return db.query(User).order_by(User.created_at.desc()).all()


@router.patch("/users/{user_id}/role", response_model=AdminUserOut)
def update_user_role(
    user_id: str,
    payload: UpdateUserRole,
    db: Session = Depends(get_db),
    admin: User = Depends(require_system_admin),
):
    """Promote or demote a user's system role."""
    target_user = db.get(User, user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    old_role = target_user.role
    target_user.role = payload.role
    from app.audit import log_audit_event
    log_audit_event(
        db=db,
        action="user.role_change",
        resource_type="user",
        resource_id=target_user.id,
        user=admin,
        org_id=None,
        details={"target_email": target_user.email, "old_role": old_role, "new_role": payload.role},
    )
    db.commit()
    db.refresh(target_user)
    return target_user


@router.post("/prune")
def trigger_retention_prune(
    db: Session = Depends(get_db),
    admin: User = Depends(require_system_admin),
):
    """Manually triggers data retention pruning across heartbeats, logs, executions, and terminal jobs."""
    from app.retention import run_retention_prune
    return run_retention_prune(db=db, user=admin)


