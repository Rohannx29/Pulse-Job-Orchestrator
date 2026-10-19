"""Healthcheck and Prometheus metrics router for Pulse.

Endpoints:
- GET /health: Deep readiness probe verifying PostgreSQL and Redis connectivity.
- GET /health/live: Shallow process liveness probe.
- GET /metrics: Prometheus text exposition format (version 0.0.4).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.metrics import collect_dynamic_metrics, registry

router = APIRouter(tags=["monitoring"])
logger = logging.getLogger(__name__)


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


@router.get("/metrics", summary="Prometheus scrape endpoint", response_class=Response)
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
