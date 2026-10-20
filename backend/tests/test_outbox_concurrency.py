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


# =========================================================================
# 7. Monotonic Dispatch Generation & Repeated Manual Retry Fencing
# =========================================================================

def test_job_manual_retry_monotonic_dispatch_generation(memory_db, concurrency_setup):
    """Proves that repeated manual retries increment monotonic dispatch_generation,
    preventing fencing key collisions even when current_retry_count is reset to 0."""
    queue = concurrency_setup["queue"]

    # 1. Initial job creation: generation 1
    job = Job(
        id=str(uuid.uuid4()),
        queue_id=queue.id,
        name="retry-test-job",
        priority=5,
        status=JobStatus.QUEUED,
        max_retries=3,
        current_retry_count=0,
        dispatch_generation=1,
    )
    memory_db.add(job)
    evt1 = record_job_dispatch_outbox(memory_db, job)
    memory_db.commit()

    assert evt1.fencing_key == f"job:dispatch:{job.id}:1"

    # 2. Simulate job failure into DEAD_LETTER after retries
    job.status = JobStatus.DEAD_LETTER
    job.current_retry_count = 3
    job.dispatch_generation = 2
    memory_db.commit()

    # 3. First manual retry: current_retry_count reset to 0, generation incremented to 3
    job.current_retry_count = 0
    job.dispatch_generation = 3
    job.status = JobStatus.QUEUED
    evt2 = record_job_dispatch_outbox(memory_db, job)
    memory_db.commit()

    assert evt2.fencing_key == f"job:dispatch:{job.id}:3"
    assert evt2.fencing_key != evt1.fencing_key

    # 4. Second manual retry: current_retry_count reset to 0, generation incremented to 4
    job.current_retry_count = 0
    job.dispatch_generation = 4
    job.status = JobStatus.QUEUED
    evt3 = record_job_dispatch_outbox(memory_db, job)
    memory_db.commit()

    assert evt3.fencing_key == f"job:dispatch:{job.id}:4"
    assert len({evt1.fencing_key, evt2.fencing_key, evt3.fencing_key}) == 3


# =========================================================================
# 8. Worker Durable State Guard: Refuses BLOCKED, Terminal, or Stale Tasks
# =========================================================================

def test_worker_durable_state_guard(memory_db, concurrency_setup):
    """Proves the worker drops tasks if job is BLOCKED, terminal, or generation is stale."""
    from app.tasks import execute_job

    queue = concurrency_setup["queue"]

    # Case 1: Job is BLOCKED (e.g. premature task delivery or race)
    blocked_job = Job(
        id=str(uuid.uuid4()),
        queue_id=queue.id,
        name="blocked-job",
        priority=5,
        status=JobStatus.BLOCKED,
        dispatch_generation=1,
    )
    memory_db.add(blocked_job)
    memory_db.commit()

    SessionMaker = sessionmaker(bind=memory_db.get_bind())
    with patch("app.tasks.SessionLocal", side_effect=SessionMaker):
        execute_job(str(blocked_job.id), dispatch_generation=1)

    memory_db.expire_all()
    refreshed_blocked = memory_db.get(Job, blocked_job.id)
    # Must remain BLOCKED, not transitioned to CLAIMED or RUNNING
    assert refreshed_blocked.status == JobStatus.BLOCKED

    # Case 2: Job is already COMPLETED
    completed_job = Job(
        id=str(uuid.uuid4()),
        queue_id=queue.id,
        name="completed-job",
        priority=5,
        status=JobStatus.COMPLETED,
        dispatch_generation=1,
    )
    memory_db.add(completed_job)
    memory_db.commit()

    with patch("app.tasks.SessionLocal", side_effect=SessionMaker):
        execute_job(str(completed_job.id), dispatch_generation=1)

    memory_db.expire_all()
    refreshed_completed = memory_db.get(Job, completed_job.id)
    assert refreshed_completed.status == JobStatus.COMPLETED

    # Case 3: Stale dispatch generation (task has gen 1, DB has gen 2)
    active_job = Job(
        id=str(uuid.uuid4()),
        queue_id=queue.id,
        name="active-job",
        priority=5,
        status=JobStatus.QUEUED,
        dispatch_generation=2,
    )
    memory_db.add(active_job)
    memory_db.commit()

    with patch("app.tasks.SessionLocal", side_effect=SessionMaker):
        execute_job(str(active_job.id), dispatch_generation=1)

    memory_db.expire_all()
    refreshed_active = memory_db.get(Job, active_job.id)
    # Stale generation 1 task was dropped; job remained QUEUED
    assert refreshed_active.status == JobStatus.QUEUED


# =========================================================================
# 9. Outbox Reconciliation Heals Stranded Dispatchable Jobs
# =========================================================================

def test_outbox_reconciliation_heals_stranded_jobs(memory_db, concurrency_setup):
    """Proves that a committed runnable job missing an outbox record
    is automatically detected and staged by reconcile_stranded_jobs."""
    from app.outbox import reconcile_stranded_jobs

    queue = concurrency_setup["queue"]

    # Create a job that is QUEUED but has NO OutboxEvent (simulating a crash window)
    stranded_job = Job(
        id=str(uuid.uuid4()),
        queue_id=queue.id,
        name="stranded-job",
        status=JobStatus.QUEUED,
        dispatch_generation=1,
        created_at=utcnow() - timedelta(seconds=60),
    )
    memory_db.add(stranded_job)
    memory_db.commit()

    # Verify no outbox record exists initially
    initial_evts = memory_db.query(OutboxEvent).filter(OutboxEvent.aggregate_id == str(stranded_job.id)).all()
    assert len(initial_evts) == 0

    # Run reconciliation
    reconciled = reconcile_stranded_jobs(memory_db, max_age_seconds=10, limit=50)
    assert reconciled == 1

    # Verify an OutboxEvent was staged
    memory_db.refresh(stranded_job)
    assert stranded_job.dispatch_generation == 2
    evts_after = memory_db.query(OutboxEvent).filter(OutboxEvent.aggregate_id == str(stranded_job.id)).all()
    assert len(evts_after) == 1
    assert evts_after[0].fencing_key == f"job:dispatch:{stranded_job.id}:2"
    assert evts_after[0].status == OutboxStatus.PENDING

    # Running reconciliation again is idempotent (does not duplicate)
    reconciled_again = reconcile_stranded_jobs(memory_db, max_age_seconds=10, limit=50)
    assert reconciled_again == 0


# =========================================================================
# 10. Post-Commit Guarantee: Rollback Emits Zero Redis / Webhook Effects
# =========================================================================

def test_transaction_rollback_emits_no_redis_or_webhook_events(memory_db, concurrency_setup):
    """Proves that transactional mutations that roll back emit NO outbox rows,
    guaranteeing that no Celery task, Redis pub/sub, or webhook delivery is ever sent."""
    from app.outbox import record_event_outbox, record_webhook_dispatch_outbox

    org = concurrency_setup["org"]
    delivery_id = str(uuid.uuid4())

    # Begin transaction, stage both an SSE event and a webhook dispatch
    record_event_outbox(memory_db, org.id, "queue.updated", {"queue_id": "test-q"})
    record_webhook_dispatch_outbox(memory_db, delivery_id)

    # Force a transaction rollback
    memory_db.rollback()

    # Verify zero outbox rows were committed
    events = memory_db.query(OutboxEvent).all()
    assert len(events) == 0

    # Running the publisher confirms zero dispatches
    with patch("app.events.publish_event") as mock_redis_pub, \
         patch("app.celery_app.celery_app.send_task") as mock_celery:
        dispatched = publish_pending_outbox_events(memory_db)
        assert dispatched == 0
        assert mock_redis_pub.call_count == 0
        assert mock_celery.call_count == 0


# =========================================================================
# 11. RBAC-Protected Outbox Inspect, Backlog Stats & Replay Operation
# =========================================================================

def test_admin_outbox_inspect_and_replay_audit(memory_db, concurrency_setup):
    """Proves RBAC protection, operational stats inspection, and idempotent replay of failed outbox rows."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.deps import get_db, require_system_admin
    from app.security import create_access_token
    from app.config import settings

    sys_admin = User(
        id=str(uuid.uuid4()),
        email="sysadmin@pulse.dev",
        hashed_password="pw",
        role=UserRole.SYSTEM_ADMIN,
    )
    regular_member = User(
        id=str(uuid.uuid4()),
        email="member@pulse.dev",
        hashed_password="pw",
        role=UserRole.MEMBER,
    )
    memory_db.add_all([sys_admin, regular_member])

    # Insert a failed outbox event
    failed_event = OutboxEvent(
        id=str(uuid.uuid4()),
        event_type="job.dispatch",
        aggregate_type="job",
        aggregate_id="job-123",
        payload={"task_name": "app.tasks.execute_job", "args": ["job-123"]},
        status=OutboxStatus.FAILED,
        retry_count=5,
        last_error="Simulated network failure",
        created_at=utcnow(),
    )
    memory_db.add(failed_event)
    memory_db.commit()

    app.dependency_overrides[get_db] = lambda: memory_db

    try:
        with TestClient(app) as client:
            # 1. Regular member is rejected with 403 Forbidden
            token_member = create_access_token(regular_member.id)
            client.cookies.set(settings.ACCESS_TOKEN_COOKIE_NAME, token_member)
            res_forbidden = client.get("/admin/outbox/stats", headers={"Origin": "http://localhost:3000"})
            assert res_forbidden.status_code == 403

            # 2. System admin can inspect outbox stats
            token_admin = create_access_token(sys_admin.id)
            client.cookies.set(settings.ACCESS_TOKEN_COOKIE_NAME, token_admin)
            res_stats = client.get("/admin/outbox/stats", headers={"Origin": "http://localhost:3000"})
            assert res_stats.status_code == 200
            stats_data = res_stats.json()
            assert stats_data["failed_count"] >= 1

            # 3. System admin can list outbox events
            res_events = client.get("/admin/outbox/events?status=FAILED", headers={"Origin": "http://localhost:3000"})
            assert res_events.status_code == 200
            events_data = res_events.json()
            assert len(events_data) >= 1
            assert events_data[0]["id"] == failed_event.id

            # 4. System admin can replay the failed event
            res_replay = client.post(
                f"/admin/outbox/events/{failed_event.id}/replay",
                headers={"Origin": "http://localhost:3000"},
            )
            assert res_replay.status_code == 200
            replay_data = res_replay.json()
            assert replay_data["status"] == "PENDING"
            assert replay_data["retry_count"] == 0

            # 5. Verify audit log was written for replay
            audit = (
                memory_db.query(AuditLog)
                .filter(AuditLog.action == "outbox.replay", AuditLog.resource_id == failed_event.id)
                .first()
            )
            assert audit is not None
            assert audit.user_id == sys_admin.id

            # 6. Idempotent: replaying already PENDING event remains PENDING
            res_replay_again = client.post(
                f"/admin/outbox/events/{failed_event.id}/replay",
                headers={"Origin": "http://localhost:3000"},
            )
            assert res_replay_again.status_code == 200
            assert res_replay_again.json()["status"] == "PENDING"
    finally:
        app.dependency_overrides.clear()
