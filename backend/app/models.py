import enum
import uuid
from app.timeutils import utcnow

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import backref, relationship

from app.database import Base


def gen_uuid():
    return str(uuid.uuid4())


# ---------- Enums ----------

class JobStatus(str, enum.Enum):
    BLOCKED = "blocked"
    QUEUED = "queued"
    SCHEDULED = "scheduled"
    CLAIMED = "claimed"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class RetryStrategy(str, enum.Enum):
    FIXED = "fixed"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"


class WorkerStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class LogLevel(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# ---------- Core entities ----------

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    organizations = relationship("Organization", back_populates="owner")
    refresh_tokens = relationship("RefreshToken", back_populates="user", cascade="all, delete-orphan")


class RefreshToken(Base):
    """One-time refresh token record used for rotation and server-side revocation."""

    __tablename__ = "refresh_tokens"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    family_id = Column(UUID(as_uuid=False), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    user = relationship("User", back_populates="refresh_tokens")


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    name = Column(String(255), nullable=False)
    owner_id = Column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    owner = relationship("User", back_populates="organizations")
    projects = relationship("Project", back_populates="organization", cascade="all, delete-orphan")


class Project(Base):
    __tablename__ = "projects"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    org_id = Column(UUID(as_uuid=False), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    organization = relationship("Organization", back_populates="projects")
    queues = relationship("Queue", back_populates="project", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_project_name_per_org"),)


class Queue(Base):
    __tablename__ = "queues"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    project_id = Column(UUID(as_uuid=False), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    priority = Column(Integer, default=5, nullable=False)  # 1 (highest) - 10 (lowest)
    concurrency_limit = Column(Integer, default=4, nullable=False)
    is_paused = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    project = relationship("Project", back_populates="queues")
    jobs = relationship("Job", back_populates="queue", cascade="all, delete-orphan")
    retry_policies = relationship("RetryPolicy", back_populates="queue", cascade="all, delete-orphan")
    schedules = relationship("JobSchedule", back_populates="queue", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_queue_name_per_project"),)


class RetryPolicy(Base):
    __tablename__ = "retry_policies"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    queue_id = Column(UUID(as_uuid=False), ForeignKey("queues.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(120), nullable=False, default="default")
    strategy = Column(Enum(RetryStrategy), nullable=False, default=RetryStrategy.EXPONENTIAL)
    max_retries = Column(Integer, nullable=False, default=3)
    base_delay_seconds = Column(Integer, nullable=False, default=10)

    queue = relationship("Queue", back_populates="retry_policies")


class JobSchedule(Base):
    """Defines a recurring (cron) job. Celery Beat reads active rows and
    dispatches new Job rows on each tick."""

    __tablename__ = "job_schedules"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    queue_id = Column(UUID(as_uuid=False), ForeignKey("queues.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    cron_expression = Column(String(120), nullable=False)  # standard 5-field cron
    payload_template = Column(JSONB, nullable=False, default=dict)
    is_active = Column(Boolean, default=True, nullable=False)
    last_dispatched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    queue = relationship("Queue", back_populates="schedules")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    queue_id = Column(UUID(as_uuid=False), ForeignKey("queues.id", ondelete="CASCADE"), nullable=False, index=True)
    schedule_id = Column(UUID(as_uuid=False), ForeignKey("job_schedules.id", ondelete="SET NULL"), nullable=True)
    retry_policy_id = Column(UUID(as_uuid=False), ForeignKey("retry_policies.id", ondelete="SET NULL"), nullable=True)
    depends_on_job_id = Column(UUID(as_uuid=False), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    batch_id = Column(UUID(as_uuid=False), nullable=True, index=True)  # groups batch-created jobs

    name = Column(String(255), nullable=False)
    payload = Column(JSONB, nullable=False, default=dict)
    status = Column(Enum(JobStatus), nullable=False, default=JobStatus.QUEUED, index=True)
    priority = Column(Integer, default=5, nullable=False)

    run_at = Column(DateTime, nullable=True)  # null = run immediately
    idempotency_key = Column(String(255), nullable=True, index=True)

    max_retries = Column(Integer, nullable=False, default=3)
    current_retry_count = Column(Integer, nullable=False, default=0)

    celery_task_id = Column(String(155), nullable=True, index=True)
    claimed_by_worker_id = Column(UUID(as_uuid=False), ForeignKey("workers.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    queue = relationship("Queue", back_populates="jobs")
    executions = relationship("JobExecution", back_populates="job", cascade="all, delete-orphan")
    dead_letter_entry = relationship("DeadLetterJob", back_populates="job", uselist=False, cascade="all, delete-orphan")
    dependents = relationship("Job", backref=backref("depends_on", remote_side=[id]))

    __table_args__ = (
        Index("ix_jobs_queue_status", "queue_id", "status"),
        UniqueConstraint("queue_id", "idempotency_key", name="uq_job_idempotency_per_queue"),
    )


class JobExecution(Base):
    __tablename__ = "job_executions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    job_id = Column(UUID(as_uuid=False), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    worker_id = Column(UUID(as_uuid=False), ForeignKey("workers.id", ondelete="SET NULL"), nullable=True)

    attempt_number = Column(Integer, nullable=False, default=1)
    status = Column(Enum(JobStatus), nullable=False, default=JobStatus.RUNNING)
    started_at = Column(DateTime, default=utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    result = Column(JSONB, nullable=True)

    job = relationship("Job", back_populates="executions")
    logs = relationship("JobLog", back_populates="execution", cascade="all, delete-orphan")


class JobLog(Base):
    __tablename__ = "job_logs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    job_execution_id = Column(UUID(as_uuid=False), ForeignKey("job_executions.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp = Column(DateTime, default=utcnow, nullable=False)
    level = Column(Enum(LogLevel), nullable=False, default=LogLevel.INFO)
    message = Column(Text, nullable=False)

    execution = relationship("JobExecution", back_populates="logs")


class Worker(Base):
    __tablename__ = "workers"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    hostname = Column(String(255), nullable=False, unique=True)
    celery_worker_id = Column(String(255), nullable=True)
    status = Column(Enum(WorkerStatus), nullable=False, default=WorkerStatus.OFFLINE)
    last_heartbeat_at = Column(DateTime, nullable=True)
    registered_at = Column(DateTime, default=utcnow, nullable=False)

    heartbeats = relationship("WorkerHeartbeat", back_populates="worker", cascade="all, delete-orphan")


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    worker_id = Column(UUID(as_uuid=False), ForeignKey("workers.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp = Column(DateTime, default=utcnow, nullable=False)
    active_jobs_count = Column(Integer, default=0, nullable=False)

    worker = relationship("Worker", back_populates="heartbeats")


class DeadLetterJob(Base):
    __tablename__ = "dead_letter_jobs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    job_id = Column(UUID(as_uuid=False), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    reason = Column(Text, nullable=False)
    original_payload = Column(JSONB, nullable=False)
    retry_count_at_failure = Column(Integer, nullable=False)
    failed_at = Column(DateTime, default=utcnow, nullable=False)

    job = relationship("Job", back_populates="dead_letter_entry")
