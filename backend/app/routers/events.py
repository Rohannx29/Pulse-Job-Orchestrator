import asyncio
import time
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
import redis.asyncio as aioredis
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user, get_user_org
from app.models import User, UserRole

router = APIRouter(tags=["events"])


@router.get("/events")
async def events_stream(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Server-Sent Events (SSE) endpoint providing real-time live updates.

    Subscribes to tenant-isolated Redis channels (`pulse:events:{org_id}`).
    System administrators can listen globally or specify `?org_id=<id>`.
    Sends periodic keepalive pings and automatically cleans up connections
    when the client disconnects.
    """
    org = get_user_org(db, user)
    org_id = org.id

    if user.role == UserRole.SYSTEM_ADMIN:
        req_org = request.query_params.get("org_id")
        channel = f"pulse:events:{req_org}" if req_org else "pulse:events:global"
    else:
        channel = f"pulse:events:{org_id}"

    async def event_generator():
        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)

        # Send initial connection acknowledgment
        yield f"event: connected\ndata: {{\"status\": \"connected\", \"channel\": \"{channel}\", \"org_id\": \"{org_id}\"}}\n\n"

        last_keepalive = time.time()
        try:
            while True:
                if await request.is_disconnected():
                    break

                # Poll Redis pubsub with a short timeout
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                now = time.time()

                if message is not None and message.get("type") == "message":
                    data = message.get("data", "")
                    yield f"event: update\ndata: {data}\n\n"
                    last_keepalive = now
                elif now - last_keepalive >= 15.0:
                    yield ": keepalive\n\n"
                    last_keepalive = now

                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
                await client.close()
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
