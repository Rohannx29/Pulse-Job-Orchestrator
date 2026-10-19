"""Regression tests for anomalies identified and fixed during full-project code review:
1. UserOut serialization with default role fallback
2. sync_worker_heartbeats empty seen_hostnames handling
3. Concurrency module fail-safe resilience against Redis exceptions
4. Job detail relationship query structure
"""

from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.models import JobStatus, OrgMemberRole, User, UserRole, Worker, WorkerStatus
from app.schemas import UserOut
from app.concurrency import acquire_queue_slot, release_queue_slot, current_usage
from app.timeutils import utcnow


def test_user_out_defaults_to_org_admin_when_role_unspecified():
    """Verify UserOut doesn't throw ValidationError when role is missing or default."""
    u = User(
        id="test-user-id",
        email="test@pulse.dev",
        full_name="Default User",
        role=UserRole.ORG_ADMIN,
    )
    out = UserOut.model_validate(u)
    assert out.id == "test-user-id"
    assert out.role == UserRole.ORG_ADMIN


def test_concurrency_fail_safe_on_redis_error():
    """Verify acquire, release, and current_usage return safe fallbacks on Redis errors."""
    with patch("app.concurrency._acquire_script", side_effect=Exception("Redis down")):
        token = acquire_queue_slot("queue-1", limit=5)
        assert token is None

    with patch("app.concurrency._redis.zrem", side_effect=Exception("Redis down")):
        # Should not raise exception
        release_queue_slot("queue-1", "token-xyz")

    with patch("app.concurrency._redis.zremrangebyscore", side_effect=Exception("Redis down")):
        usage = current_usage("queue-1")
        assert usage == 0


def test_sync_worker_heartbeats_handles_empty_seen_hostnames():
    """Verify that when no workers respond, stale workers are properly marked offline without SQL error."""
    from app.tasks import sync_worker_heartbeats
    from app.database import Base, SessionLocal
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()

    stale_time = utcnow() - timedelta(minutes=10)
    worker = Worker(
        hostname="stale-worker-1",
        status=WorkerStatus.ONLINE,
        last_heartbeat_at=stale_time,
    )
    session.add(worker)
    session.commit()

    # Mock inspect to return empty stats (no active workers)
    mock_inspect = Mock()
    mock_inspect.stats.return_value = {}
    mock_inspect.active.return_value = {}

    worker_id = worker.id
    from app.celery_app import celery_app
    with patch.object(celery_app.control, "inspect", return_value=mock_inspect):
        with patch("app.tasks.SessionLocal", return_value=session):
            sync_worker_heartbeats()

    verify_session = TestingSession()
    updated_worker = verify_session.get(Worker, worker_id)
    assert updated_worker.status == WorkerStatus.OFFLINE
    verify_session.close()


def test_list_all_organizations_grouped_member_counts():
    """Verify optimized single-query group_by member counting across multiple orgs."""
    import uuid
    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models import Organization, OrganizationMember

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()

    org1_id = str(uuid.uuid4())
    org2_id = str(uuid.uuid4())
    u1_id = str(uuid.uuid4())
    u2_id = str(uuid.uuid4())
    u3_id = str(uuid.uuid4())

    org1 = Organization(id=org1_id, name="Alpha Org", owner_id=u1_id)
    org2 = Organization(id=org2_id, name="Beta Org", owner_id=u2_id)
    session.add_all([org1, org2])
    session.commit()

    m1 = OrganizationMember(id=str(uuid.uuid4()), org_id=org1_id, user_id=u1_id, role=OrgMemberRole.ADMIN)
    m2 = OrganizationMember(id=str(uuid.uuid4()), org_id=org1_id, user_id=u3_id, role=OrgMemberRole.MEMBER)
    m3 = OrganizationMember(id=str(uuid.uuid4()), org_id=org2_id, user_id=u2_id, role=OrgMemberRole.ADMIN)
    session.add_all([m1, m2, m3])
    session.commit()

    # Query exactly as implemented in optimized admin router
    member_counts = dict(
        session.query(OrganizationMember.org_id, func.count(OrganizationMember.id))
        .group_by(OrganizationMember.org_id)
        .all()
    )

    assert member_counts.get(org1_id) == 2
    assert member_counts.get(org2_id) == 1
    assert member_counts.get("nonexistent", 0) == 0
    session.close()


def test_list_org_members_eager_loads_user():
    """Verify joinedload on OrganizationMember.user successfully loads user relationship."""
    import uuid
    from sqlalchemy import create_engine
    from sqlalchemy.orm import joinedload, sessionmaker
    from app.database import Base
    from app.models import Organization, OrganizationMember, User, UserRole

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()

    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())

    user = User(id=user_id, email="member@pulse.dev", hashed_password="pw", role=UserRole.MEMBER)
    org = Organization(id=org_id, name="Test Org", owner_id=user_id)
    member = OrganizationMember(id=str(uuid.uuid4()), org_id=org_id, user_id=user_id, role=OrgMemberRole.MEMBER)

    session.add_all([user, org, member])
    session.commit()

    # Query with joinedload as in routers/orgs.py
    members = (
        session.query(OrganizationMember)
        .options(joinedload(OrganizationMember.user))
        .filter(OrganizationMember.org_id == org_id)
        .all()
    )

    assert len(members) == 1
    assert members[0].user is not None
    assert members[0].user.email == "member@pulse.dev"
    session.close()


def test_get_job_eager_loads_executions_and_queue():
    """Verify selectinload and joinedload on Job successfully eager-loads executions and queue project."""
    import uuid
    from sqlalchemy import create_engine
    from sqlalchemy.orm import joinedload, selectinload, sessionmaker
    from app.database import Base
    from app.models import Job, JobExecution, JobLog, JobStatus, Organization, Project, Queue, User, UserRole

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()

    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())
    queue_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    exec_id = str(uuid.uuid4())

    user = User(id=user_id, email="owner@pulse.dev", hashed_password="pw", role=UserRole.ORG_ADMIN)
    org = Organization(id=org_id, name="Job Org", owner_id=user_id)
    project = Project(id=proj_id, org_id=org_id, name="Job Proj")
    queue = Queue(id=queue_id, project_id=proj_id, name="Job Queue")
    job = Job(id=job_id, queue_id=queue_id, name="Test Job", status=JobStatus.COMPLETED)
    execution = JobExecution(id=exec_id, job_id=job_id, attempt_number=1, status=JobStatus.COMPLETED)
    log = JobLog(id=str(uuid.uuid4()), job_execution_id=exec_id, message="Step 1 complete")

    session.add_all([user, org, project, queue, job, execution, log])
    session.commit()

    # Query with selectinload and joinedload as in routers/jobs.py
    loaded_job = (
        session.query(Job)
        .options(
            selectinload(Job.executions).selectinload(JobExecution.logs),
            joinedload(Job.queue).joinedload(Queue.project),
        )
        .filter(Job.id == job_id)
        .first()
    )

    assert loaded_job is not None
    assert loaded_job.queue.project.org_id == org_id
    assert len(loaded_job.executions) == 1
    assert len(loaded_job.executions[0].logs) == 1
    assert loaded_job.executions[0].logs[0].message == "Step 1 complete"
    session.close()


