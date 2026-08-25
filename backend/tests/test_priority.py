from app.priority import QUEUE_HIGH, QUEUE_LOW, QUEUE_NORMAL, priority_to_celery_queue


def test_high_priority_tier():
    assert priority_to_celery_queue(1) == QUEUE_HIGH
    assert priority_to_celery_queue(3) == QUEUE_HIGH


def test_normal_priority_tier():
    assert priority_to_celery_queue(4) == QUEUE_NORMAL
    assert priority_to_celery_queue(7) == QUEUE_NORMAL


def test_low_priority_tier():
    assert priority_to_celery_queue(8) == QUEUE_LOW
    assert priority_to_celery_queue(10) == QUEUE_LOW
