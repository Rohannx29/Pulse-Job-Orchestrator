try:
    from celery import Celery
    HAS_CELERY = True
except ImportError:
    Celery = None
    HAS_CELERY = False

from app.config import settings

if HAS_CELERY:
    celery_app = Celery(
        "pulse",
        broker=settings.REDIS_URL,
        backend=settings.REDIS_URL,
        include=["app.tasks"],
    )
else:
    class _DummyControl:
        def inspect(self, *args, **kwargs):
            return type("Inspect", (), {"stats": lambda self: {}, "active": lambda self: {}})()

    class _DummyCelery:
        class _Conf:
            def __init__(self):
                self.beat_schedule = {}
            def update(self, *args, **kwargs):
                pass
        def __init__(self):
            self.conf = self._Conf()
            self.control = _DummyControl()
        def send_task(self, *args, **kwargs):
            return None
        def task(self, *args, **kwargs):
            bind = kwargs.get("bind", False)
            max_retries = kwargs.get("max_retries", 3)
            def decorator(fn):
                class TaskWrapper:
                    def __init__(self, target_fn):
                        self.target_fn = target_fn
                        self.max_retries = max_retries
                        self.request = type("Req", (), {"retries": 0})()
                    def __call__(self, *a, **k):
                        if bind:
                            return self.target_fn(self, *a, **k)
                        return self.target_fn(*a, **k)
                    def apply_async(self, *a, **k):
                        return None
                    def delay(self, *a, **k):
                        return None
                    def retry(self, *a, **k):
                        raise RuntimeError(f"Retry triggered: {k}")
                wrapper = TaskWrapper(fn)
                return wrapper
            return decorator
    celery_app = _DummyCelery()

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    worker_send_task_events=True,
    task_send_sent_event=True,
    # Fair dispatch: don't let one worker hoard many long jobs at once
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    # Jobs are routed explicitly to pulse.high/normal/low at dispatch time
    # (see app/priority.py). Anything NOT explicitly routed — i.e. Beat's
    # own internal housekeeping tasks below — lands here instead, kept
    # separate so a burst of job execution can never starve heartbeat
    # sync or cron dispatch.
    task_default_queue="pulse.internal",
)

# Beat schedule: periodic housekeeping tasks, pinned to pulse.internal.
# Recurring *user* JobSchedules are dispatched dynamically by
# `dispatch_due_schedules`, which itself runs on a fixed interval below,
# and routes the jobs it spawns to the correct priority-tier queue.
celery_app.conf.beat_schedule = {
    "sync-worker-heartbeats": {
        "task": "app.tasks.sync_worker_heartbeats",
        "schedule": settings.HEARTBEAT_INTERVAL_SECONDS,
        "options": {"queue": "pulse.internal"},
    },
    "dispatch-due-schedules": {
        "task": "app.tasks.dispatch_due_schedules",
        "schedule": 30.0,  # check every 30s for cron matches
        "options": {"queue": "pulse.internal"},
    },
    "prune-stale-data": {
        "task": "app.tasks.prune_stale_data",
        "schedule": settings.PRUNE_INTERVAL_SECONDS,
        "options": {"queue": "pulse.internal"},
    },
}

# ---------------------------------------------------------------------------
# Celery worker signals: structured JSON logging and correlation ID propagation
# ---------------------------------------------------------------------------
try:
    from celery.signals import setup_logging as celery_setup_logging, task_postrun, task_prerun

    @celery_setup_logging.connect
    def on_celery_setup_logging(**kwargs):
        if settings.LOG_FORMAT == "json":
            from app.logging_config import setup_logging
            setup_logging(service_name="pulse-worker", level=settings.LOG_LEVEL)

    @task_prerun.connect
    def on_task_prerun(task_id, task, *args, **kwargs):
        from app.logging_config import set_correlation_id
        headers = getattr(task.request, "headers", None) or {}
        corr_id = headers.get("correlation_id") or task_id
        set_correlation_id(corr_id)

    @task_postrun.connect
    def on_task_postrun(*args, **kwargs):
        from app.logging_config import set_correlation_id
        set_correlation_id("")
except Exception:
    pass

