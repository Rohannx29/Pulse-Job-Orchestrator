"""Shared service for organization invitations and member provisioning.

Centralizes invite creation, validation, token hashing, and audit logging
into a single authoritative service called by both /auth/invite and /orgs/me/invite.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Tuple

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.audit import log_audit_event
from app.models import InviteToken, Organization, OrganizationMember, User
from app.security import create_refresh_token, hash_refresh_token
from app.timeutils import utcnow

INVITE_EXPIRE_HOURS = 72


def create_organization_invite(
    db: Session,
    org: Organization,
    actor_user: User,
    email: str,
) -> Tuple[str, datetime]:
    """Validates target email, invalidates pending invites, generates a single-use token,
    hashes the token at rest in PostgreSQL, and logs an audited 'member.invite' event.

    Returns (raw_token, expires_at).
    """
    clean_email = email.strip().lower()

    # 1. Check if the target email is already an active member of this organization
    existing = (
        db.query(User)
        .join(OrganizationMember, User.id == OrganizationMember.user_id)
        .filter(
            OrganizationMember.org_id == org.id,
            User.email == clean_email,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already a member of this organization",
        )

    # 2. Invalidate any previous unused invites for this email + org combination
    db.query(InviteToken).filter(
        InviteToken.org_id == org.id,
        InviteToken.email == clean_email,
        InviteToken.used_at.is_(None),
    ).update({InviteToken.used_at: utcnow()}, synchronize_session=False)

    # 3. Generate raw secret token and hash
    raw_token = create_refresh_token()
    expires_at = utcnow() + timedelta(hours=INVITE_EXPIRE_HOURS)

    invite = InviteToken(
        org_id=org.id,
        invited_by_user_id=actor_user.id,
        email=clean_email,
        token_hash=hash_refresh_token(raw_token),
        expires_at=expires_at,
    )
    db.add(invite)

    # 4. Durable audit logging
    log_audit_event(
        db=db,
        action="member.invite",
        resource_type="organization",
        resource_id=org.id,
        user=actor_user,
        org_id=org.id,
        details={"invited_email": clean_email},
    )

    db.commit()

    return raw_token, expires_at
