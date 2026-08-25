"""Single place that resolves a Celery hostname to a Worker row, creating
it if this is the first time we've seen it. Used by execute_job (to
populate Job.claimed_by_worker_id and JobExecution.worker_id with a real
FK, not just a hostname string in a log line) and by sync_worker_heartbeats
(to keep the worker fleet list current) — one upsert path instead of two
copies that could drift.
"""

from app.timeutils import utcnow

from app.models import Worker, WorkerStatus


def get_or_create_worker(db, hostname: str) -> Worker:
    worker = db.query(Worker).filter(Worker.hostname == hostname).first()
    if worker is None:
        worker = Worker(hostname=hostname, status=WorkerStatus.ONLINE, last_heartbeat_at=utcnow())
        db.add(worker)
        db.flush()
    return worker
