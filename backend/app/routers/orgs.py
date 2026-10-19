from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_user, get_user_org, require_org_admin
from app.models import (
    InviteToken,
    OrgMemberRole,
    Organization,
    OrganizationMember,
    User,
)
from app.schemas import InviteCreate, InviteOut, OrgMemberOut, OrgOut
from app.services.invitations import create_organization_invite
from app.security import create_refresh_token, hash_refresh_token
from app.timeutils import utcnow

router = APIRouter(prefix="/orgs", tags=["organizations"])

INVITE_EXPIRE_HOURS = 72


@router.get("/me", response_model=OrgOut)
def get_my_organization(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    org = get_user_org(db, user)
    member_count = db.query(OrganizationMember).filter(OrganizationMember.org_id == org.id).count()
    return OrgOut(
        id=org.id,
        name=org.name,
        owner_id=org.owner_id,
        member_count=member_count,
        created_at=org.created_at,
    )


@router.get("/me/members", response_model=list[OrgMemberOut])
def list_org_members(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    org = get_user_org(db, user)
    members = (
        db.query(OrganizationMember)
        .options(joinedload(OrganizationMember.user))
        .filter(OrganizationMember.org_id == org.id)
        .order_by(OrganizationMember.created_at.asc())
        .all()
    )
    result = []
    for m in members:
        if m.user:
            result.append(
                OrgMemberOut(
                    id=m.id,
                    user_id=m.user.id,
                    email=m.user.email,
                    full_name=m.user.full_name,
                    role=m.role,
                    created_at=m.created_at,
                )
            )
    return result


@router.post("/me/invite", response_model=InviteOut)
def invite_member(
    payload: InviteCreate,
    db: Session = Depends(get_db),
    auth: tuple[User, Organization] = Depends(require_org_admin),
):
    user, org = auth
    raw_token, expires_at = create_organization_invite(
        db=db,
        org=org,
        actor_user=user,
        email=str(payload.email),
    )
    return InviteOut(invite_token=raw_token, email=str(payload.email), expires_at=expires_at)


@router.delete("/me/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_org_member(
    member_id: str,
    db: Session = Depends(get_db),
    auth: tuple[User, Organization] = Depends(require_org_admin),
):
    admin_user, org = auth
    member = db.query(OrganizationMember).filter(
        OrganizationMember.id == member_id,
        OrganizationMember.org_id == org.id,
    ).first()

    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    if member.user_id == org.owner_id:
        raise HTTPException(status_code=400, detail="Cannot remove the organization owner")

    target_user_id = member.user_id
    db.delete(member)
    from app.audit import log_audit_event
    log_audit_event(
        db=db,
        action="member.remove",
        resource_type="organization",
        resource_id=org.id,
        user=admin_user,
        org_id=org.id,
        details={"removed_user_id": target_user_id},
    )
    db.commit()

