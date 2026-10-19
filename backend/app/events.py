import json
import logging
from typing import Any

try:
    import redis
except ImportError:
    redis = None  # Mocked in test environments without redis-py

from app.config import settings
from app.timeutils import utcnow

logger = logging.getLogger(__name__)

# Synchronous Redis client for worker tasks and FastAPI route handlers
_redis_client = None


def get_redis_client():
    global _redis_client
    if _redis_client is None:
        if redis is None:
            raise RuntimeError("redis package is not installed")
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


def publish_event(org_id: str, event_type: str, data: dict[str, Any]) -> None:
    """Publish a real-time event to Redis Pub/Sub for an organization.

    Subscribers listening on `pulse:events:{org_id}` will receive this event
    over their SSE stream. Never raises an exception if Redis is temporarily
    unavailable, ensuring background tasks and API operations remain unaffected.
    """
    if not org_id:
        return

    try:
        r = get_redis_client()
        payload = json.dumps({
            "event": event_type,
            "org_id": str(org_id),
            "timestamp": utcnow().isoformat(),
            "data": data,
        })
        # Tenant-scoped channel guarantees tenant isolation
        r.publish(f"pulse:events:{org_id}", payload)
        # Global channel for platform system administrators
        r.publish("pulse:events:global", payload)
        try:
            from app.metrics import record_event_published
            record_event_published(event_type)
        except Exception:
            pass
    except Exception as exc:
        logger.warning("Failed to publish SSE event %s to org %s: %s", event_type, org_id, exc)

    # Outbound webhook delivery bridge
    try:
        from app.database import SessionLocal
        from app.webhooks import dispatch_webhooks_for_event
        db = SessionLocal()
        try:
            dispatch_webhooks_for_event(db, str(org_id), event_type, data)
        finally:
            db.close()
    except Exception as exc:
        logger.debug("Failed to dispatch outbound webhooks for org %s event %s: %s", org_id, event_type, exc)


def get_job_org_id(db, job) -> str | None:
    """Safely traverses job -> queue -> project -> org_id."""
    if job is None:
        return None

    # Direct relationship traversal if relationship is already attached
    try:
        queue = getattr(job, "queue", None)
        if queue and getattr(queue, "project", None) and getattr(queue.project, "org_id", None):
            return str(queue.project.org_id)
    except Exception:
        pass

    if not getattr(job, "queue_id", None) or db is None:
        return None

    try:
        from app.models import Queue
        queue = db.get(Queue, job.queue_id)
        if queue and queue.project:
            return str(queue.project.org_id)
    except Exception:
        pass

    return None
