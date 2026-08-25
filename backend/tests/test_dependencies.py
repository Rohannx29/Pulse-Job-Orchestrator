"""Integration tests for job dependencies (app.tasks._release_dependents /
_cascade_fail_dependents), run against a real Postgres connection like the
rest of the suite. Celery dispatch itself is mocked out here — this is
testing the DB-level state machine (blocked -> released, blocked ->
cascaded dead_letter), not re-proving Celery delivery, which the manual
live verification in this session already did end to end.
"""

import uuid
from unittest.mock import patch

import pytest

from app.database import SessionLocal
from app.models import Job, JobStatus, Organization, Project, Queue, User
from app.security import hash_password
from app.tasks import _cascade_fail_dependents, _release_dependents


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def queue(db):
    """A throwaway org/project/queue for each test, cleaned up after."""
    user = User(email=f"dep-test-{uuid.uuid4()}@test.com", hashed_password=hash_password("x"))
    db.add(user)
    db.flush()
    org = Organization(name="dep-test-org", owner_id=user.id)
    db.add(org)
    db.flush()
    project = Project(org_id=org.id, name="dep-test-project")
    db.add(project)
    db.flush()
    q = Queue(project_id=project.id, name="dep-test-queue", concurrency_limit=5)
    db.add(q)
    db.commit()
    yield q
    db.query(Job).filter(Job.queue_id == q.id).delete()
    db.query(Queue).filter(Queue.id == q.id).delete()
    db.query(Project).filter(Project.id == project.id).delete()
    db.query(Organization).filter(Organization.id == org.id).delete()
    db.query(User).filter(User.id == user.id).delete()
    db.commit()


def _make_job(db, queue, name, status, depends_on_job_id=None):
    job = Job(queue_id=queue.id, name=name, status=status, depends_on_job_id=depends_on_job_id)
    db.add(job)
    db.flush()
    return job


def test_release_dependents_dispatches_and_flips_status(db, queue):
    parent = _make_job(db, queue, "parent", JobStatus.COMPLETED)
    child = _make_job(db, queue, "child", JobStatus.BLOCKED, depends_on_job_id=parent.id)
    other_queue_job = _make_job(db, queue, "unrelated", JobStatus.BLOCKED, depends_on_job_id=None)
    db.commit()

    with patch("app.tasks.dispatch_job") as mock_dispatch:
        _release_dependents(db, parent)

    mock_dispatch.assert_called_once()
    called_with_job = mock_dispatch.call_args[0][0]
    assert called_with_job.id == child.id

    db.refresh(other_queue_job)
    assert other_queue_job.status == JobStatus.BLOCKED  # untouched, no dependency


def test_release_dependents_noop_when_nothing_blocked(db, queue):
    parent = _make_job(db, queue, "parent", JobStatus.COMPLETED)
    db.commit()

    with patch("app.tasks.dispatch_job") as mock_dispatch:
        _release_dependents(db, parent)

    mock_dispatch.assert_not_called()


def test_cascade_fail_propagates_through_multi_level_chain(db, queue):
    x = _make_job(db, queue, "x", JobStatus.DEAD_LETTER)
    y = _make_job(db, queue, "y", JobStatus.BLOCKED, depends_on_job_id=x.id)
    z = _make_job(db, queue, "z", JobStatus.BLOCKED, depends_on_job_id=y.id)
    unrelated = _make_job(db, queue, "unrelated", JobStatus.BLOCKED)
    db.commit()

    _cascade_fail_dependents(db, x, "root cause failure")

    db.refresh(y)
    db.refresh(z)
    db.refresh(unrelated)
    assert y.status == JobStatus.DEAD_LETTER
    assert z.status == JobStatus.DEAD_LETTER
    assert "root cause failure" in y.dead_letter_entry.reason
    assert x.id in z.dead_letter_entry.reason  # traceable back through the chain
    assert unrelated.status == JobStatus.BLOCKED  # untouched
