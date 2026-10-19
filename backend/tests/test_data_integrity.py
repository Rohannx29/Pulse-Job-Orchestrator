import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import (
    AuditLog,
    Job,
    JobStatus,
    Organization,
    OrganizationMember,
    OrgMemberRole,
    Project,
    Queue,
    RetryPolicy,
    RetryStrategy,
    User,
    UserRole,
)
from app.security import create_access_token, hash_password


@pytest.fixture
def integrity_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def integrity_client(integrity_db):
    def override_get_db():
        yield integrity_db

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def _create_tenant(db, prefix="tenant1"):
    user = User(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        hashed_password=hash_password("Password123!"),
        role=UserRole.ORG_ADMIN,
    )
    db.add(user)
    db.flush()

    org = Organization(name=f"{prefix}-org", owner_id=user.id)
    db.add(org)
    db.flush()

    member = OrganizationMember(org_id=org.id, user_id=user.id, role=OrgMemberRole.ADMIN)
    db.add(member)
    db.flush()

    project = Project(name=f"{prefix}-project", org_id=org.id)
    db.add(project)
    db.flush()

    queue = Queue(name=f"{prefix}-queue", project_id=project.id, concurrency_limit=5)
    db.add(queue)
    db.flush()

    retry_policy = RetryPolicy(
        queue_id=queue.id,
        name=f"{prefix}-policy",
        strategy=RetryStrategy.EXPONENTIAL,
        max_retries=3,
        base_delay_seconds=5,
    )
    db.add(retry_policy)
    db.commit()
    return user, org, project, queue, retry_policy


def _auth_client(client: TestClient, user: User):
    token = create_access_token(str(user.id))
    client.cookies.set(settings.ACCESS_TOKEN_COOKIE_NAME, token)
    client.headers.update({"Origin": "http://localhost:3000"})


def test_cross_tenant_retry_policy_rejected(integrity_client, integrity_db):
    user_a, org_a, proj_a, queue_a, policy_a = _create_tenant(integrity_db, "tenant-a")
    user_b, org_b, proj_b, queue_b, policy_b = _create_tenant(integrity_db, "tenant-b")

    _auth_client(integrity_client, user_a)

    # Attempt to create job in Queue A using Tenant B's retry policy
    resp = integrity_client.post(
        f"/projects/{proj_a.id}/queues/{queue_a.id}/jobs",
        json={
            "name": "malicious-cross-tenant-job",
            "retry_policy_id": policy_b.id,
            "payload": {"key": "val"},
        },
    )
    assert resp.status_code == 400
    assert "Retry policy" in resp.json()["detail"]
    assert "not found or does not belong to this queue" in resp.json()["detail"]

    # Attempt to create batch jobs with Tenant B's retry policy
    resp_batch = integrity_client.post(
        f"/projects/{proj_a.id}/queues/{queue_a.id}/jobs/batch",
        json={
            "jobs": [
                {
                    "name": "batch-job-cross-tenant",
                    "retry_policy_id": policy_b.id,
                    "payload": {"key": "val"},
                }
            ]
        },
    )
    assert resp_batch.status_code == 400
    assert "Retry policy" in resp_batch.json()["detail"]


def test_job_payload_size_limit(integrity_client, integrity_db):
    user, org, proj, queue, policy = _create_tenant(integrity_db, "limits")
    _auth_client(integrity_client, user)

    # 1. Huge payload exceeding 256 KB
    huge_payload = {"data": "A" * (270 * 1024)}
    resp = integrity_client.post(
        f"/projects/{proj.id}/queues/{queue.id}/jobs",
        json={"name": "oversized-job", "payload": huge_payload},
    )
    assert resp.status_code == 422
    assert "exceeds maximum limit of 262144 bytes" in resp.text

    # 2. HTTP job URL exceeding 2048 chars
    resp = integrity_client.post(
        f"/projects/{proj.id}/queues/{queue.id}/jobs",
        json={
            "name": "bad-url-job",
            "payload": {
                "type": "http",
                "url": "https://example.com/" + "a" * 2050,
            },
        },
    )
    assert resp.status_code == 422
    assert "HTTP job url exceeds 2048 characters limit" in resp.text

    # 3. HTTP job headers exceeding 50 entries
    headers = {f"X-Header-{i}": "val" for i in range(55)}
    resp = integrity_client.post(
        f"/projects/{proj.id}/queues/{queue.id}/jobs",
        json={
            "name": "bad-headers-job",
            "payload": {
                "type": "http",
                "url": "https://example.com/webhook",
                "headers": headers,
            },
        },
    )
    assert resp.status_code == 422
    assert "HTTP job headers cannot exceed 50 entries" in resp.text


def test_batch_size_limit(integrity_client, integrity_db):
    user, org, proj, queue, policy = _create_tenant(integrity_db, "batch-limit")
    _auth_client(integrity_client, user)

    # Create batch with 101 jobs (max allowed is 100)
    jobs = [{"name": f"job-{i}", "payload": {}} for i in range(101)]
    resp = integrity_client.post(
        f"/projects/{proj.id}/queues/{queue.id}/jobs/batch",
        json={"jobs": jobs},
    )
    assert resp.status_code == 422


def test_entity_field_limits(integrity_client, integrity_db):
    user, org, proj, queue, policy = _create_tenant(integrity_db, "entity-limits")
    _auth_client(integrity_client, user)

    # Project name > 120 chars
    resp = integrity_client.post(
        f"/projects",
        json={"name": "P" * 125, "description": "desc"},
    )
    assert resp.status_code == 422

    # Queue name > 120 chars
    resp = integrity_client.post(
        f"/projects/{proj.id}/queues",
        json={"name": "Q" * 125, "concurrency_limit": 5},
    )
    assert resp.status_code == 422


def test_cursor_pagination_jobs(integrity_client, integrity_db):
    user, org, proj, queue, policy = _create_tenant(integrity_db, "cursor-jobs")
    _auth_client(integrity_client, user)

    # Insert 5 jobs directly into DB with sequential created_at
    jobs = []
    for i in range(5):
        j = Job(
            id=str(uuid.uuid4()),
            queue_id=queue.id,
            name=f"job-{i}",
            status=JobStatus.QUEUED,
            created_at=datetime(2026, 1, 1, 10, i, 0, tzinfo=timezone.utc),
        )
        integrity_db.add(j)
        jobs.append(j)
    integrity_db.commit()

    # Page 1: limit = 2
    resp1 = integrity_client.get(f"/projects/{proj.id}/queues/{queue.id}/jobs?limit=2")
    assert resp1.status_code == 200
    page1 = resp1.json()
    assert len(page1) == 2
    assert resp1.headers.get("X-Has-More") == "true"
    next_cursor = resp1.headers.get("X-Next-Cursor")
    assert next_cursor is not None

    # Page 2: with cursor
    resp2 = integrity_client.get(
        f"/projects/{proj.id}/queues/{queue.id}/jobs?limit=2&cursor={next_cursor}"
    )
    assert resp2.status_code == 200
    page2 = resp2.json()
    assert len(page2) == 2
    assert resp2.headers.get("X-Has-More") == "true"
    next_cursor2 = resp2.headers.get("X-Next-Cursor")

    # IDs between page 1 and page 2 must be disjoint
    ids_p1 = {j["id"] for j in page1}
    ids_p2 = {j["id"] for j in page2}
    assert ids_p1.isdisjoint(ids_p2)

    # Page 3: remaining 1 job
    resp3 = integrity_client.get(
        f"/projects/{proj.id}/queues/{queue.id}/jobs?limit=2&cursor={next_cursor2}"
    )
    assert resp3.status_code == 200
    page3 = resp3.json()
    assert len(page3) == 1
    assert resp3.headers.get("X-Has-More") == "false"
    assert resp3.headers.get("X-Next-Cursor") is None


def test_cursor_pagination_audit_logs(integrity_client, integrity_db):
    user, org, proj, queue, policy = _create_tenant(integrity_db, "cursor-audit")
    _auth_client(integrity_client, user)

    # Insert 4 audit logs
    for i in range(4):
        log = AuditLog(
            id=str(uuid.uuid4()),
            org_id=org.id,
            user_id=user.id,
            action="test_action",
            resource_type="queue",
            resource_id=queue.id,
            details={"step": i},
            created_at=datetime(2026, 1, 1, 12, i, 0, tzinfo=timezone.utc),
        )
        integrity_db.add(log)
    integrity_db.commit()

    resp = integrity_client.get("/audit-logs?limit=2")
    assert resp.status_code == 200
    logs1 = resp.json()
    assert len(logs1) == 2
    assert resp.headers.get("X-Has-More") == "true"
    cursor = resp.headers.get("X-Next-Cursor")
    assert cursor is not None

    resp2 = integrity_client.get(f"/audit-logs?limit=2&cursor={cursor}")
    assert resp2.status_code == 200
    logs2 = resp2.json()
    assert len(logs2) == 2
    assert resp2.headers.get("X-Has-More") == "false"
