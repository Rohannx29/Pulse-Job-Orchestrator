from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

try:
    import email_validator  # noqa: F401
    from pydantic import EmailStr
except ImportError:
    EmailStr = str  # Fallback for environments without email-validator installed

from app.models import JobStatus, OrgMemberRole, RetryStrategy, UserRole, WorkerStatus, LogLevel


# ---------- Auth ----------

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: Optional[str] = None
    organization_name: str

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")
        if not (any(c.isalpha() for c in v) and any(not c.isalpha() for c in v)):
            raise ValueError("Password must contain both letters and numbers/symbols")
        return v


class UserOut(BaseModel):
    id: str
    email: EmailStr
    full_name: Optional[str] = None
    role: UserRole = UserRole.ORG_ADMIN

    model_config = ConfigDict(from_attributes=True)


# ---------- Invite / Members ----------

class InviteCreate(BaseModel):
    """Body for POST /orgs/me/invite — sent by an org admin."""
    email: EmailStr


class InviteOut(BaseModel):
    """Returned to the inviting admin. The raw token is shown once."""
    invite_token: str
    email: str
    expires_at: datetime


class AcceptInviteRequest(BaseModel):
    """Body for POST /auth/accept-invite — submitted by the invitee."""
    token: str
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: Optional[str] = None

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")
        if not (any(c.isalpha() for c in v) and any(not c.isalpha() for c in v)):
            raise ValueError("Password must contain both letters and numbers/symbols")
        return v


class OrgMemberOut(BaseModel):
    id: str
    user_id: str
    email: str
    full_name: Optional[str]
    role: OrgMemberRole
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrgOut(BaseModel):
    id: str
    name: str
    owner_id: str
    member_count: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Admin (system-level) ----------

class AdminOrgOut(BaseModel):
    id: str
    name: str
    owner_id: str
    member_count: int
    created_at: datetime


class AdminUserOut(BaseModel):
    id: str
    email: str
    full_name: Optional[str]
    role: UserRole
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UpdateUserRole(BaseModel):
    role: UserRole


class AdminStatsOut(BaseModel):
    total_organizations: int
    total_users: int
    total_projects: int
    total_queues: int
    total_jobs: int



# ---------- Projects ----------

class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None


class ProjectOut(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Queues ----------

class QueueCreate(BaseModel):
    name: str
    priority: int = Field(default=5, ge=1, le=10)
    concurrency_limit: int = Field(default=4, ge=1)


class QueueUpdate(BaseModel):
    priority: Optional[int] = Field(default=None, ge=1, le=10)
    concurrency_limit: Optional[int] = Field(default=None, ge=1)
    is_paused: Optional[bool] = None


class QueueOut(BaseModel):
    id: str
    project_id: str
    name: str
    priority: int
    concurrency_limit: int
    is_paused: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class QueueStatsOut(BaseModel):
    queue_id: str
    counts_by_status: dict[str, int]
    total_jobs: int
    success_rate: Optional[float]  # completed / (completed + failed_terminal), None if no terminal jobs yet
    avg_execution_seconds: Optional[float]
    throughput_last_hour: int  # jobs completed in the last 60 minutes
    current_concurrency_usage: int
    concurrency_limit: int


# ---------- Retry Policies ----------

class RetryPolicyCreate(BaseModel):
    name: str = "default"
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL
    max_retries: int = Field(default=3, ge=0, le=20)
    base_delay_seconds: int = Field(default=10, ge=1)


class RetryPolicyOut(RetryPolicyCreate):
    id: str
    queue_id: str

    model_config = ConfigDict(from_attributes=True)


# ---------- Jobs ----------

class JobCreate(BaseModel):
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: Optional[int] = Field(default=None, ge=1, le=10)  # None = inherit queue's priority
    run_at: Optional[datetime] = None  # None = run immediately
    max_retries: int = Field(default=3, ge=0, le=20)
    retry_policy_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    depends_on_job_id: Optional[str] = None
    depends_on_job_ids: Optional[list[str]] = None


class JobBatchCreate(BaseModel):
    jobs: list[JobCreate]


class JobOut(BaseModel):
    id: str
    queue_id: str
    name: str
    payload: dict[str, Any]
    status: JobStatus
    priority: int
    run_at: Optional[datetime]
    max_retries: int
    current_retry_count: int
    celery_task_id: Optional[str]
    depends_on_job_id: Optional[str] = None
    depends_on_job_ids: list[str] = Field(default_factory=list)
    claimed_by_worker_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class JobLogOut(BaseModel):
    id: str
    timestamp: datetime
    level: LogLevel
    message: str

    model_config = ConfigDict(from_attributes=True)


class JobExecutionOut(BaseModel):
    id: str
    worker_id: Optional[str]
    attempt_number: int
    status: JobStatus
    started_at: datetime
    finished_at: Optional[datetime]
    error_message: Optional[str]
    result: Optional[dict[str, Any]]
    logs: list[JobLogOut] = []

    model_config = ConfigDict(from_attributes=True)


class JobDetailOut(JobOut):
    executions: list[JobExecutionOut] = []


# ---------- DAG Workflows ----------

class DAGNode(BaseModel):
    id: str
    name: str
    queue_id: str
    queue_name: Optional[str] = None
    status: JobStatus
    priority: int
    current_retry_count: int
    max_retries: int
    parent_job_ids: list[str] = Field(default_factory=list)
    run_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DAGEdge(BaseModel):
    parent_id: str
    child_id: str


class DAGGraphOut(BaseModel):
    root_job_id: Optional[str] = None
    nodes: list[DAGNode] = Field(default_factory=list)
    edges: list[DAGEdge] = Field(default_factory=list)
    is_acyclic: bool = True


# ---------- Schedules ----------

class JobScheduleCreate(BaseModel):
    name: str
    cron_expression: str
    payload_template: dict[str, Any] = Field(default_factory=dict)


class JobScheduleOut(BaseModel):
    id: str
    queue_id: str
    name: str
    cron_expression: str
    payload_template: dict[str, Any]
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Workers ----------

class WorkerOut(BaseModel):
    id: str
    hostname: str
    status: WorkerStatus
    last_heartbeat_at: Optional[datetime]
    registered_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Dead Letter Queue ----------

class DeadLetterJobOut(BaseModel):
    id: str
    job_id: str
    reason: str
    original_payload: dict[str, Any]
    retry_count_at_failure: int
    failed_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Audit Logs ----------

class AuditLogOut(BaseModel):
    id: str
    org_id: Optional[str]
    user_id: Optional[str]
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    action: str
    resource_type: str
    resource_id: Optional[str]
    details: dict[str, Any]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------- Webhooks ----------

class WebhookSubscriptionCreate(BaseModel):
    url: str
    events: list[str] = Field(default_factory=lambda: ["*"])
    description: Optional[str] = None


class WebhookSubscriptionUpdate(BaseModel):
    url: Optional[str] = None
    events: Optional[list[str]] = None
    is_active: Optional[bool] = None
    description: Optional[str] = None


class WebhookSubscriptionOut(BaseModel):
    id: str
    org_id: str
    url: str
    secret: str
    events: list[str]
    is_active: bool
    description: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WebhookDeliveryOut(BaseModel):
    id: str
    subscription_id: str
    event: str
    payload: dict[str, Any]
    status_code: Optional[int] = None
    response_body: Optional[str] = None
    error_message: Optional[str] = None
    attempt_number: int
    duration_ms: Optional[float] = None
    is_success: bool
    delivered_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WebhookTestResponse(BaseModel):
    status: str
    delivery_id: str
    status_code: Optional[int] = None
    is_success: bool
    duration_ms: Optional[float] = None


