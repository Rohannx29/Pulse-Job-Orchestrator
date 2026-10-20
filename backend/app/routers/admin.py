from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_system_admin
from app.models import (
    Job,
    Organization,
    OrganizationMember,
    OutboxEvent,
    OutboxStatus,
    Project,
    Queue,
    User,
    UserRole,
)
from app.outbox import get_outbox_stats, replay_failed_outbox_event
from app.schemas import AdminOrgOut, AdminStatsOut, AdminUserOut, OutboxEventOut, OutboxStatsOut, UpdateUserRole

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


@router.get("/outbox/stats", response_model=OutboxStatsOut)
def get_admin_outbox_stats(
    db: Session = Depends(get_db),
    _: User = Depends(require_system_admin),
):
    """Retrieves operational backlog, oldest pending age, and failure stats for the transactional outbox."""
    stats = get_outbox_stats(db)
    return OutboxStatsOut(**stats)


@router.get("/outbox/events", response_model=list[OutboxEventOut])
def list_outbox_events(
    db: Session = Depends(get_db),
    _: User = Depends(require_system_admin),
    status: Optional[OutboxStatus] = Query(None, description="Filter by outbox status (PENDING, DISPATCHED, FAILED)"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Inspects outbox events across the platform with pagination and status filtering."""
    q = db.query(OutboxEvent)
    if status is not None:
        q = q.filter(OutboxEvent.status == status)
    return q.order_by(OutboxEvent.created_at.desc()).offset(offset).limit(limit).all()


@router.post("/outbox/events/{event_id}/replay", response_model=OutboxEventOut)
def replay_outbox_event(
    event_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_system_admin),
):
    """Idempotently replays a FAILED outbox event by resetting it to PENDING."""
    event = replay_failed_outbox_event(db, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Outbox event not found")

    from app.audit import log_audit_event
    log_audit_event(
        db=db,
        action="outbox.replay",
        resource_type="outbox_event",
        resource_id=event.id,
        user=admin,
        org_id=None,
        details={"event_type": event.event_type, "aggregate_id": event.aggregate_id},
    )
    db.commit()
    return event


@router.post("/webhooks/encrypt-secrets")
def encrypt_existing_webhook_secrets(
    db: Session = Depends(get_db),
    admin: User = Depends(require_system_admin),
    batch_size: int = Query(100, ge=1, le=1000),
):
    """Administrative command to migrate and encrypt legacy plaintext webhook secrets."""
    from app.commands.encrypt_webhook_secrets import run_webhook_secret_encryption
    from app.audit import log_audit_event

    results = run_webhook_secret_encryption(db=db, batch_size=batch_size, dry_run=False)
    log_audit_event(
        db=db,
        action="webhooks.encrypt_secrets",
        resource_type="system",
        resource_id="kms_migration",
        user=admin,
        org_id=None,
        details=results,
    )
    db.commit()
    return results


