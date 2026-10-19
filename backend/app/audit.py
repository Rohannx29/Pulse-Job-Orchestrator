from typing import Any
from sqlalchemy.orm import Session

from app.models import AuditLog, User


def log_audit_event(
    db: Session,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    user: User | None = None,
    org_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Records an immutable audit log entry.
    Flushes to database as part of the calling route's transaction."""
    entry = AuditLog(
        org_id=org_id,
        user_id=user.id if user else None,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id else None,
        details=details or {},
    )
    db.add(entry)
    return entry
