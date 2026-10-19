"""Prometheus metrics registry, collectors, and text exposition for Pulse.

Provides zero-dependency Prometheus exposition (format version 0.0.4) tracking:
- HTTP request counts, status codes, and duration histograms
- Dynamic queue depths and job counts across states
- Worker heartbeat and availability gauges
- Component health status and latency gauges (PostgreSQL, Redis)
- Security counters (rate limit hits, failed logins, account lockouts)
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# UUID / Hex ID regex for path normalization to prevent high cardinality
_UUID_OR_ID_PATTERN = re.compile(
    r"/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{24,32}|[0-9]+)"
)


def normalize_http_path(path: str) -> str:
    """Replaces UUIDs and raw integer IDs in paths with ':id' to prevent metric cardinality explosion."""
    if not path:
        return "/"
    # Clean query string if present
    path = path.split("?")[0]
    return _UUID_OR_ID_PATTERN.sub("/:id", path)


class Metric:
    """Base Prometheus metric container with thread-safe value storage."""

    def __init__(self, name: str, documentation: str, metric_type: str, label_names: Optional[List[str]] = None):
        self.name = name
        self.documentation = documentation
        self.metric_type = metric_type
        self.label_names = tuple(label_names or [])
        self._lock = threading.Lock()

    def _labels_to_key(self, labels: Dict[str, Any]) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted((k, str(labels.get(k, ""))) for k in self.label_names))

    def _format_labels(self, label_tuple: Tuple[Tuple[str, str], ...]) -> str:
        if not label_tuple:
            return ""
        pairs = [f'{k}="{v}"' for k, v in label_tuple]
        return "{" + ",".join(pairs) + "}"


class Counter(Metric):
    """Monotonically increasing cumulative metric."""

    def __init__(self, name: str, documentation: str, label_names: Optional[List[str]] = None):
        super().__init__(name, documentation, "counter", label_names)
        self._values: Dict[Tuple[Tuple[str, str], ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: Any) -> None:
        if amount < 0:
            raise ValueError("Counters can only be incremented by non-negative amounts.")
        key = self._labels_to_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def get(self, **labels: Any) -> float:
        key = self._labels_to_key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> List[str]:
        lines = [
            f"# HELP {self.name} {self.documentation}",
            f"# TYPE {self.name} counter",
        ]
        with self._lock:
            if not self._values and not self.label_names:
                lines.append(f"{self.name} 0")
            else:
                for labels, val in sorted(self._values.items()):
                    lbl_str = self._format_labels(labels)
                    # Format as integer if it's a whole number
                    formatted_val = f"{int(val)}" if val.is_integer() else f"{val:.4f}"
                    lines.append(f"{self.name}{lbl_str} {formatted_val}")
        return lines


class Gauge(Metric):
    """Metric that can arbitrarily go up and down."""

    def __init__(self, name: str, documentation: str, label_names: Optional[List[str]] = None):
        super().__init__(name, documentation, "gauge", label_names)
        self._values: Dict[Tuple[Tuple[str, str], ...], float] = {}

    def set(self, value: float, **labels: Any) -> None:
        key = self._labels_to_key(labels)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, **labels: Any) -> None:
        key = self._labels_to_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: Any) -> None:
        key = self._labels_to_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) - amount

    def get(self, **labels: Any) -> float:
        key = self._labels_to_key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def render(self) -> List[str]:
        lines = [
            f"# HELP {self.name} {self.documentation}",
            f"# TYPE {self.name} gauge",
        ]
        with self._lock:
            if not self._values and not self.label_names:
                lines.append(f"{self.name} 0")
            else:
                for labels, val in sorted(self._values.items()):
                    lbl_str = self._format_labels(labels)
                    formatted_val = f"{int(val)}" if val.is_integer() else f"{val:.6f}".rstrip("0").rstrip(".")
                    lines.append(f"{self.name}{lbl_str} {formatted_val}")
        return lines


class Histogram(Metric):
    """Tracks frequency and cumulative values within specified buckets."""

    DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(
        self,
        name: str,
        documentation: str,
        label_names: Optional[List[str]] = None,
        buckets: Optional[Tuple[float, ...]] = None,
    ):
        super().__init__(name, documentation, "histogram", label_names)
        self.buckets = sorted(buckets or self.DEFAULT_BUCKETS)
        self._counts: Dict[Tuple[Tuple[str, str], ...], Dict[float, int]] = {}
        self._sums: Dict[Tuple[Tuple[str, str], ...], float] = {}
        self._totals: Dict[Tuple[Tuple[str, str], ...], int] = {}

    def observe(self, value: float, **labels: Any) -> None:
        key = self._labels_to_key(labels)
        with self._lock:
            if key not in self._counts:
                self._counts[key] = {b: 0 for b in self.buckets}
                self._sums[key] = 0.0
                self._totals[key] = 0

            self._totals[key] += 1
            self._sums[key] += float(value)
            for b in self.buckets:
                if value <= b:
                    self._counts[key][b] += 1

    def render(self) -> List[str]:
        lines = [
            f"# HELP {self.name} {self.documentation}",
            f"# TYPE {self.name} histogram",
        ]
        with self._lock:
            for key in sorted(self._totals.keys()):
                lbl_dict = dict(key)
                running_count = 0
                for b in self.buckets:
                    running_count = self._counts[key][b]
                    b_labels = dict(lbl_dict)
                    b_labels["le"] = str(b)
                    lbl_str = "{" + ",".join(f'{k}="{v}"' for k, v in sorted(b_labels.items())) + "}"
                    lines.append(f"{self.name}_bucket{lbl_str} {running_count}")

                # +Inf bucket
                inf_labels = dict(lbl_dict)
                inf_labels["le"] = "+Inf"
                inf_str = "{" + ",".join(f'{k}="{v}"' for k, v in sorted(inf_labels.items())) + "}"
                lines.append(f"{self.name}_bucket{inf_str} {self._totals[key]}")

                # Count and Sum
                lbl_str = self._format_labels(key)
                lines.append(f"{self.name}_count{lbl_str} {self._totals[key]}")
                lines.append(f"{self.name}_sum{lbl_str} {self._sums[key]:.6f}".rstrip("0").rstrip("."))
        return lines


class MetricsRegistry:
    """Central registry of Prometheus metrics for the Pulse platform."""

    def __init__(self):
        self._metrics: Dict[str, Metric] = {}

    def register(self, metric: Metric) -> Metric:
        self._metrics[metric.name] = metric
        return metric

    def generate_latest(self) -> str:
        """Renders all registered metrics into standard Prometheus exposition text."""
        chunks: List[str] = []
        for _, metric in sorted(self._metrics.items()):
            rendered = metric.render()
            if rendered:
                chunks.extend(rendered)
        # Prometheus format requires a trailing newline
        return "\n".join(chunks) + "\n"


# ---------- Global Metrics Registry & Pre-Registered Metrics ----------

registry = MetricsRegistry()

# System Availability
pulse_up = registry.register(Gauge("pulse_up", "Static 1.0 indicating that the Pulse API process is running"))
pulse_up.set(1.0)

pulse_component_up = registry.register(
    Gauge("pulse_component_up", "Health status of dependent system components (1 = up, 0 = down)", ["component"])
)
pulse_component_latency_seconds = registry.register(
    Gauge("pulse_component_latency_seconds", "Observed round-trip ping latency for components in seconds", ["component"])
)

# HTTP Request Metrics
pulse_http_requests_total = registry.register(
    Counter(
        "pulse_http_requests_total",
        "Total HTTP requests handled by method, normalized endpoint, and HTTP status code",
        ["method", "handler", "status"],
    )
)
pulse_http_request_duration_seconds = registry.register(
    Histogram(
        "pulse_http_request_duration_seconds",
        "HTTP request latency histogram in seconds",
        ["handler"],
    )
)

# Operational & Scheduler Gauges
pulse_queue_depth = registry.register(
    Gauge(
        "pulse_queue_depth",
        "Current number of jobs in a queue partitioned by status",
        ["queue", "status"],
    )
)
pulse_workers_active = registry.register(
    Gauge(
        "pulse_workers_active",
        "Count of registered workers that reported a heartbeat within the last 60 seconds",
    )
)
pulse_queues_total = registry.register(
    Gauge("pulse_queues_total", "Total registered queues by state", ["state"])
)
pulse_dead_letter_jobs_total = registry.register(
    Gauge("pulse_dead_letter_jobs_total", "Total jobs currently residing in the Dead Letter Queue")
)
pulse_job_schedules_total = registry.register(
    Gauge("pulse_job_schedules_total", "Total recurring job schedules by active state", ["state"])
)

# Security and Auth Counters
pulse_rate_limited_requests_total = registry.register(
    Counter(
        "pulse_rate_limited_requests_total",
        "Total requests rejected with HTTP 429 Too Many Requests by limit type or endpoint",
        ["limit_type"],
    )
)
pulse_account_lockouts_total = registry.register(
    Counter("pulse_account_lockouts_total", "Total account lockout events triggered by brute-force protection")
)
pulse_failed_logins_total = registry.register(
    Counter("pulse_failed_logins_total", "Total failed login attempts recorded")
)
pulse_events_published_total = registry.register(
    Counter("pulse_events_published_total", "Total real-time SSE events published over Redis Pub/Sub", ["event_type"])
)

# Data Retention & Pruning Metrics
pulse_pruned_rows_total = registry.register(
    Counter("pulse_pruned_rows_total", "Total database rows deleted by automated data retention pruning", ["table"])
)
pulse_last_prune_timestamp_seconds = registry.register(
    Gauge("pulse_last_prune_timestamp_seconds", "UNIX timestamp of the most recent data retention prune run")
)


# ---------- Metric Recording Helpers ----------

def record_http_request(method: str, path: str, status_code: int, duration_seconds: float) -> None:
    """Records an incoming HTTP request in Prometheus counters and histograms."""
    try:
        normalized_path = normalize_http_path(path)
        pulse_http_requests_total.inc(method=method, handler=normalized_path, status=str(status_code))
        pulse_http_request_duration_seconds.observe(duration_seconds, handler=normalized_path)
    except Exception as exc:
        logger.debug("Failed to record HTTP metrics: %s", exc)


def record_rate_limit_hit(limit_type: str) -> None:
    """Records a rate limit rejection."""
    try:
        pulse_rate_limited_requests_total.inc(limit_type=limit_type)
    except Exception as exc:
        logger.debug("Failed to record rate limit metric: %s", exc)


def record_failed_login() -> None:
    """Records a failed authentication attempt."""
    try:
        pulse_failed_logins_total.inc()
    except Exception as exc:
        logger.debug("Failed to record failed login metric: %s", exc)


def record_account_lockout() -> None:
    """Records an account lockout."""
    try:
        pulse_account_lockouts_total.inc()
    except Exception as exc:
        logger.debug("Failed to record account lockout metric: %s", exc)


def record_event_published(event_type: str) -> None:
    """Records an SSE event published."""
    try:
        pulse_events_published_total.inc(event_type=event_type)
    except Exception as exc:
        logger.debug("Failed to record event published metric: %s", exc)


def record_data_pruned(table: str, count: int) -> None:
    """Records database rows pruned."""
    if count <= 0:
        return
    try:
        pulse_pruned_rows_total.inc(float(count), table=table)
    except Exception as exc:
        logger.debug("Failed to record data pruned metric: %s", exc)


def record_prune_run() -> None:
    """Updates the last data retention prune timestamp."""
    try:
        pulse_last_prune_timestamp_seconds.set(time.time())
    except Exception as exc:
        logger.debug("Failed to record prune run timestamp: %s", exc)



# ---------- Dynamic On-Scrape Metric Collectors ----------

def collect_dynamic_metrics(db: Optional[Session] = None) -> None:
    """Probes PostgreSQL, Redis, and scheduler state to refresh dynamic Prometheus gauges.
    
    Guaranteed fail-safe: will catch exceptions and mark component gauges as down
    rather than crashing the /metrics endpoint.
    """
    from app.config import settings

    # 1. Probe Redis Health & Latency
    t0 = time.perf_counter()
    try:
        import redis
        client = redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=2.0)
        client.ping()
        redis_latency = time.perf_counter() - t0
        pulse_component_up.set(1.0, component="redis")
        pulse_component_latency_seconds.set(redis_latency, component="redis")
    except Exception as exc:
        logger.warning("Metrics scrape: Redis ping probe failed: %s", exc)
        pulse_component_up.set(0.0, component="redis")
        pulse_component_latency_seconds.set(0.0, component="redis")

    # 2. Probe Database Health, Latency, and Table Aggregations
    if db is None:
        return

    t0 = time.perf_counter()
    try:
        db.execute(text("SELECT 1"))
        db_latency = time.perf_counter() - t0
        pulse_component_up.set(1.0, component="database")
        pulse_component_latency_seconds.set(db_latency, component="database")
    except Exception as exc:
        logger.warning("Metrics scrape: Database ping probe failed: %s", exc)
        pulse_component_up.set(0.0, component="database")
        pulse_component_latency_seconds.set(0.0, component="database")
        return  # Cannot query database tables if DB probe failed

    # Query dynamic scheduler stats from database
    try:
        from app.models import DeadLetterJob, Job, JobSchedule, Queue, Worker, WorkerStatus

        # Clear old queue depths before refreshing
        pulse_queue_depth.clear()

        # Queue depths: count jobs by (queue name, status)
        queue_stats = (
            db.query(Queue.name, Job.status, func.count(Job.id))
            .join(Job, Job.queue_id == Queue.id)
            .group_by(Queue.name, Job.status)
            .all()
        )
        for q_name, j_status, count in queue_stats:
            status_str = j_status.value if hasattr(j_status, "value") else str(j_status)
            pulse_queue_depth.set(float(count), queue=q_name, status=status_str)

        # Active workers: reported heartbeat in last 60 seconds
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
        # Note: handle SQLite test environments without timezone comparison issues
        try:
            active_worker_count = (
                db.query(func.count(Worker.id))
                .filter(Worker.status == WorkerStatus.ONLINE, Worker.last_heartbeat_at >= cutoff)
                .scalar()
                or 0
            )
        except Exception:
            active_worker_count = (
                db.query(func.count(Worker.id))
                .filter(Worker.status == WorkerStatus.ONLINE)
                .scalar()
                or 0
            )
        pulse_workers_active.set(float(active_worker_count))

        # Queues total by active/paused state
        paused_count = db.query(func.count(Queue.id)).filter(Queue.is_paused == True).scalar() or 0
        active_count = db.query(func.count(Queue.id)).filter(Queue.is_paused == False).scalar() or 0
        pulse_queues_total.set(float(active_count), state="active")
        pulse_queues_total.set(float(paused_count), state="paused")

        # Dead letter queue total
        dlq_total = db.query(func.count(DeadLetterJob.id)).scalar() or 0
        pulse_dead_letter_jobs_total.set(float(dlq_total))

        # Schedules total by state
        active_sched = db.query(func.count(JobSchedule.id)).filter(JobSchedule.is_active == True).scalar() or 0
        inactive_sched = db.query(func.count(JobSchedule.id)).filter(JobSchedule.is_active == False).scalar() or 0
        pulse_job_schedules_total.set(float(active_sched), state="active")
        pulse_job_schedules_total.set(float(inactive_sched), state="inactive")

    except Exception as exc:
        logger.warning("Metrics scrape: Error collecting table aggregates: %s", exc)
