from celery import Celery

from app.config import settings

celery_app = Celery(
    "pulse",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks"],
)

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
}
