import logging
import math
from fastapi import HTTPException, status

try:
    import redis
except ImportError:
    redis = None

from app.config import settings

logger = logging.getLogger(__name__)

MAX_FAILED_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION_SECONDS = 900  # 15 minutes
FAILED_ATTEMPTS_TTL = 900       # 15 minutes rolling window

_redis_client = None


def get_redis_client():
    global _redis_client
    if _redis_client is None:
        if redis is None:
            return None
        try:
            _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception as exc:
            logger.warning("Failed to connect to Redis for auth hardening: %s", exc)
            return None
    return _redis_client


def check_account_lockout(email: str) -> None:
    """Raises HTTP 429 if the given account is temporarily locked."""
    r = get_redis_client()
    if r is None or not email:
        return

    lock_key = f"pulse:auth:locked:{email.lower().strip()}"
    try:
        ttl = r.ttl(lock_key)
        if ttl and ttl > 0:
            minutes = max(1, math.ceil(ttl / 60))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Account is temporarily locked due to {MAX_FAILED_LOGIN_ATTEMPTS} consecutive failed login attempts. Please try again in {minutes} minutes.",
                headers={"Retry-After": str(ttl)},
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Failed to check lockout state for %s: %s", email, exc)


def record_failed_login(email: str) -> None:
    """Records a failed login attempt and locks account if threshold reached."""
    r = get_redis_client()
    if r is None or not email:
        return

    clean_email = email.lower().strip()
    fail_key = f"pulse:auth:failed:{clean_email}"
    lock_key = f"pulse:auth:locked:{clean_email}"

    try:
        attempts = r.incr(fail_key)
        if attempts == 1:
            r.expire(fail_key, FAILED_ATTEMPTS_TTL)

        try:
            from app.metrics import record_failed_login as metrics_record_failed_login
            metrics_record_failed_login()
        except Exception:
            pass

        if attempts >= MAX_FAILED_LOGIN_ATTEMPTS:
            r.setex(lock_key, LOCKOUT_DURATION_SECONDS, "1")
            r.delete(fail_key)
            try:
                from app.metrics import record_account_lockout as metrics_record_account_lockout
                metrics_record_account_lockout()
            except Exception:
                pass
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Account has been temporarily locked for {LOCKOUT_DURATION_SECONDS // 60} minutes due to {MAX_FAILED_LOGIN_ATTEMPTS} consecutive failed login attempts.",
                headers={"Retry-After": str(LOCKOUT_DURATION_SECONDS)},
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Failed to record failed login for %s: %s", email, exc)


def record_successful_login(email: str) -> None:
    """Clears failed attempts and lockout on successful authentication."""
    r = get_redis_client()
    if r is None or not email:
        return

    clean_email = email.lower().strip()
    try:
        r.delete(f"pulse:auth:failed:{clean_email}")
        r.delete(f"pulse:auth:locked:{clean_email}")
    except Exception as exc:
        logger.warning("Failed to clear auth failure records for %s: %s", email, exc)
