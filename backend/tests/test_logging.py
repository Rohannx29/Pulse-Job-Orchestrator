import json
import logging
import uuid
import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from app.logging_config import (
    JSONFormatter,
    get_correlation_id,
    set_correlation_id,
    CorrelationIdMiddleware,
)


def test_json_formatter_basic():
    formatter = JSONFormatter(service_name="pulse-test")
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test_path.py",
        lineno=42,
        msg="Test log message with %s",
        args=("arg1",),
        exc_info=None,
    )

    set_correlation_id("corr-abc-123")
    try:
        output = formatter.format(record)
        data = json.loads(output)

        assert data["level"] == "INFO"
        assert data["logger"] == "test_logger"
        assert data["message"] == "Test log message with arg1"
        assert data["service"] == "pulse-test"
        assert data["line"] == 42
        assert data["correlation_id"] == "corr-abc-123"
        assert "timestamp" in data
    finally:
        set_correlation_id("")


def test_json_formatter_extra_fields():
    formatter = JSONFormatter(service_name="pulse-test")
    record = logging.LogRecord(
        name="test_logger",
        level=logging.WARNING,
        pathname="test_path.py",
        lineno=100,
        msg="Job failed",
        args=(),
        exc_info=None,
    )
    record.__dict__["job_id"] = "job-999"
    record.__dict__["queue_id"] = "queue-888"

    output = formatter.format(record)
    data = json.loads(output)

    assert data["job_id"] == "job-999"
    assert data["queue_id"] == "queue-888"
    assert "correlation_id" not in data  # No correlation ID set


def test_json_formatter_exception_traceback():
    formatter = JSONFormatter(service_name="pulse-test")
    try:
        raise ValueError("Simulated failure")
    except ValueError:
        import sys
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="test_logger",
        level=logging.ERROR,
        pathname="test_path.py",
        lineno=50,
        msg="An error occurred",
        args=(),
        exc_info=exc_info,
    )

    output = formatter.format(record)
    data = json.loads(output)

    assert data["level"] == "ERROR"
    assert "exception" in data
    assert "ValueError: Simulated failure" in data["exception"]


@pytest.mark.anyio
async def test_correlation_id_middleware_with_custom_header():
    middleware = CorrelationIdMiddleware(app=None)

    async def mock_call_next(request: Request):
        # Assert correlation ID is active in contextvar during execution
        assert get_correlation_id() == "client-provided-corr-id"
        return PlainTextResponse("OK")

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/health",
        "headers": [(b"x-correlation-id", b"client-provided-corr-id")],
    }
    request = Request(scope)
    response = await middleware.dispatch(request, mock_call_next)

    assert response.headers["X-Correlation-ID"] == "client-provided-corr-id"
    # Ensure ContextVar was reset after request completion
    assert get_correlation_id() == ""


@pytest.mark.anyio
async def test_correlation_id_middleware_generates_fresh_id():
    middleware = CorrelationIdMiddleware(app=None)

    captured_corr_id = None

    async def mock_call_next(request: Request):
        nonlocal captured_corr_id
        captured_corr_id = get_correlation_id()
        assert len(captured_corr_id) >= 16
        return PlainTextResponse("OK")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/jobs",
        "headers": [],
    }
    request = Request(scope)
    response = await middleware.dispatch(request, mock_call_next)

    assert "X-Correlation-ID" in response.headers
    assert response.headers["X-Correlation-ID"] == captured_corr_id
    assert get_correlation_id() == ""
