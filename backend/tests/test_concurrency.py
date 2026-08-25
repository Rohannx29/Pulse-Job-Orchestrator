import time
import uuid

from app.concurrency import acquire_queue_slot, current_usage, release_queue_slot


def test_semaphore_respects_limit():
    queue_id = str(uuid.uuid4())
    t1 = acquire_queue_slot(queue_id, limit=2)
    t2 = acquire_queue_slot(queue_id, limit=2)
    try:
        assert t1 is not None
        assert t2 is not None
        # third acquire should fail — limit reached
        assert acquire_queue_slot(queue_id, limit=2) is None
        assert current_usage(queue_id) == 2
    finally:
        release_queue_slot(queue_id, t1)
        release_queue_slot(queue_id, t2)


def test_semaphore_release_frees_a_slot():
    queue_id = str(uuid.uuid4())
    token = acquire_queue_slot(queue_id, limit=1)
    try:
        assert token is not None
        assert acquire_queue_slot(queue_id, limit=1) is None
        release_queue_slot(queue_id, token)
        assert current_usage(queue_id) == 0
        assert acquire_queue_slot(queue_id, limit=1) is not None
    finally:
        release_queue_slot(queue_id, token)


def test_release_is_ownership_aware():
    """Releasing a token that was never issued (or already released) must
    be a safe no-op — it must never be able to free someone else's slot."""
    queue_id = str(uuid.uuid4())
    real_token = acquire_queue_slot(queue_id, limit=1)
    try:
        assert real_token is not None
        release_queue_slot(queue_id, "some-other-workers-token-that-was-never-issued")
        # the real lease must still be held — a bogus release didn't touch it
        assert current_usage(queue_id) == 1
        assert acquire_queue_slot(queue_id, limit=1) is None
    finally:
        release_queue_slot(queue_id, real_token)


def test_lease_expires_and_self_heals_without_release():
    """A worker that crashes mid-job without releasing must not permanently
    lock up a slot — the lease should expire on its own."""
    queue_id = str(uuid.uuid4())
    token = acquire_queue_slot(queue_id, limit=1, ttl_seconds=1)
    assert token is not None
    assert acquire_queue_slot(queue_id, limit=1) is None  # still held

    time.sleep(1.2)  # let the lease expire

    assert current_usage(queue_id) == 0  # self-healed, no release() needed
    token2 = acquire_queue_slot(queue_id, limit=1)
    assert token2 is not None
    release_queue_slot(queue_id, token2)


def test_semaphore_never_goes_negative():
    queue_id = str(uuid.uuid4())
    release_queue_slot(queue_id, "no-such-token")  # release with no prior acquire
    assert current_usage(queue_id) == 0
