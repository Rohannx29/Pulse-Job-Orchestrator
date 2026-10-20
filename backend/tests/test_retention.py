"""Unit tests for automated data retention and table pruning."""

from datetime import timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.models import (
    AuditLog,
    Job,
    JobExecution,
    JobLog,
    JobStatus,
    LogLevel,
    Organization,
    OutboxEvent,
    OutboxStatus,
    Project,
    Queue,
    User,
    UserRole,
    WebhookDelivery,
    WebhookSubscription,
    Worker,
    WorkerHeartbeat,
    WorkerStatus,
)
from app.retention import run_retention_prune
from app.timeutils import utcnow


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_prune_old_worker_heartbeats(db_session):
    now = utcnow()
    worker = Worker(hostname="worker-1", status=WorkerStatus.ONLINE)
    db_session.add(worker)
    db_session.commit()

    # 1 fresh heartbeat (1 hour ago)
    fresh_hb = WorkerHeartbeat(worker_id=worker.id, timestamp=now - timedelta(hours=1))
    # 2 stale heartbeats (10 and 20 days ago)
    stale_hb1 = WorkerHeartbeat(worker_id=worker.id, timestamp=now - timedelta(days=10))
    stale_hb2 = WorkerHeartbeat(worker_id=worker.id, timestamp=now - timedelta(days=20))

    db_session.add_all([fresh_hb, stale_hb1, stale_hb2])
    db_session.commit()

    summary = run_retention_prune(db_session, batch_size=10)

    assert summary["pruned_heartbeats"] == 2
    assert db_session.query(WorkerHeartbeat).count() == 1
    remaining = db_session.query(WorkerHeartbeat).first()
    assert remaining.id == fresh_hb.id


def test_prune_old_logs_and_executions(db_session):
    now = utcnow()
    user = User(email="test@example.com", hashed_password="pwd", role=UserRole.ORG_ADMIN)
    org = Organization(name="TestOrg", owner=user)
    proj = Project(name="TestProj", organization=org)
    queue = Queue(name="default", project=proj)
    job = Job(name="test-job", queue=queue, status=JobStatus.COMPLETED)
    db_session.add_all([user, org, proj, queue, job])
    db_session.commit()

    # Old finished execution (40 days ago)
    old_exec = JobExecution(
        job_id=job.id,
        status=JobStatus.COMPLETED,
        started_at=now - timedelta(days=40),
        finished_at=now - timedelta(days=40),
    )
    # Fresh finished execution (5 days ago)
    fresh_exec = JobExecution(
        job_id=job.id,
        status=JobStatus.COMPLETED,
        started_at=now - timedelta(days=5),
        finished_at=now - timedelta(days=5),
    )
    db_session.add_all([old_exec, fresh_exec])
    db_session.commit()

    old_log = JobLog(job_execution_id=old_exec.id, level=LogLevel.INFO, message="Old log", timestamp=now - timedelta(days=40))
    fresh_log = JobLog(job_execution_id=fresh_exec.id, level=LogLevel.INFO, message="Fresh log", timestamp=now - timedelta(days=5))
    db_session.add_all([old_log, fresh_log])
    db_session.commit()

    old_exec_id = old_exec.id
    fresh_exec_id = fresh_exec.id

    summary = run_retention_prune(db_session, batch_size=10)

    assert summary["pruned_job_logs"] >= 1
    assert summary["pruned_job_executions"] >= 1
    assert db_session.query(JobExecution).filter(JobExecution.id == fresh_exec_id).first() is not None
    assert db_session.query(JobExecution).filter(JobExecution.id == old_exec_id).first() is None


def test_prune_terminal_jobs_only(db_session):
    now = utcnow()
    user = User(email="admin@example.com", hashed_password="pwd", role=UserRole.SYSTEM_ADMIN)
    org = Organization(name="TestOrg2", owner=user)
    proj = Project(name="TestProj2", organization=org)
    queue = Queue(name="default", project=proj)
    db_session.add_all([user, org, proj, queue])
    db_session.commit()

    # Old completed job (70 days ago) -> should be pruned
    job_old_completed = Job(
        name="job-old-completed",
        queue_id=queue.id,
        status=JobStatus.COMPLETED,
        updated_at=now - timedelta(days=70),
    )
    # Old running job (70 days ago) -> should NOT be pruned (not terminal)
    job_old_running = Job(
        name="job-old-running",
        queue_id=queue.id,
        status=JobStatus.RUNNING,
        updated_at=now - timedelta(days=70),
    )
    # Recent completed job (5 days ago) -> should NOT be pruned
    job_recent_completed = Job(
        name="job-recent-completed",
        queue_id=queue.id,
        status=JobStatus.COMPLETED,
        updated_at=now - timedelta(days=5),
    )
    db_session.add_all([job_old_completed, job_old_running, job_recent_completed])
    db_session.commit()

    old_completed_id = job_old_completed.id
    old_running_id = job_old_running.id
    recent_completed_id = job_recent_completed.id

    summary = run_retention_prune(db_session, user=user)

    assert summary["pruned_completed_jobs"] == 1
    assert db_session.query(Job).filter(Job.id == old_completed_id).first() is None
    assert db_session.query(Job).filter(Job.id == old_running_id).first() is not None
    assert db_session.query(Job).filter(Job.id == recent_completed_id).first() is not None

    # Verify audit log was recorded for admin prune
    audit = db_session.query(AuditLog).filter(AuditLog.action == "system.data_prune").first()
    assert audit is not None
    assert audit.user_id == user.id
    assert audit.details["pruned_completed_jobs"] == 1


def test_prune_outbox_events_and_webhook_deliveries(db_session):
    now = utcnow()
    user = User(email="retention_owner@example.com", hashed_password="pwd", role=UserRole.ORG_ADMIN)
    org = Organization(name="RetentionOrg", owner=user)
    db_session.add_all([user, org])
    db_session.commit()

    # 1. Outbox events:
    # Stale dispatched outbox event (20 days ago) -> should be pruned
    stale_dispatched = OutboxEvent(
        event_type="job.completed",
        aggregate_type="job",
        aggregate_id="job-1",
        status=OutboxStatus.DISPATCHED,
        published_at=now - timedelta(days=20),
    )
    # Stale pending outbox event (20 days ago) -> should NOT be pruned (not dispatched)
    stale_pending = OutboxEvent(
        event_type="job.completed",
        aggregate_type="job",
        aggregate_id="job-2",
        status=OutboxStatus.PENDING,
        created_at=now - timedelta(days=20),
    )
    # Fresh dispatched outbox event (2 days ago) -> should NOT be pruned
    fresh_dispatched = OutboxEvent(
        event_type="job.completed",
        aggregate_type="job",
        aggregate_id="job-3",
        status=OutboxStatus.DISPATCHED,
        published_at=now - timedelta(days=2),
    )
    db_session.add_all([stale_dispatched, stale_pending, fresh_dispatched])

    # 2. Webhook deliveries:
    sub = WebhookSubscription(
        org_id=org.id,
        url="https://example.com/webhook",
        secret="whsec_test_secret_123",
    )
    db_session.add(sub)
    db_session.commit()

    # Stale delivery (45 days ago) -> should be pruned
    stale_delivery = WebhookDelivery(
        subscription_id=sub.id,
        event="job.completed",
        payload={"job_id": "1"},
        delivered_at=now - timedelta(days=45),
    )
    # Fresh delivery (3 days ago) -> should NOT be pruned
    fresh_delivery = WebhookDelivery(
        subscription_id=sub.id,
        event="job.completed",
        payload={"job_id": "2"},
        delivered_at=now - timedelta(days=3),
    )
    db_session.add_all([stale_delivery, fresh_delivery])
    db_session.commit()

    stale_disp_id = stale_dispatched.id
    stale_pend_id = stale_pending.id
    fresh_disp_id = fresh_dispatched.id
    stale_del_id = stale_delivery.id
    fresh_del_id = fresh_delivery.id

    summary = run_retention_prune(db_session, batch_size=10)

    assert summary["pruned_outbox_events"] == 1
    assert summary["pruned_webhook_deliveries"] == 1

    assert db_session.query(OutboxEvent).filter(OutboxEvent.id == stale_disp_id).first() is None
    assert db_session.query(OutboxEvent).filter(OutboxEvent.id == stale_pend_id).first() is not None
    assert db_session.query(OutboxEvent).filter(OutboxEvent.id == fresh_disp_id).first() is not None

    assert db_session.query(WebhookDelivery).filter(WebhookDelivery.id == stale_del_id).first() is None
    assert db_session.query(WebhookDelivery).filter(WebhookDelivery.id == fresh_del_id).first() is not None

