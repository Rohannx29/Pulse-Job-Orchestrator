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


import ipaddress


def is_trusted_proxy(ip_str: str) -> bool:
    """Checks whether an IP address belongs to the configured trusted proxy allowlist."""
    if not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str)
        for trusted in getattr(settings, "TRUSTED_PROXIES", []):
            try:
                if "/" in trusted:
                    if ip in ipaddress.ip_network(trusted, strict=False):
                        return True
                elif ip == ipaddress.ip_address(trusted):
                    return True
            except ValueError:
                continue
    except ValueError:
        pass
    return False


def check_rate_limit(
    key: str,
    limit: int,
    window_seconds: int,
    fail_closed: bool = False,
) -> tuple[bool, int, int]:
    """Sliding-window rate limiter using Redis sorted sets.

    Returns:
        (is_allowed: bool, remaining_requests: int, retry_after_seconds: int)
    """
    r = get_redis_client()
    if r is None:
        if fail_closed:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiter unavailable; request rejected for security",
            )
        # Fail open for general endpoints if Redis is unavailable
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
        logger.warning("Rate limit check failed for %s: %s", key, exc)
        if fail_closed:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiter unavailable; request rejected for security",
            )
        return True, limit - 1, window_seconds


def get_client_ip(request: Request) -> str:
    """Extracts client IP with strict proxy trust validation.
    
    Security rule:
    - X-Forwarded-For is trusted ONLY when the immediate TCP connection originates
      from an explicitly configured trusted proxy.
    - If connecting directly or through an untrusted intermediary, header spoofing is ignored
      and the direct socket peer IP is returned.
    """
    direct_ip = "127.0.0.1"
    if request.client and request.client.host:
        direct_ip = request.client.host

    # Only inspect X-Forwarded-For if the direct peer is a trusted reverse proxy
    if is_trusted_proxy(direct_ip):
        forwarded = request.headers.get("X-Forwarded-For") or request.headers.get("x-forwarded-for")
        if forwarded:
            # Parse CSV list of forwarded IPs; leftmost non-empty IP represents original client
            ips = [ip.strip() for ip in forwarded.split(",") if ip.strip()]
            if ips:
                return ips[0]

    return direct_ip



class RateLimiter:
    """FastAPI route dependency enforcing sliding-window rate limits."""

    def __init__(
        self,
        limit: int,
        window_seconds: int,
        scope: str = "ip",
        tag: str = "",
        fail_closed: bool = False,
    ):
        self.limit = limit
        self.window_seconds = window_seconds
        self.scope = scope
        self.tag = tag
        self.fail_closed = fail_closed

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
            fail_closed=self.fail_closed,
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


rate_limit = RateLimiter


