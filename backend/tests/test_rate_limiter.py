from unittest.mock import MagicMock, patch
import pytest
from pydantic import ValidationError

from app.rate_limiter import check_rate_limit
from app.auth_hardening import (
    check_account_lockout,
    record_failed_login,
    record_successful_login,
    MAX_FAILED_LOGIN_ATTEMPTS,
)
from app.schemas import UserCreate
from fastapi import HTTPException


def test_rate_limiter_allows_under_limit():
    mock_redis = MagicMock()
    # Mock return from Lua: [allowed=1, remaining=4, reset=60]
    mock_redis.eval.return_value = [1, 4, 60]

    with patch("app.rate_limiter.get_redis_client", return_value=mock_redis):
        allowed, remaining, reset = check_rate_limit("test-key", limit=5, window_seconds=60)
        assert allowed is True
        assert remaining == 4
        assert reset == 60


def test_rate_limiter_blocks_over_limit():
    mock_redis = MagicMock()
    # Mock return from Lua: [allowed=0, remaining=0, retry_after=42]
    mock_redis.eval.return_value = [0, 0, 42]

    with patch("app.rate_limiter.get_redis_client", return_value=mock_redis):
        allowed, remaining, retry_after = check_rate_limit("test-key", limit=5, window_seconds=60)
        assert allowed is False
        assert remaining == 0
        assert retry_after == 42


def test_rate_limiter_fail_open_on_redis_error():
    mock_redis = MagicMock()
    mock_redis.eval.side_effect = RuntimeError("Redis timeout")

    with patch("app.rate_limiter.get_redis_client", return_value=mock_redis):
        allowed, remaining, retry_after = check_rate_limit("test-key", limit=10, window_seconds=60)
        assert allowed is True
        assert remaining == 9


def test_password_strength_validation():
    # Valid passwords (at least 8 chars, contains letters and numbers/symbols)
    valid_data = {
        "email": "user@example.com",
        "password": "StrongPassword123",
        "organization_name": "Acme Inc",
    }
    user = UserCreate(**valid_data)
    assert user.password == "StrongPassword123"

    # Too short (< 8 chars)
    with pytest.raises(ValidationError):
        UserCreate(email="user@example.com", password="Pass1", organization_name="Acme Inc")

    # Only letters (no digits or symbols)
    with pytest.raises(ValidationError):
        UserCreate(email="user@example.com", password="OnlyLettersHere", organization_name="Acme Inc")

    # Only digits (no letters)
    with pytest.raises(ValidationError):
        UserCreate(email="user@example.com", password="1234567890", organization_name="Acme Inc")


def test_account_lockout_mechanism():
    mock_redis = MagicMock()

    # Step 1: Account not locked
    mock_redis.ttl.return_value = -2  # Key does not exist
    with patch("app.auth_hardening.get_redis_client", return_value=mock_redis):
        # Should not raise
        check_account_lockout("test@example.com")

    # Step 2: Failed attempt increments counter
    mock_redis.incr.return_value = 1
    with patch("app.auth_hardening.get_redis_client", return_value=mock_redis):
        record_failed_login("test@example.com")
        mock_redis.expire.assert_called_once()

    # Step 3: Reaching MAX_FAILED_LOGIN_ATTEMPTS locks account
    mock_redis.incr.return_value = MAX_FAILED_LOGIN_ATTEMPTS
    with patch("app.auth_hardening.get_redis_client", return_value=mock_redis):
        with pytest.raises(HTTPException) as exc_info:
            record_failed_login("test@example.com")
        assert exc_info.value.status_code == 429
        assert "temporarily locked" in exc_info.value.detail
        mock_redis.setex.assert_called_once()

    # Step 4: Successful login clears counters and locks
    mock_redis.delete.reset_mock()
    with patch("app.auth_hardening.get_redis_client", return_value=mock_redis):
        record_successful_login("test@example.com")
        assert mock_redis.delete.call_count == 2
