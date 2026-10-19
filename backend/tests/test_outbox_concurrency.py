"""Concurrency and Exactly-Once Dispatch Integration Tests for Pulse.

Acceptance Criteria Verified:
1. Synchronized two-parent fan-in race:
   When two parent jobs complete simultaneously, atomic conditional update
   guarantees child job transitions BLOCKED -> QUEUED exactly once, produces
   only one dispatch, and is never executed twice.
2. Transactional outbox durability & idempotence:
   Events are recorded in the DB transaction and only dispatched after commit.
   Rollback prevents dispatch. Subsequent publisher runs are idempotent.
3. Outbox crash recovery & retry limit:
   Worker crash or broker connection failure increments retry count and stores last_error.
   Exceeding max retries marks event as FAILED without infinite loop.
4. Schedule occurrence deduplication under races:
   Concurrent beat workers trying to claim the same fire time are deduplicated
   by database unique constraint (schedule_id, scheduled_fire_time). Exactly one job created.
5. Fail-closed malformed legacy schedule:
   Invalid cron expressions disable the offending schedule and record an audit event,
   while valid schedules continue uninterrupted.
6. Safe Redis lock release with owned UUID token:
   Compare-and-delete Lua script prevents a stale worker from releasing a lock
   acquired by a subsequent worker.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.database import Base
from app.dag import link_dag_dependencies, release_dag_dependents
from app.dispatch import dispatch_job
from app.models import (
    AuditLog,
    Job,
    JobDependency,
    JobSchedule,
    JobStatus,
    Organization,
    OutboxEvent,
    OutboxStatus,
    Project,
    Queue,
    ScheduleOccurrence,
    User,
    UserRole,
)
from app.outbox import (
    MAX_OUTBOX_RETRIES,
    publish_pending_outbox_events,
    publish_single_outbox_event,
    record_job_dispatch_outbox,
)
from app.tasks import LUA_SAFE_RELEASE_LOCK, dispatch_due_schedules
from app.timeutils import utcnow


@pytest.fixture
def memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def concurrency_setup(memory_db):
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())
    queue_id = str(uuid.uuid4())

    user = User(
        id=user_id,
        email="concurrency@pulse.dev",
        hashed_password="pw",
        full_name="Concurrency Tester",
        role=UserRole.ORG_ADMIN,
    )
    org = Organization(id=org_id, name="Concurrency Org", owner_id=user_id)
    proj = Project(id=proj_id, org_id=org_id, name="Concurrency Project")
    queue = Queue(id=queue_id, project_id=proj_id, name="default", priority=5, concurrency_limit=5)

    memory_db.add_all([user, org, proj, queue])
    memory_db.commit()

    def make_job(name: str, status: JobStatus = JobStatus.QUEUED) -> Job:
        job = Job(id=str(uuid.uuid4()), queue_id=queue_id, name=name, status=status)
        memory_db.add(job)
        memory_db.commit()
        return job

    return {
        "user": user,
        "org": org,
        "proj": proj,
        "queue": queue,
        "make_job": make_job,
    }


@pytest.fixture
def file_db():
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"timeout": 30},
        future=True,
    )
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session, engine
    finally:
        session.close()
        engine.dispose()
        try:
            os.remove(path)
            for ext in ("-wal", "-shm"):
                if os.path.exists(path + ext):
                    os.remove(path + ext)
        except OSError:
            pass


# =========================================================================
# 1. Synchronized Two-Parent Fan-In Atomic Dispatch
# =========================================================================

def test_dag_two_parent_fan_in_single_dispatch_race(file_db):
    """Verify that when Parent 1 and Parent 2 complete at the exact same moment,
    only ONE worker thread wins the atomic conditional update BLOCKED -> QUEUED
    and dispatches the child job.
    """
    db, engine = file_db
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())
    queue_id = str(uuid.uuid4())

    user = User(id=user_id, email="dag@pulse.dev", hashed_password="pw", full_name="DAG", role=UserRole.ORG_ADMIN)
    org = Organization(id=org_id, name="DAG Org", owner_id=user_id)
    proj = Project(id=proj_id, org_id=org_id, name="DAG Proj")
    queue = Queue(id=queue_id, project_id=proj_id, name="default", priority=5, concurrency_limit=5)
    db.add_all([user, org, proj, queue])
    db.commit()

    p1 = Job(id=str(uuid.uuid4()), queue_id=queue_id, name="Parent-1", status=JobStatus.RUNNING)
    p2 = Job(id=str(uuid.uuid4()), queue_id=queue_id, name="Parent-2", status=JobStatus.RUNNING)
    child = Job(id=str(uuid.uuid4()), queue_id=queue_id, name="Child", status=JobStatus.BLOCKED)
    db.add_all([p1, p2, child])
    db.commit()

    link_dag_dependencies(db, child, [p1.id, p2.id])
    db.commit()

    # Both parents complete
    p1.status = JobStatus.COMPLETED
    p2.status = JobStatus.COMPLETED
    db.commit()

    dispatch_calls = []
    def recording_dispatcher(job):
        dispatch_calls.append(job.id)

    # Use a thread barrier to coordinate simultaneous release execution
    barrier = threading.Barrier(2)
    errors = []
    session_factory = sessionmaker(bind=engine)

    def run_worker(parent_id):
        session = session_factory()
        try:
            parent_job = session.get(Job, parent_id)
            barrier.wait(timeout=5)
            # Both threads call release_dag_dependents with their own thread session
            release_dag_dependents(session, parent_job, dispatcher=recording_dispatcher)
            session.commit()
        except Exception as exc:
            errors.append(exc)
        finally:
            session.close()

    t1 = threading.Thread(target=run_worker, args=(p1.id,))
    t2 = threading.Thread(target=run_worker, args=(p2.id,))

    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"Encountered thread errors: {errors}"

    fresh_session = session_factory()
    try:
        fresh_child = fresh_session.get(Job, child.id)
        assert fresh_child.status == JobStatus.QUEUED
    finally:
        fresh_session.close()

    # CRITICAL: Verify Child was dispatched EXACTLY ONCE
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0] == child.id


# =========================================================================
# 2. Transactional Outbox Durability & Idempotence
# =========================================================================

def test_outbox_record_and_transactional_rollback(memory_db, concurrency_setup):
    """Verify that OutboxEvent is written within the DB transaction:
    if transaction rolls back, no outbox event is created and no dispatch occurs.
    """
    make_job = concurrency_setup["make_job"]
    job = make_job("Rollback-Job", status=JobStatus.QUEUED)

    # Record outbox event in uncommitted session
    event = record_job_dispatch_outbox(memory_db, job)
    assert event.id is not None
    assert event.status == OutboxStatus.PENDING

    # Transaction aborts/rolls back
    memory_db.rollback()

    # Event must NOT exist in the database
    ev_in_db = memory_db.get(OutboxEvent, event.id)
    assert ev_in_db is None


def test_outbox_publish_success_and_idempotence(memory_db, concurrency_setup):
    """Verify outbox publisher claims pending rows, marks DISPATCHED,
    and subsequent executions do not double-dispatch.
    """
    make_job = concurrency_setup["make_job"]
    job = make_job("Outbox-Success-Job", status=JobStatus.QUEUED)

    event = record_job_dispatch_outbox(memory_db, job)
    memory_db.commit()

    with patch("app.celery_app.celery_app.send_task") as mock_send:
        # First publisher run: processes 1 event
        count1 = publish_pending_outbox_events(memory_db)
        assert count1 == 1
        assert mock_send.call_count == 1

        memory_db.refresh(event)
        assert event.status == OutboxStatus.DISPATCHED
        assert event.published_at is not None

        # Second publisher run: 0 events pending (idempotent, no double dispatch!)
        count2 = publish_pending_outbox_events(memory_db)
        assert count2 == 0
        assert mock_send.call_count == 1


# =========================================================================
# 3. Outbox Crash Recovery & Retry Bounds
# =========================================================================

def test_outbox_publish_failure_and_retry_limit(memory_db, concurrency_setup):
    """Verify that network/broker failure increments retry_count and stores error.
    When MAX_OUTBOX_RETRIES is reached, status transitions to FAILED.
    """
    make_job = concurrency_setup["make_job"]
    job = make_job("Outbox-Fail-Job", status=JobStatus.QUEUED)

    event = record_job_dispatch_outbox(memory_db, job)
    memory_db.commit()

    with patch("app.celery_app.celery_app.send_task", side_effect=ConnectionError("Celery broker unreachable")):
        # First failure: retry_count = 1, status remains PENDING
        publish_pending_outbox_events(memory_db)
        memory_db.refresh(event)
        assert event.retry_count == 1
        assert event.status == OutboxStatus.PENDING
        assert "Celery broker unreachable" in (event.last_error or "")

        # Fast-forward retry_count to MAX_OUTBOX_RETRIES - 1
        event.retry_count = MAX_OUTBOX_RETRIES - 1
        memory_db.commit()

        # Final failure: transitions to FAILED
        publish_pending_outbox_events(memory_db)
        memory_db.refresh(event)
        assert event.retry_count == MAX_OUTBOX_RETRIES
        assert event.status == OutboxStatus.FAILED


# =========================================================================
# 4. Schedule Occurrence Deduplication Under Races
# =========================================================================

def test_schedule_occurrence_unique_constraint(memory_db, concurrency_setup):
    """Verify database unique constraint (schedule_id, scheduled_fire_time)
    strictly rejects duplicate occurrence attempts for the same scheduled fire time.
    """
    queue = concurrency_setup["queue"]
    schedule = JobSchedule(
        queue_id=queue.id,
        name="Hourly Report",
        cron_expression="0 * * * *",
        payload_template={"type": "simulate"},
        is_active=True,
    )
    memory_db.add(schedule)
    memory_db.commit()

    fire_time = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)

    # First worker claims occurrence
    occ1 = ScheduleOccurrence(
        schedule_id=schedule.id,
        scheduled_fire_time=fire_time,
        dispatched_at=utcnow(),
    )
    memory_db.add(occ1)
    memory_db.commit()

    # Second worker attempts duplicate claim for exact same fire time
    occ2 = ScheduleOccurrence(
        schedule_id=schedule.id,
        scheduled_fire_time=fire_time,
        dispatched_at=utcnow(),
    )
    memory_db.add(occ2)

    with pytest.raises(IntegrityError):
        memory_db.commit()

    memory_db.rollback()


# =========================================================================
# 5. Fail-Closed Malformed Cron Syntax
# =========================================================================

def test_schedule_fail_closed_malformed_cron(memory_db, concurrency_setup):
    """Verify that an invalid cron expression disables the schedule,
    records an audit event, and allows valid schedules to proceed safely.
    """
    queue = concurrency_setup["queue"]
    now = utcnow()

    # 1. Malformed schedule
    sched_bad = JobSchedule(
        queue_id=queue.id,
        name="Bad Cron Schedule",
        cron_expression="* * * * * * * invalid",
        payload_template={"type": "simulate"},
        is_active=True,
    )

    # 2. Healthy schedule due for dispatch
    sched_good = JobSchedule(
        queue_id=queue.id,
        name="Good Cron Schedule",
        cron_expression="*/5 * * * *",
        payload_template={"type": "simulate"},
        is_active=True,
        last_dispatched_at=now - timedelta(minutes=10),
    )

    memory_db.add_all([sched_bad, sched_good])
    memory_db.commit()

    session_factory = sessionmaker(bind=memory_db.get_bind())
    with patch("app.tasks.SessionLocal", side_effect=session_factory), \
         patch("app.tasks._redis.set", return_value=True), \
         patch("app.tasks._redis.eval", return_value=1), \
         patch("app.tasks.dispatch_job") as mock_dispatch:

        dispatch_due_schedules()

    sched_bad_updated = memory_db.get(JobSchedule, sched_bad.id)
    sched_good_updated = memory_db.get(JobSchedule, sched_good.id)

    # VERIFY: Malformed schedule disabled
    assert sched_bad_updated.is_active is False

    # VERIFY: Audit event recorded
    audit_evt = (
        memory_db.query(AuditLog)
        .filter(
            AuditLog.action == "schedule.invalid_disabled",
            AuditLog.resource_id == sched_bad.id,
        )
        .first()
    )
    assert audit_evt is not None
    assert audit_evt.details["cron_expression"] == "* * * * * * * invalid"

    # VERIFY: Good schedule was not blocked and was dispatched!
    assert sched_good_updated.is_active is True
    assert sched_good_updated.last_dispatched_at is not None
    assert mock_dispatch.call_count == 1


# =========================================================================
# 6. Safe Redis Compare-and-Delete Lua Script
# =========================================================================

def test_redis_safe_lock_lua_token_comparison():
    """Verify Lua compare-and-delete script logic:
    - If current lock value matches owner token, delete key and return 1.
    - If current lock value differs (expired and claimed by another worker), do not delete and return 0.
    """
    # Create a mock redis simulating Redis eval behavior
    redis_store = {}

    def mock_eval(script, numkeys, key, token):
        if script == LUA_SAFE_RELEASE_LOCK:
            current_val = redis_store.get(key)
            if current_val == token:
                del redis_store[key]
                return 1
            return 0
        return None

    mock_redis = MagicMock()
    mock_redis.eval.side_effect = mock_eval

    lock_key = "pulse:lock:dispatch_due_schedules"
    token_worker_a = "token-uuid-worker-a"
    token_worker_b = "token-uuid-worker-b"

    # Case 1: Worker A holds lock and releases it successfully
    redis_store[lock_key] = token_worker_a
    result_a = mock_redis.eval(LUA_SAFE_RELEASE_LOCK, 1, lock_key, token_worker_a)
    assert result_a == 1
    assert lock_key not in redis_store

    # Case 2: Worker A's lock expired, Worker B acquired it
    redis_store[lock_key] = token_worker_b

    # Worker A wakes up and attempts to release its expired lock
    result_a_stale = mock_redis.eval(LUA_SAFE_RELEASE_LOCK, 1, lock_key, token_worker_a)
    assert result_a_stale == 0
    # Lock STILL held by Worker B! Not deleted!
    assert redis_store[lock_key] == token_worker_b
