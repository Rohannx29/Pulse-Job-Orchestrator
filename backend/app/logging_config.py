import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Request-scoped correlation ID context variable
correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    """Returns the correlation ID for the active request or worker task."""
    return correlation_id_ctx.get() or ""


def set_correlation_id(corr_id: str) -> None:
    """Sets the correlation ID for the active execution context."""
    correlation_id_ctx.set(corr_id or "")


class JSONFormatter(logging.Formatter):
    """Formats Python log records as structured, single-line JSON objects."""

    def __init__(self, service_name: str = "pulse"):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        now = datetime.now(timezone.utc)
        corr_id = get_correlation_id()

        payload: dict[str, Any] = {
            "timestamp": now.isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
            "module": record.module,
            "line": record.lineno,
        }

        if corr_id:
            payload["correlation_id"] = corr_id

        # Attach exception details if present
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # Include custom extra fields passed by callers
        standard_attrs = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message",
        }
        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, default=str)


def setup_logging(service_name: str = "pulse-api", level: str = "INFO") -> None:
    """Configures structured JSON logging for standard stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(service_name=service_name))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Reduce spam from lower-level libraries while keeping Pulse logs detailed
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """FastAPI/Starlette middleware that manages X-Correlation-ID headers and access logs."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Extract existing correlation ID or generate a fresh UUID4
        corr_id = (
            request.headers.get("X-Correlation-ID")
            or request.headers.get("X-Request-ID")
            or uuid.uuid4().hex
        )
        set_correlation_id(corr_id)

        start_time = time.perf_counter()
        logger = logging.getLogger("app.access")

        try:
            response: Response = await call_next(request)
            duration_sec = time.perf_counter() - start_time
            duration_ms = round(duration_sec * 1000, 2)

            # Record Prometheus HTTP request metrics
            try:
                from app.metrics import record_http_request
                record_http_request(request.method, request.url.path, response.status_code, duration_sec)
            except Exception:
                pass

            # Inject header into downstream response
            response.headers["X-Correlation-ID"] = corr_id

            logger.info(
                "%s %s -> %s (%sms)",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                    "client_ip": request.client.host if request.client else "unknown",
                },
            )
            return response
        except Exception as exc:
            duration_sec = time.perf_counter() - start_time
            duration_ms = round(duration_sec * 1000, 2)

            # Record Prometheus HTTP request metrics for 500
            try:
                from app.metrics import record_http_request
                record_http_request(request.method, request.url.path, 500, duration_sec)
            except Exception:
                pass

            logger.exception(
                "Unhandled error in %s %s (%sms): %s",
                request.method,
                request.url.path,
                duration_ms,
                exc,
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "duration_ms": duration_ms,
                },
            )
            raise
        finally:
            set_correlation_id("")
