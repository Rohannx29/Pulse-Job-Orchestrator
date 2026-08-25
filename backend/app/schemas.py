from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import JobStatus, RetryStrategy, WorkerStatus, LogLevel


# ---------- Auth ----------

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: Optional[str] = None
    organization_name: str


class UserOut(BaseModel):
    id: str
    email: EmailStr
    full_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


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
    depends_on_job_id: Optional[str]
    claimed_by_worker_id: Optional[str]
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
