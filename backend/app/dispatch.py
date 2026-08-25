"""Single place that knows how to hand a Job to Celery. Used both when a
job is first created (routers/jobs.py) and when a previously-BLOCKED
dependent job is released after its dependency completes (tasks.py) — one
code path, so both cases get identical priority routing and ETA handling.
"""

from app.models import Job, JobStatus
from app.priority import priority_to_celery_queue
from app.timeutils import utcnow


def dispatch_job(job: Job) -> None:
    # Imported lazily to avoid a circular import (tasks.py imports this
    # module too, and celery_app/tasks are what actually run the job).
    from app.tasks import execute_job

    celery_queue = priority_to_celery_queue(job.priority)
    if job.run_at and job.run_at > utcnow():
        job.status = JobStatus.SCHEDULED
        execute_job.apply_async(args=[job.id], eta=job.run_at, queue=celery_queue)
    else:
        job.status = JobStatus.QUEUED
        execute_job.apply_async(args=[job.id], queue=celery_queue)
