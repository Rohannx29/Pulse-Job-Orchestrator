from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import settings
from app.models import OrgMemberRole, Organization, OrganizationMember, User, UserRole
from app.security import decode_access_token

# In-memory ticket cache fallback for local/test environments when Redis is not running
_LOCAL_TICKET_CACHE: dict[str, tuple[str, float]] = {}


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Cookie"},
    )
    token = request.cookies.get(settings.ACCESS_TOKEN_COOKIE_NAME)
    if token is None:
        raise credentials_exception
    user_id = decode_access_token(token)
    if user_id is None:
        raise credentials_exception
    user = db.get(User, user_id)
    if user is None:
        raise credentials_exception
    return user


def require_system_admin(user: User = Depends(get_current_user)) -> User:
    """Dependency that allows only SYSTEM_ADMIN users through.
    Use on /admin/* routes that manage the entire platform."""
    if user.role != UserRole.SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="System administrator access required")
    return user


def require_org_admin(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> tuple[User, Organization]:
    """Dependency that allows ORG_ADMIN (or SYSTEM_ADMIN) users through.
    Returns (user, organization) so routes don't need to re-query the org.

    SYSTEM_ADMIN is implicitly allowed everywhere an ORG_ADMIN is allowed —
    they are the platform operator and must be able to manage any org."""
    if user.role == UserRole.SYSTEM_ADMIN:
        # System admins don't own an org themselves in the conventional sense,
        # but they registered with one. Use the first org they own.
        org = db.query(Organization).filter(Organization.owner_id == user.id).first()
        if org is None:
            raise HTTPException(status_code=400, detail="No organization found for this admin")
        return user, org

    if user.role not in (UserRole.ORG_ADMIN, UserRole.MEMBER):
        raise HTTPException(status_code=403, detail="Organization admin access required")

    org = db.query(Organization).filter(Organization.owner_id == user.id).first()
    if org is None:
        # Check if user is an org admin via OrganizationMember
        membership = (
            db.query(OrganizationMember)
            .filter(
                OrganizationMember.user_id == user.id,
                OrganizationMember.role == OrgMemberRole.ADMIN,
            )
            .first()
        )
        if membership is None:
            raise HTTPException(status_code=403, detail="Organization admin access required")
        org = db.get(Organization, membership.org_id)

    return user, org


def get_user_org(db: Session, user: User) -> Organization:
    """Gets the primary organization for a user (as owner or member)."""
    org = db.query(Organization).filter(Organization.owner_id == user.id).first()
    if org is None:
        membership = (
            db.query(OrganizationMember)
            .filter(OrganizationMember.user_id == user.id)
            .first()
        )
        if membership:
            org = db.get(Organization, membership.org_id)
    if org is None and user.role == UserRole.SYSTEM_ADMIN:
        org = db.query(Organization).first()
    if org is None:
        raise HTTPException(status_code=400, detail="User has no organization")
    return org


def user_can_access_org(db: Session, user: User, org_id: str) -> bool:
    """Checks whether user has access to a specific organization."""
    if user.role == UserRole.SYSTEM_ADMIN:
        return True
    if db.query(Organization).filter(Organization.id == org_id, Organization.owner_id == user.id).first():
        return True
    if db.query(OrganizationMember).filter(OrganizationMember.org_id == org_id, OrganizationMember.user_id == user.id).first():
        return True
    return False

