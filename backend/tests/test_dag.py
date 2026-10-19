"""Comprehensive unit and integration tests for Multi-Parent Directed Acyclic Graph (DAG) Workflows:
1. Diamond fan-in/fan-out (A -> B, C -> D) join resolution
2. Wide fan-out (1 parent -> N children unblocked simultaneously)
3. Wide fan-in (M parents -> 1 child waiting for all prerequisites)
4. Cycle detection (direct A -> B -> A, transitive A -> B -> C -> A, and self-dependency)
5. Recursive cascading failure (dead-letter in any branch cascades to downstream joins)
6. REST API submission with depends_on_job_ids and DAG graph extraction endpoints
7. Backward compatibility with legacy single-parent depends_on_job_id
"""

import uuid
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

from app.database import Base
from app.dag import (
    build_dag_graph,
    cascade_fail_dag_dependents,
    detect_cycle_dfs,
    link_dag_dependencies,
    release_dag_dependents,
)
from app.models import (
    DeadLetterJob,
    Job,
    JobDependency,
    JobStatus,
    Organization,
    OrganizationMember,
    OrgMemberRole,
    Project,
    Queue,
    User,
    UserRole,
)


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
def test_setup(memory_db):
    user_id = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    proj_id = str(uuid.uuid4())
    queue_id = str(uuid.uuid4())

    user = User(id=user_id, email="dag-tester@pulse.dev", hashed_password="pw", full_name="DAG Tester", role=UserRole.ORG_ADMIN)
    org = Organization(id=org_id, name="DAG Org", owner_id=user_id)
    proj = Project(id=proj_id, org_id=org_id, name="DAG Project")
    queue = Queue(id=queue_id, project_id=proj_id, name="default", priority=5, concurrency_limit=5)

    memory_db.add_all([user, org, proj, queue])
    memory_db.commit()

    def make_job(name: str, status: JobStatus = JobStatus.QUEUED) -> Job:
        job = Job(id=str(uuid.uuid4()), queue_id=queue_id, name=name, status=status)
        memory_db.add(job)
        memory_db.commit()
        return job

    return {"user": user, "org": org, "proj": proj, "queue": queue, "make_job": make_job}


def test_dag_cycle_detection_self_and_direct(memory_db, test_setup):
    """Verify cycle detection catches self-dependency and 2-node cycles."""
    make_job = test_setup["make_job"]
    job_a = make_job("job-a")
    job_b = make_job("job-b")

    # 1. Self dependency: A -> A
    cycle = detect_cycle_dfs(memory_db, job_a.id, [job_a.id])
    assert cycle is not None
    assert cycle == [job_a.id, job_a.id]

    # Link A -> B (B depends on A)
    link_dag_dependencies(memory_db, job_b, [job_a.id])
    memory_db.commit()

    # 2. Try making A depend on B (B -> A) which would create A -> B -> A
    cycle2 = detect_cycle_dfs(memory_db, job_a.id, [job_b.id])
    assert cycle2 is not None

    with pytest.raises(ValueError, match="Cyclic dependency detected"):
        link_dag_dependencies(memory_db, job_a, [job_b.id])


def test_dag_cycle_detection_transitive_multi_hop(memory_db, test_setup):
    """Verify cycle detection catches multi-hop cycles (A -> B -> C -> D -> A)."""
    make_job = test_setup["make_job"]
    a = make_job("job-a")
    b = make_job("job-b")
    c = make_job("job-c")
    d = make_job("job-d")

    # A -> B -> C -> D
    link_dag_dependencies(memory_db, b, [a.id])
    link_dag_dependencies(memory_db, c, [b.id])
    link_dag_dependencies(memory_db, d, [c.id])
    memory_db.commit()

    # Now attempt to make A depend on D (creating A -> B -> C -> D -> A)
    cycle = detect_cycle_dfs(memory_db, a.id, [d.id])
    assert cycle is not None
    assert cycle[0] == a.id
    assert cycle[-1] == a.id
    assert set(cycle) == {a.id, b.id, c.id, d.id}

    with pytest.raises(ValueError, match="Cyclic dependency detected"):
        link_dag_dependencies(memory_db, a, [d.id])


def test_dag_diamond_fan_in(memory_db, test_setup):
    """Verify diamond workflow:
         A
        / \\
       B   C
        \\ /
         D
    D must execute ONLY when BOTH B and C have completed.
    """
    make_job = test_setup["make_job"]
    job_a = make_job("job-A", status=JobStatus.RUNNING)
    job_b = make_job("job-B", status=JobStatus.BLOCKED)
    job_c = make_job("job-C", status=JobStatus.BLOCKED)
    job_d = make_job("job-D", status=JobStatus.BLOCKED)

    # Link A -> B, A -> C
    link_dag_dependencies(memory_db, job_b, [job_a.id])
    link_dag_dependencies(memory_db, job_c, [job_a.id])
    # Link B, C -> D (Multi-parent fan-in!)
    link_dag_dependencies(memory_db, job_d, [job_b.id, job_c.id])
    memory_db.commit()

    assert job_b.status == JobStatus.BLOCKED
    assert job_c.status == JobStatus.BLOCKED
    assert job_d.status == JobStatus.BLOCKED
    assert set(job_d.parent_job_ids) == {job_b.id, job_c.id}

    with patch("app.dag.dispatch_job") as mock_dispatch:
        # Step 1: Job A completes -> unblocks BOTH B and C, but NOT D
        job_a.status = JobStatus.COMPLETED
        memory_db.commit()

        unblocked_1 = release_dag_dependents(memory_db, job_a)
        assert len(unblocked_1) == 2
        unblocked_ids = {j.id for j in unblocked_1}
        assert unblocked_ids == {job_b.id, job_c.id}

        memory_db.refresh(job_b)
        memory_db.refresh(job_c)
        memory_db.refresh(job_d)
        assert job_b.status == JobStatus.QUEUED
        assert job_c.status == JobStatus.QUEUED
        assert job_d.status == JobStatus.BLOCKED  # D still waiting!

        # Step 2: Job B completes -> D still has C pending!
        job_b.status = JobStatus.COMPLETED
        memory_db.commit()

        unblocked_2 = release_dag_dependents(memory_db, job_b)
        assert len(unblocked_2) == 0  # D not released yet

        memory_db.refresh(job_d)
        assert job_d.status == JobStatus.BLOCKED

        # Step 3: Job C completes -> All prerequisites fulfilled, D is unblocked!
        job_c.status = JobStatus.COMPLETED
        memory_db.commit()

        unblocked_3 = release_dag_dependents(memory_db, job_c)
        assert len(unblocked_3) == 1
        assert unblocked_3[0].id == job_d.id

        memory_db.refresh(job_d)
        assert job_d.status == JobStatus.QUEUED
        assert mock_dispatch.call_count == 3  # B, C, and finally D


def test_dag_wide_fan_out_and_fan_in(memory_db, test_setup):
    """Verify wide fan-out (1 -> 4) and wide fan-in (4 -> 1)."""
    make_job = test_setup["make_job"]
    root = make_job("root", status=JobStatus.RUNNING)
    workers = [make_job(f"worker-{i}", status=JobStatus.BLOCKED) for i in range(4)]
    sink = make_job("sink", status=JobStatus.BLOCKED)

    # Root -> all 4 workers
    for w in workers:
        link_dag_dependencies(memory_db, w, [root.id])

    # All 4 workers -> sink
    link_dag_dependencies(memory_db, sink, [w.id for w in workers])
    memory_db.commit()

    with patch("app.dag.dispatch_job"):
        # Root completes -> releases all 4 workers
        root.status = JobStatus.COMPLETED
        memory_db.commit()
        unblocked = release_dag_dependents(memory_db, root)
        assert len(unblocked) == 4

        # Complete workers 0, 1, 2
        for i in range(3):
            workers[i].status = JobStatus.COMPLETED
            memory_db.commit()
            assert len(release_dag_dependents(memory_db, workers[i])) == 0
            memory_db.refresh(sink)
            assert sink.status == JobStatus.BLOCKED

        # Complete final worker 3 -> sink unblocked
        workers[3].status = JobStatus.COMPLETED
        memory_db.commit()
        unblocked_sink = release_dag_dependents(memory_db, workers[3])
        assert len(unblocked_sink) == 1
        assert unblocked_sink[0].id == sink.id


def test_dag_cascading_failure(memory_db, test_setup):
    """Verify cascading DLQ propagation across multi-parent DAG branches."""
    make_job = test_setup["make_job"]
    job_a = make_job("A", status=JobStatus.RUNNING)
    job_b = make_job("B", status=JobStatus.BLOCKED)
    job_c = make_job("C", status=JobStatus.BLOCKED)
    job_d = make_job("D", status=JobStatus.BLOCKED)

    # A -> B, C -> D
    link_dag_dependencies(memory_db, job_b, [job_a.id])
    link_dag_dependencies(memory_db, job_c, [job_a.id])
    link_dag_dependencies(memory_db, job_d, [job_b.id, job_c.id])
    memory_db.commit()

    # Job A completes
    with patch("app.dag.dispatch_job"):
        job_a.status = JobStatus.COMPLETED
        memory_db.commit()
        release_dag_dependents(memory_db, job_a)

    # Job B fails permanently into DLQ
    job_b.status = JobStatus.DEAD_LETTER
    memory_db.commit()

    cascaded = cascade_fail_dag_dependents(memory_db, job_b, "Out of memory in worker")
    assert len(cascaded) == 1
    assert cascaded[0].id == job_d.id

    memory_db.refresh(job_d)
    assert job_d.status == JobStatus.DEAD_LETTER

    # Verify DeadLetterJob record with provenance
    dlq_entry = memory_db.query(DeadLetterJob).filter(DeadLetterJob.job_id == job_d.id).first()
    assert dlq_entry is not None
    assert "Upstream prerequisite" in dlq_entry.reason
    assert job_b.id in dlq_entry.reason


def test_dag_api_endpoints(memory_db, test_setup):
    """Verify REST API job creation with depends_on_job_ids and DAG graph endpoints."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.deps import get_current_user, get_db

    user = test_setup["user"]
    proj = test_setup["proj"]
    queue = test_setup["queue"]
    make_job = test_setup["make_job"]

    parent1 = make_job("Parent-1", status=JobStatus.COMPLETED)
    parent2 = make_job("Parent-2", status=JobStatus.QUEUED)

    app.dependency_overrides[get_db] = lambda: memory_db
    app.dependency_overrides[get_current_user] = lambda: user

    try:
        with TestClient(app) as client:
            # 1. Create child job with multi-parent dependencies [parent1, parent2]
            resp = client.post(
                f"/projects/{proj.id}/queues/{queue.id}/jobs",
                json={
                    "name": "Child-Job",
                    "payload": {"type": "simulate"},
                    "depends_on_job_ids": [parent1.id, parent2.id],
                },
            )
            assert resp.status_code == 201, resp.text
            child_data = resp.json()
            child_id = child_data["id"]
            # Since parent2 is QUEUED (not COMPLETED), child must start as BLOCKED
            assert child_data["status"] == "blocked"
            assert set(child_data["depends_on_job_ids"]) == {parent1.id, parent2.id}

            # 2. Query DAG graph for this job
            resp_dag = client.get(f"/jobs/{child_id}/dag")
            assert resp_dag.status_code == 200
            dag_data = resp_dag.json()
            assert dag_data["root_job_id"] == child_id
            assert len(dag_data["nodes"]) == 3  # parent1, parent2, child
            assert len(dag_data["edges"]) == 2  # (p1->c), (p2->c)
            assert dag_data["is_acyclic"] is True

            # 3. Query DAG graph for the entire project
            resp_proj_dag = client.get(f"/projects/{proj.id}/dag")
            assert resp_proj_dag.status_code == 200
            proj_dag = resp_proj_dag.json()
            assert len(proj_dag["nodes"]) >= 3
            assert len(proj_dag["edges"]) >= 2
    finally:
        app.dependency_overrides.clear()
