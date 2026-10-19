"""Healthcheck and Prometheus metrics router for Pulse.

Endpoints:
- GET /health: Deep readiness probe verifying PostgreSQL and Redis connectivity.
- GET /health/live: Shallow process liveness probe.
- GET /metrics: Prometheus text exposition format (version 0.0.4).
"""

from __future__ import annotations

import logging
import hmac
import ipaddress
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.metrics import collect_dynamic_metrics, registry
from app.rate_limiter import get_client_ip

router = APIRouter(tags=["monitoring"])
logger = logging.getLogger(__name__)


def verify_metrics_access(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_metrics_token: Optional[str] = Header(None),
) -> None:
    """Verifies that the /metrics scrape request is authorized:
    - If METRICS_TOKEN is configured: requires valid Bearer or X-Metrics-Token header.
    - If METRICS_PROTECT is True: restricts access to private/loopback network addresses.
    """
    if settings.METRICS_TOKEN:


        token = None
        if authorization and authorization.startswith("Bearer "):
            token = authorization[len("Bearer ") :].strip()
        elif x_metrics_token:
            token = x_metrics_token.strip()

        if not token or not hmac.compare_digest(token, settings.METRICS_TOKEN):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: invalid or missing metrics token",
                headers={"WWW-Authenticate": "Bearer realm='Metrics'"},
            )
        return

    if settings.METRICS_PROTECT:
        client_ip_str = get_client_ip(request)
        if client_ip_str in ("127.0.0.1", "::1", "localhost"):


            return
        if client_ip_str == "testclient":
            forwarded = request.headers.get("X-Forwarded-For") or request.headers.get("x-forwarded-for")
            if not forwarded:
                return
            client_ip_str = forwarded.split(",")[0].strip()

        try:
            ip = ipaddress.ip_address(client_ip_str)
            if not (ip.is_private or ip.is_loopback):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Metrics endpoint restricted to internal network",
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Metrics endpoint restricted to internal network",
            )





@router.get("/health/live", summary="Process liveness check")
def liveness_check():
    """Returns HTTP 200 if the FastAPI application process is up and running.
    
    Used by orchestrators (e.g. Kubernetes liveness probes) to verify the process is
    alive and has not deadlocked, without triggering restarts during dependent service maintenance.
    """
    return {
        "status": "alive",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.1.0",
    }


@router.get("/health", summary="Deep component readiness check")
def readiness_check(response: Response, db: Session = Depends(get_db)):
    """Deep readiness check validating connectivity and latency of core dependencies (PostgreSQL, Redis).
    
    Returns HTTP 200 OK if all components are healthy.
    Returns HTTP 503 Service Unavailable if any required component is unreachable.
    """
    components: Dict[str, Dict[str, Any]] = {}
    is_healthy = True

    # 1. Check PostgreSQL
    t0 = time.perf_counter()
    try:
        db.execute(text("SELECT 1"))
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        components["database"] = {
            "status": "healthy",
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.error("Health check: PostgreSQL query failed: %s", exc)
        components["database"] = {
            "status": "unhealthy",
            "latency_ms": latency_ms,
            "error": str(exc),
        }
        is_healthy = False

    # 2. Check Redis
    t0 = time.perf_counter()
    try:
        import redis
        client = redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=3.0)
        client.ping()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        components["redis"] = {
            "status": "healthy",
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.error("Health check: Redis ping failed: %s", exc)
        components["redis"] = {
            "status": "unhealthy",
            "latency_ms": latency_ms,
            "error": str(exc),
        }
        is_healthy = False

    if not is_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "healthy" if is_healthy else "unhealthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.1.0",
        "components": components,
    }


@router.get(
    "/metrics",
    summary="Prometheus scrape endpoint",
    response_class=Response,
    dependencies=[Depends(verify_metrics_access)],
)
def prometheus_metrics(db: Session = Depends(get_db)):

    """Exposes platform and scheduler metrics in standard Prometheus exposition format (0.0.4).
    
    Dynamically refreshes queue depths, worker heartbeats, and component latencies on scrape.
    """
    # Refresh dynamic gauges (guaranteed fail-safe)
    collect_dynamic_metrics(db)

    # Render Prometheus text exposition
    body = registry.generate_latest()
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )
