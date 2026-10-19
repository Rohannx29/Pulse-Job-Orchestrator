import json
from unittest.mock import MagicMock, patch
import pytest

from app.events import get_job_org_id, publish_event


def test_publish_event_success():
    mock_redis = MagicMock()
    with patch("app.events.get_redis_client", return_value=mock_redis):
        publish_event("org-123", "job.completed", {"job_id": "job-abc", "status": "COMPLETED"})

        assert mock_redis.publish.call_count == 2
        # Check first call (tenant channel)
        args_tenant = mock_redis.publish.call_args_list[0][0]
        assert args_tenant[0] == "pulse:events:org-123"
        payload_tenant = json.loads(args_tenant[1])
        assert payload_tenant["event"] == "job.completed"
        assert payload_tenant["org_id"] == "org-123"
        assert payload_tenant["data"]["job_id"] == "job-abc"
        assert "timestamp" in payload_tenant

        # Check second call (global channel)
        args_global = mock_redis.publish.call_args_list[1][0]
        assert args_global[0] == "pulse:events:global"


def test_publish_event_error_handling():
    mock_redis = MagicMock()
    mock_redis.publish.side_effect = RuntimeError("Redis connection failure")
    with patch("app.events.get_redis_client", return_value=mock_redis):
        # Should not raise exception
        publish_event("org-123", "job.failed", {"error": "boom"})


def test_publish_event_empty_org():
    mock_redis = MagicMock()
    with patch("app.events.get_redis_client", return_value=mock_redis):
        publish_event("", "job.failed", {})
        assert mock_redis.publish.call_count == 0


def test_get_job_org_id():
    mock_db = MagicMock()
    
    # None job
    assert get_job_org_id(mock_db, None) is None

    # Job without queue_id
    mock_job_no_queue = MagicMock()
    mock_job_no_queue.queue_id = None
    mock_job_no_queue.queue = None
    assert get_job_org_id(mock_db, mock_job_no_queue) is None

    # Direct relationship traversal
    mock_job_direct = MagicMock()
    mock_queue = MagicMock()
    mock_project = MagicMock()
    mock_project.org_id = "org-direct-123"
    mock_queue.project = mock_project
    mock_job_direct.queue = mock_queue

    assert get_job_org_id(mock_db, mock_job_direct) == "org-direct-123"
