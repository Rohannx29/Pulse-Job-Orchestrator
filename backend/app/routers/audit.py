from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_user, get_user_org
from app.models import AuditLog, User, UserRole
from app.schemas import AuditLogOut

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])


@router.get("", response_model=list[AuditLogOut])
def list_audit_logs(
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Retrieve immutable audit logs.
    System administrators see platform-wide events; other users see their organization's events."""
    query = db.query(AuditLog)

    if user.role != UserRole.SYSTEM_ADMIN:
        org = get_user_org(db, user)
        query = query.filter(AuditLog.org_id == org.id)

    if action:
        query = query.filter(AuditLog.action == action)
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)

    logs = (
        query.options(joinedload(AuditLog.user))
        .order_by(AuditLog.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    result = []
    for log in logs:
        result.append(
            AuditLogOut(
                id=log.id,
                org_id=log.org_id,
                user_id=log.user_id,
                user_email=log.user.email if log.user else None,
                user_name=log.user.full_name if log.user else None,
                action=log.action,
                resource_type=log.resource_type,
                resource_id=log.resource_id,
                details=log.details,
                created_at=log.created_at,
            )
        )
    return result
