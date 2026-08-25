"""Maps a job's numeric priority (1=highest, 10=lowest) to a dedicated
Celery queue name. Redis-backed Celery doesn't support true per-task
priority ordering, so real prioritization requires routing to separate
queues that separate worker pools consume — see docker-compose.yml
(worker-high / worker-normal / worker-low) and docs/design-decisions.md.
"""

QUEUE_HIGH = "pulse.high"
QUEUE_NORMAL = "pulse.normal"
QUEUE_LOW = "pulse.low"

ALL_TIERS = (QUEUE_HIGH, QUEUE_NORMAL, QUEUE_LOW)


def priority_to_celery_queue(priority: int) -> str:
    if priority <= 3:
        return QUEUE_HIGH
    if priority <= 7:
        return QUEUE_NORMAL
    return QUEUE_LOW
