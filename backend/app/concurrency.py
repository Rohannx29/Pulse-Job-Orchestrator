"""Per-queue concurrency enforcement.

Celery's own --concurrency flag is per worker *process*, not per Pulse
queue, so a Queue.concurrency_limit needs its own mechanism: an atomic
semaphore in Redis that a task must acquire before doing real work and
release when done (success, permanent failure, or scheduling a domain
retry). If the queue is already at its limit, the task politely
reschedules itself a couple of seconds later without touching the job's
own retry count — being deferred because the queue is busy is not a
failure.

This is a lease-based design (a Redis sorted set of unique tokens, each
scored by its own expiry timestamp), not a shared counter with one TTL
that gets refreshed on every acquire. The earlier version had a real edge
case: a single long-running job could outlive that shared TTL if no other
job touched the same queue's key in the meantime, causing the key to
vanish entirely and let a second job acquire a slot the first one hadn't
actually released yet. With per-lease tokens:
  - acquire can only ever remove ITS OWN token on release — a worker can
    never accidentally release (or double-release) someone else's slot.
  - a lease from a worker that crashed without releasing still expires on
    its own after LEASE_TTL_SECONDS and stops counting automatically,
    self-healing the slot count without needing anything else to notice.
"""

import time
import uuid

import redis

from app.config import settings

_redis = redis.from_url(settings.REDIS_URL, decode_responses=True)

# Generous ceiling — comfortably longer than any expected job, so under
# normal operation a lease is always released explicitly and this only
# matters as a backstop against a crashed/killed worker.
LEASE_TTL_SECONDS = 3600

# Atomic: purge expired leases, count what's left, add a new lease only if
# under the limit. Single Lua script = single round trip, and Redis
# executes it without interleaving another client's commands in between,
# which is what prevents two workers from both reading "1 of 2 used" and
# both proceeding.
_ACQUIRE_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local token = ARGV[2]
local now = tonumber(ARGV[3])
local expires_at = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
local current = redis.call('ZCARD', key)
if current < limit then
    redis.call('ZADD', key, expires_at, token)
    redis.call('EXPIRE', key, 7200)
    return 1
else
    return 0
end
"""
_acquire_script = _redis.register_script(_ACQUIRE_LUA)


def _key(queue_id: str) -> str:
    return f"pulse:concurrency:{queue_id}"


def acquire_queue_slot(queue_id: str, limit: int, ttl_seconds: int = LEASE_TTL_SECONDS) -> str | None:
    """Returns a unique lease token if a slot was acquired, or None if the
    queue is already at capacity. The token must be passed back to
    release_queue_slot — this is what makes releases ownership-aware."""
    token = uuid.uuid4().hex
    now = time.time()
    acquired = _acquire_script(
        keys=[_key(queue_id)], args=[limit, token, now, now + ttl_seconds]
    )
    return token if acquired else None


def release_queue_slot(queue_id: str, token: str) -> None:
    """No-ops safely if the token is already gone (expired, or already
    released) rather than erroring — releasing twice, or releasing late
    after a lease already expired, should never be able to affect anyone
    else's slot."""
    _redis.zrem(_key(queue_id), token)


def current_usage(queue_id: str) -> int:
    now = time.time()
    key = _key(queue_id)
    _redis.zremrangebyscore(key, "-inf", now)
    return _redis.zcard(key)
