"""Unit tests for Prometheus metrics registry, gauges, counters, histograms, and health endpoints."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    normalize_http_path,
    record_http_request,
    record_rate_limit_hit,
    record_failed_login,
    record_account_lockout,
)


def test_counter_inc_and_render():
    reg = MetricsRegistry()
    c = reg.register(Counter("test_counter", "A test counter", ["method", "status"]))

    c.inc(method="GET", status="200")
    c.inc(2.0, method="GET", status="200")
    c.inc(method="POST", status="201")

    assert c.get(method="GET", status="200") == 3.0
    assert c.get(method="POST", status="201") == 1.0
    assert c.get(method="DELETE", status="404") == 0.0

    with pytest.raises(ValueError):
        c.inc(-1.0, method="GET", status="200")

    rendered = reg.generate_latest()
    assert "# HELP test_counter A test counter" in rendered
    assert "# TYPE test_counter counter" in rendered
    assert 'test_counter{method="GET",status="200"} 3' in rendered
    assert 'test_counter{method="POST",status="201"} 1' in rendered


def test_gauge_set_inc_dec_clear():
    reg = MetricsRegistry()
    g = reg.register(Gauge("test_gauge", "A test gauge", ["queue"]))

    g.set(10.0, queue="default")
    assert g.get(queue="default") == 10.0

    g.inc(5.0, queue="default")
    assert g.get(queue="default") == 15.0

    g.dec(3.0, queue="default")
    assert g.get(queue="default") == 12.0

    rendered = reg.generate_latest()
    assert 'test_gauge{queue="default"} 12' in rendered

    g.clear()
    assert g.get(queue="default") == 0.0


def test_histogram_observe():
    reg = MetricsRegistry()
    h = reg.register(Histogram("test_latency", "Latency in seconds", ["handler"], buckets=(0.01, 0.05, 0.1, 0.5)))

    h.observe(0.005, handler="/health")
    h.observe(0.02, handler="/health")
    h.observe(0.08, handler="/health")

    rendered = reg.generate_latest()
    assert "# HELP test_latency Latency in seconds" in rendered
    assert "# TYPE test_latency histogram" in rendered
    assert 'test_latency_bucket{handler="/health",le="0.01"} 1' in rendered
    assert 'test_latency_bucket{handler="/health",le="0.05"} 2' in rendered
    assert 'test_latency_bucket{handler="/health",le="0.1"} 3' in rendered
    assert 'test_latency_bucket{handler="/health",le="0.5"} 3' in rendered
    assert 'test_latency_bucket{handler="/health",le="+Inf"} 3' in rendered
    assert 'test_latency_count{handler="/health"} 3' in rendered


def test_normalize_http_path():
    assert normalize_http_path("/jobs/550e8400-e29b-41d4-a716-446655440000/retry") == "/jobs/:id/retry"
    assert normalize_http_path("/queues/12345/pause") == "/queues/:id/pause"
    assert normalize_http_path("/projects/abc123def456abc123def456") == "/projects/:id"
    assert normalize_http_path("/auth/login?redirect=true") == "/auth/login"
    assert normalize_http_path("/health") == "/health"
    assert normalize_http_path("") == "/"


def test_record_helpers():
    record_http_request("GET", "/health", 200, 0.002)
    record_rate_limit_hit("login_ip")
    record_failed_login()
    record_account_lockout()
    # Should complete without error


def test_health_liveness_endpoint():
    client = TestClient(app)
    res = client.get("/health/live")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "alive"
    assert data["version"] == "0.1.0"
    assert "timestamp" in data


def test_health_readiness_endpoint():
    client = TestClient(app)
    res = client.get("/health")
    # Will return 200 if both DB and Redis respond, or 503 if Redis is unavailable in local testing
    assert res.status_code in (200, 503)
    data = res.json()
    assert data["status"] in ("healthy", "unhealthy")
    assert "components" in data
    assert "database" in data["components"]
    assert "redis" in data["components"]


def test_prometheus_metrics_endpoint():
    client = TestClient(app)
    # Generate some traffic first
    client.get("/health/live")

    res = client.get("/metrics")
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    body = res.text
    assert "# HELP pulse_up" in body
    assert "pulse_up 1" in body
    assert "pulse_http_requests_total" in body
    assert "pulse_queue_depth" in body
    assert "pulse_workers_active" in body
