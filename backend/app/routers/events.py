import asyncio
import time
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
import redis.asyncio as aioredis
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_user_org
from app.models import User, UserRole
from app.security import decode_access_token

router = APIRouter(tags=["events"])


async def get_sse_authenticated_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Authenticates the SSE connection via httpOnly session cookie OR short-lived single-use ticket.
    Strictly forbids raw query-string JWT access tokens."""
    # 1. Cookie authentication (standard browser journey)
    cookie_token = request.cookies.get(settings.ACCESS_TOKEN_COOKIE_NAME)
    if cookie_token:
        user_id = decode_access_token(cookie_token)
        if user_id:
            user = db.get(User, user_id)
            if user:
                return user

    # 2. Short-lived one-time connection ticket (?ticket=sset_...)
    ticket = request.query_params.get("ticket")
    if ticket:
        user_id = None
        try:
            client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            # Atomically claim and delete ticket
            user_id = await client.getdel(f"pulse:sse_ticket:{ticket}")
            await client.close()
        except Exception:
            from app.deps import _LOCAL_TICKET_CACHE
            cached = _LOCAL_TICKET_CACHE.pop(ticket, None)
            if cached and cached[1] > time.time():
                user_id = cached[0]

        if user_id:
            user = db.get(User, user_id)
            if user:
                return user

    raise HTTPException(
        status_code=401,
        detail="Could not validate credentials for real-time stream. Cookie or valid one-time ticket required.",
        headers={"WWW-Authenticate": "Cookie, Ticket"},
    )


@router.get("/events")
async def events_stream(
    request: Request,
    user: User = Depends(get_sse_authenticated_user),
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
        client = None
        pubsub = None
        try:
            client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
            pubsub = client.pubsub()
            await pubsub.subscribe(channel)
        except Exception:
            yield f"event: connected\ndata: {{\"status\": \"connected\", \"channel\": \"{channel}\", \"org_id\": \"{org_id}\"}}\n\n"
            return

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
