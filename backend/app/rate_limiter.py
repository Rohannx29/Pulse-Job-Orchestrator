import logging
import math
import time
import uuid
from typing import Any

from fastapi import HTTPException, Request, Response, status

try:
    import redis
except ImportError:
    redis = None

from app.config import settings

logger = logging.getLogger(__name__)

# Synchronous Redis client for rate limiting
_redis_client = None


def get_redis_client():
    global _redis_client
    if _redis_client is None:
        if redis is None:
            return None
        try:
            _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception as exc:
            logger.warning("Failed to connect to Redis for rate limiting: %s", exc)
            return None
    return _redis_client


_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local member = ARGV[4]

local window_start = now - window
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)
local current = redis.call('ZCARD', key)

if current < limit then
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, window + 10)
    local remaining = limit - (current + 1)
    return {1, remaining, window}
else
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = window
    if #oldest >= 2 then
        local oldest_ts = tonumber(oldest[2])
        retry_after = math.max(1, math.ceil((oldest_ts + window) - now))
    end
    return {0, 0, retry_after}
end
"""


def check_rate_limit(key: str, limit: int, window_seconds: int) -> tuple[bool, int, int]:
    """Sliding-window rate limiter using Redis sorted sets.

    Returns:
        (is_allowed: bool, remaining_requests: int, retry_after_seconds: int)
    """
    r = get_redis_client()
    if r is None:
        # Fail open if Redis is unavailable or uninstalled in local test harness
        return True, limit - 1, window_seconds

    now = time.time()
    member = f"{now}-{uuid.uuid4().hex[:8]}"

    try:
        res = r.eval(_SLIDING_WINDOW_LUA, 1, key, limit, window_seconds, now, member)
        allowed = bool(res[0])
        remaining = int(res[1])
        retry_after = int(res[2])
        return allowed, remaining, retry_after
    except Exception as exc:
        logger.warning("Rate limit check failed for %s, failing open: %s", key, exc)
        return True, limit - 1, window_seconds


def get_client_ip(request: Request) -> str:
    """Extracts client IP, respecting X-Forwarded-For if present."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


class RateLimiter:
    """FastAPI route dependency enforcing sliding-window rate limits."""

    def __init__(self, limit: int, window_seconds: int, scope: str = "ip", tag: str = ""):
        self.limit = limit
        self.window_seconds = window_seconds
        self.scope = scope
        self.tag = tag

    async def __call__(self, request: Request, response: Response):
        ip = get_client_ip(request)
        tag = self.tag or request.url.path

        if self.scope == "ip":
            key = f"pulse:ratelimit:ip:{ip}:{tag}"
        elif self.scope == "org":
            # Extract org_id from query/path/state or fallback to IP
            org_id = request.path_params.get("project_id") or ip
            key = f"pulse:ratelimit:org:{org_id}:{tag}"
        else:
            key = f"pulse:ratelimit:general:{ip}:{tag}"

        allowed, remaining, retry_after = check_rate_limit(
            key=key,
            limit=self.limit,
            window_seconds=self.window_seconds,
        )

        response.headers["X-RateLimit-Limit"] = str(self.limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
        response.headers["X-RateLimit-Reset"] = str(retry_after)

        if not allowed:
            try:
                from app.metrics import record_rate_limit_hit
                record_rate_limit_hit(limit_type=tag)
            except Exception:
                pass
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Maximum {self.limit} requests per {self.window_seconds}s. Try again in {retry_after}s.",
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(self.limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(retry_after),
                },
            )
