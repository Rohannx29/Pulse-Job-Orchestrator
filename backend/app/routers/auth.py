import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.config import settings
from app.timeutils import utcnow
from app.database import get_db
from app.deps import get_current_user, require_org_admin
from app.models import (
    InviteToken,
    OrgMemberRole,
    Organization,
    OrganizationMember,
    RefreshToken,
    User,
    UserRole,
)
from app.schemas import AcceptInviteRequest, InviteCreate, InviteOut, UserCreate, UserOut
from app.auth_hardening import check_account_lockout, record_failed_login, record_successful_login
from app.rate_limiter import RateLimiter
from app.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.services.invitations import create_organization_invite

router = APIRouter(prefix="/auth", tags=["auth"])

# Constant-time mitigation against user enumeration
_DUMMY_PASSWORD_HASH = "$2b$12$e8N70sQ04Yq0h2q1iYq1Ie8F8yX/9M2j4D.4.J1U/F4c9R.8dGkmi"

# How long an invite link stays valid.
INVITE_EXPIRE_HOURS = 72


def _set_auth_cookies(response: Response, user_id: str, refresh_token: str) -> None:
    response.set_cookie(
        key=settings.ACCESS_TOKEN_COOKIE_NAME,
        value=create_access_token(subject=user_id),
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path="/",
    )
    response.set_cookie(
        key=settings.REFRESH_TOKEN_COOKIE_NAME,
        value=refresh_token,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path="/auth",
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(
        key=settings.ACCESS_TOKEN_COOKIE_NAME,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path="/",
    )
    response.delete_cookie(
        key=settings.REFRESH_TOKEN_COOKIE_NAME,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path="/auth",
    )


def _create_refresh_token_record(db: Session, user_id: str, family_id: str | None = None) -> str:
    raw_token = create_refresh_token()
    db.add(
        RefreshToken(
            user_id=user_id,
            family_id=family_id or str(uuid.uuid4()),
            token_hash=hash_refresh_token(raw_token),
            expires_at=utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )
    )
    return raw_token


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(limit=10, window_seconds=300, tag="auth:register"))],
)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    """Open registration — creates a user + organization.

    Role assignment:
    - If this is the very first user in the system → SYSTEM_ADMIN.
    - Otherwise → ORG_ADMIN (owns their own org, can invite members).

    This prevents arbitrary admin escalation: the platform operator account
    is bootstrapped exactly once on a fresh install.
    """
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    is_first_user = db.query(User).count() == 0
    assigned_role = UserRole.SYSTEM_ADMIN if is_first_user else UserRole.ORG_ADMIN

    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        role=assigned_role,
    )
    db.add(user)
    db.flush()

    org = Organization(name=payload.organization_name, owner_id=user.id)
    db.add(org)
    db.flush()

    # Create an OrganizationMember row for the owner so all access checks
    # can use the members table as the single source of truth.
    db.add(OrganizationMember(org_id=org.id, user_id=user.id, role=OrgMemberRole.ADMIN))
    db.commit()
    db.refresh(user)
    return user


@router.post(
    "/login",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(RateLimiter(limit=15, window_seconds=60, tag="auth:login"))],
)
def login(
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    # Check if account is locked due to excessive failed attempts
    check_account_lockout(form_data.username)

    user = db.query(User).filter(User.email == form_data.username).first()
    if not user:
        # Constant-time dummy verification to eliminate user-enumeration timing leaks
        verify_password(form_data.password, _DUMMY_PASSWORD_HASH)
        record_failed_login(form_data.username)
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    if not verify_password(form_data.password, user.hashed_password):
        record_failed_login(form_data.username)
        raise HTTPException(status_code=401, detail="Incorrect email or password")

    # Clear any previous failure counters on successful authentication
    record_successful_login(form_data.username)

    refresh_token = _create_refresh_token_record(db, user.id)
    db.commit()
    _set_auth_cookies(response, user.id, refresh_token)


@router.post("/refresh", status_code=status.HTTP_204_NO_CONTENT)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    raw_token = request.cookies.get(settings.REFRESH_TOKEN_COOKIE_NAME)
    token = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == hash_refresh_token(raw_token))
        .first()
        if raw_token
        else None
    )
    now = utcnow()
    if token is None or token.expires_at <= now:
        _clear_auth_cookies(response)
        raise HTTPException(status_code=401, detail="Refresh token is invalid or expired")

    if token.revoked_at is not None:
        db.query(RefreshToken).filter(
            RefreshToken.family_id == token.family_id,
            RefreshToken.revoked_at.is_(None),
        ).update({RefreshToken.revoked_at: now}, synchronize_session=False)
        db.commit()
        _clear_auth_cookies(response)
        raise HTTPException(status_code=401, detail="Refresh token has been revoked")

    token.revoked_at = now
    next_refresh_token = _create_refresh_token_record(db, token.user_id, token.family_id)
    db.commit()
    _set_auth_cookies(response, token.user_id, next_refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    raw_token = request.cookies.get(settings.REFRESH_TOKEN_COOKIE_NAME)
    if raw_token:
        token = (
            db.query(RefreshToken)
            .filter(RefreshToken.token_hash == hash_refresh_token(raw_token))
            .first()
        )
        if token is not None and token.revoked_at is None:
            token.revoked_at = utcnow()
            db.commit()
    _clear_auth_cookies(response)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


# ---------------------------------------------------------------------------
# Invite flow — org admins invite new members; invitees accept via token
# ---------------------------------------------------------------------------

@router.post("/invite", response_model=InviteOut)
def invite_member(
    payload: InviteCreate,
    db: Session = Depends(get_db),
    auth: tuple = Depends(require_org_admin),
):
    """Org admin creates an invite link for a new member.

    The raw token is returned in the response (shown once to the admin).
    Only the SHA-256 hash is stored in the database.
    The invite expires in 72 hours and is tied to the specific email address.
    """
    user, org = auth
    raw_token, expires_at = create_organization_invite(
        db=db,
        org=org,
        actor_user=user,
        email=str(payload.email),
    )
    return InviteOut(invite_token=raw_token, email=str(payload.email), expires_at=expires_at)


@router.post(
    "/accept-invite",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(limit=15, window_seconds=300, tag="auth:accept-invite"))],
)
def accept_invite(
    payload: AcceptInviteRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    """Accept an invite and create a new member account.

    Validates: token exists, not expired, not already used, email matches.
    On success: creates the User as MEMBER role, links them to the org via
    OrganizationMember, marks the invite as used, and sets session cookies
    so the user is immediately logged in.
    """
    token_record = (
        db.query(InviteToken)
        .filter(InviteToken.token_hash == hash_refresh_token(payload.token))
        .first()
    )
    now = utcnow()

    if token_record is None:
        raise HTTPException(status_code=400, detail="Invalid invite token")
    if token_record.used_at is not None:
        raise HTTPException(status_code=400, detail="Invite token has already been used")
    if token_record.expires_at <= now:
        raise HTTPException(status_code=400, detail="Invite token has expired")
    if token_record.email.lower() != str(payload.email).lower():
        raise HTTPException(status_code=400, detail="Email does not match the invite")

    if db.query(User).filter(User.email == str(payload.email)).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=str(payload.email),
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        role=UserRole.MEMBER,
    )
    db.add(user)
    db.flush()

    db.add(OrganizationMember(
        org_id=token_record.org_id,
        user_id=user.id,
        role=OrgMemberRole.MEMBER,
    ))

    token_record.used_at = now
    refresh_token = _create_refresh_token_record(db, user.id)
    db.commit()
    db.refresh(user)

    _set_auth_cookies(response, user.id, refresh_token)
    return user
