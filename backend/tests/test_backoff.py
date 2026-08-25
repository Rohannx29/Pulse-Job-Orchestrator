from app.models import RetryPolicy, RetryStrategy
from app.tasks import _compute_backoff_seconds


def _policy(strategy, base=10):
    p = RetryPolicy(strategy=strategy, base_delay_seconds=base, max_retries=5)
    return p


def test_fixed_backoff_is_constant():
    policy = _policy(RetryStrategy.FIXED, base=5)
    assert _compute_backoff_seconds(policy, attempt=1) == 5
    assert _compute_backoff_seconds(policy, attempt=2) == 5
    assert _compute_backoff_seconds(policy, attempt=10) == 5


def test_linear_backoff_scales_with_attempt():
    policy = _policy(RetryStrategy.LINEAR, base=5)
    assert _compute_backoff_seconds(policy, attempt=1) == 5
    assert _compute_backoff_seconds(policy, attempt=2) == 10
    assert _compute_backoff_seconds(policy, attempt=3) == 15


def test_exponential_backoff_doubles():
    policy = _policy(RetryStrategy.EXPONENTIAL, base=2)
    assert _compute_backoff_seconds(policy, attempt=1) == 2
    assert _compute_backoff_seconds(policy, attempt=2) == 4
    assert _compute_backoff_seconds(policy, attempt=3) == 8
    assert _compute_backoff_seconds(policy, attempt=4) == 16


def test_no_policy_falls_back_to_exponential_default():
    # No retry policy attached -> default exponential w/ base_default=10
    assert _compute_backoff_seconds(None, attempt=1) == 10
    assert _compute_backoff_seconds(None, attempt=2) == 20
