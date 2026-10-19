"""Cross-Site Request Forgery (CSRF) Protection Middleware for Pulse.

Enforces origin and referer validation on all state-modifying HTTP requests
(POST, PUT, PATCH, DELETE) when browser credentials (cookies) are present.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class CSRFProtectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in SAFE_METHODS:
            return await call_next(request)

        # Check if cookie authentication is present on this request
        cookie_token = request.cookies.get(settings.ACCESS_TOKEN_COOKIE_NAME)
        if not cookie_token:
            # Not authenticated via cookies (e.g. pure Bearer token or unauthenticated auth routes)
            return await call_next(request)

        # Cookie authentication is active: verify Origin or Referer header
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")

        candidate_origin = None
        if origin:
            candidate_origin = origin.rstrip("/")
        elif referer:
            parsed = urlparse(referer)
            if parsed.scheme and parsed.netloc:
                candidate_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

        if not candidate_origin:
            logger.warning(
                "CSRF violation: missing Origin/Referer header on %s %s with cookie auth",
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF protection: missing Origin/Referer header with cookie auth"},
            )

        # Build allowed origins set
        allowed_origins = {o.rstrip("/") for o in settings.CORS_ORIGINS}
        host = request.headers.get("host")
        if host:
            allowed_origins.add(f"http://{host}".rstrip("/"))
            allowed_origins.add(f"https://{host}".rstrip("/"))
        if settings.IS_DEV:
            allowed_origins.add("http://testserver")
            allowed_origins.add("http://localhost:3000")
            allowed_origins.add("http://127.0.0.1:3000")
            allowed_origins.add("http://localhost:8000")
            allowed_origins.add("http://127.0.0.1:8000")

        if candidate_origin not in allowed_origins:
            logger.warning(
                "CSRF violation: rejected untrusted origin %s on %s %s",
                candidate_origin,
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": f"CSRF protection: untrusted origin {candidate_origin!r}"},
            )

        return await call_next(request)
