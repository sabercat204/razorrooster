"""T-KSI-031 — retry / backoff helper acceptance tests.

Includes the explicit assertion that Retry-After header presence is
ignored (Kalshi 429 responses do not include it; treating its absence
as authoritative would create silent dependence).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import httpx
import pytest

from razor_rooster.kalshi_connector.client.rate_limit import TokenBucket
from razor_rooster.kalshi_connector.client.retry import (
    RetryAttempt,
    RetryExhaustedError,
    retry_with_backoff,
)


@dataclass
class _FakeResponse:
    """Minimal stand-in for httpx.Response with a configurable status code."""

    status_code: int
    headers: dict[str, str]
    text: str = ""


def _no_sleep(_: float) -> None:
    return None


def test_success_first_attempt() -> None:
    """A successful (200) response returns immediately."""
    calls = 0

    def callable_() -> _FakeResponse:
        nonlocal calls
        calls += 1
        return _FakeResponse(status_code=200, headers={})

    result = retry_with_backoff(callable_, sleep=_no_sleep)
    assert result.status_code == 200
    assert calls == 1


def test_retries_on_429_then_succeeds() -> None:
    """A 429 followed by 200 succeeds after one retry."""
    counter = {"calls": 0}

    def callable_() -> _FakeResponse:
        counter["calls"] += 1
        if counter["calls"] == 1:
            return _FakeResponse(status_code=429, headers={})
        return _FakeResponse(status_code=200, headers={})

    captured: list[RetryAttempt] = []
    result = retry_with_backoff(
        callable_,
        sleep=_no_sleep,
        rng=random.Random(42),
        on_retry=captured.append,
    )
    assert result.status_code == 200
    assert counter["calls"] == 2
    assert len(captured) == 1
    assert captured[0].status_code == 429


def test_retries_on_5xx_then_succeeds() -> None:
    counter = {"calls": 0}

    def callable_() -> _FakeResponse:
        counter["calls"] += 1
        if counter["calls"] < 3:
            return _FakeResponse(status_code=503, headers={})
        return _FakeResponse(status_code=200, headers={})

    result = retry_with_backoff(
        callable_,
        sleep=_no_sleep,
        rng=random.Random(0),
        max_retries=5,
    )
    assert result.status_code == 200
    assert counter["calls"] == 3


def test_persistent_429_exhausts_retries() -> None:
    counter = {"calls": 0}

    def callable_() -> _FakeResponse:
        counter["calls"] += 1
        return _FakeResponse(status_code=429, headers={})

    with pytest.raises(RetryExhaustedError):
        retry_with_backoff(
            callable_,
            sleep=_no_sleep,
            rng=random.Random(0),
            max_retries=2,
        )
    # 1 initial + 2 retries = 3 calls
    assert counter["calls"] == 3


def test_retries_on_transport_error_then_succeeds() -> None:
    counter = {"calls": 0}

    def callable_() -> _FakeResponse:
        counter["calls"] += 1
        if counter["calls"] == 1:
            raise httpx.ConnectError(
                "simulated", request=httpx.Request("GET", "https://example/api")
            )
        return _FakeResponse(status_code=200, headers={})

    result = retry_with_backoff(
        callable_,
        sleep=_no_sleep,
        rng=random.Random(0),
    )
    assert result.status_code == 200
    assert counter["calls"] == 2


def test_persistent_transport_error_exhausts_retries() -> None:
    counter = {"calls": 0}

    def callable_() -> _FakeResponse:
        counter["calls"] += 1
        raise httpx.ReadTimeout("boom", request=httpx.Request("GET", "https://example/api"))

    with pytest.raises(RetryExhaustedError):
        retry_with_backoff(
            callable_,
            sleep=_no_sleep,
            rng=random.Random(0),
            max_retries=1,
        )
    assert counter["calls"] == 2


def test_non_retryable_status_is_not_retried() -> None:
    """A 400 / 404 propagates as a normal response (caller decides)."""
    calls = 0

    def callable_() -> _FakeResponse:
        nonlocal calls
        calls += 1
        return _FakeResponse(status_code=404, headers={})

    result = retry_with_backoff(callable_, sleep=_no_sleep)
    assert result.status_code == 404
    assert calls == 1


def test_non_retryable_exception_propagates_immediately() -> None:
    """A ValueError inside callable_ is not caught by the retry harness."""
    calls = 0

    def callable_() -> _FakeResponse:
        nonlocal calls
        calls += 1
        raise ValueError("not transient")

    with pytest.raises(ValueError, match="not transient"):
        retry_with_backoff(callable_, sleep=_no_sleep)
    assert calls == 1


def test_retry_after_header_is_ignored() -> None:
    """T-KSI-031 invariant: Kalshi has no Retry-After header.

    Even if a fixture surfaces one, the helper must not adapt its
    backoff to its value. We confirm by passing a synthetic header that
    would otherwise tell us to wait 60 seconds, and showing the helper
    still uses its own (zero-sleep) schedule.
    """
    sleep_durations: list[float] = []

    def custom_sleep(s: float) -> None:
        sleep_durations.append(s)

    counter = {"calls": 0}

    def callable_() -> _FakeResponse:
        counter["calls"] += 1
        if counter["calls"] == 1:
            # Synthetic Retry-After=60. The helper must not honor this.
            return _FakeResponse(status_code=429, headers={"Retry-After": "60"})
        return _FakeResponse(status_code=200, headers={})

    retry_with_backoff(
        callable_,
        sleep=custom_sleep,
        rng=random.Random(0),
        base_seconds=1.0,
        max_seconds=5.0,
    )
    # If the helper had honored Retry-After=60, the sleep would be 60s.
    # The helper's own backoff is at most 5s.
    assert all(s <= 5.0 for s in sleep_durations)


def test_backoff_grows_then_caps() -> None:
    """Sleep durations grow exponentially up to max_seconds."""
    sleep_durations: list[float] = []

    def custom_sleep(s: float) -> None:
        sleep_durations.append(s)

    counter = {"calls": 0}

    def callable_() -> _FakeResponse:
        counter["calls"] += 1
        return _FakeResponse(status_code=503, headers={})

    rng = random.Random(0)
    with pytest.raises(RetryExhaustedError):
        retry_with_backoff(
            callable_,
            sleep=custom_sleep,
            rng=rng,
            base_seconds=1.0,
            max_seconds=4.0,
            max_retries=4,
        )
    # 4 retries → 4 sleeps. Each is bounded by max_seconds.
    assert len(sleep_durations) == 4
    assert all(0 < s <= 4.0 for s in sleep_durations)


def test_drains_bucket_on_429() -> None:
    """When supplied a TokenBucket, a 429 retry drains it."""
    bucket = TokenBucket(capacity=100.0, refill_per_second=0.01)
    # Confirm bucket starts full
    stats_before = bucket.stats()
    assert stats_before.tokens_available > 50.0

    counter = {"calls": 0}

    def callable_() -> _FakeResponse:
        counter["calls"] += 1
        if counter["calls"] == 1:
            return _FakeResponse(status_code=429, headers={})
        return _FakeResponse(status_code=200, headers={})

    retry_with_backoff(
        callable_,
        sleep=_no_sleep,
        rng=random.Random(0),
        bucket=bucket,
    )
    stats_after = bucket.stats()
    # Bucket drained on the 429 response.
    assert stats_after.tokens_available < 1.0


def test_invalid_max_retries_rejected() -> None:
    def _ok() -> _FakeResponse:
        return _FakeResponse(status_code=200, headers={})

    with pytest.raises(ValueError, match="max_retries"):
        retry_with_backoff(_ok, max_retries=-1, sleep=_no_sleep)


def test_invalid_backoff_seconds_rejected() -> None:
    def _ok() -> _FakeResponse:
        return _FakeResponse(status_code=200, headers={})

    with pytest.raises(ValueError, match="must both be"):
        retry_with_backoff(_ok, base_seconds=0, max_seconds=10, sleep=_no_sleep)
    with pytest.raises(ValueError, match="must be <="):
        retry_with_backoff(_ok, base_seconds=10, max_seconds=5, sleep=_no_sleep)


def test_callable_returns_non_response_passes_through() -> None:
    """A non-response return value isn't treated as retryable."""
    calls = 0

    def callable_() -> str:
        nonlocal calls
        calls += 1
        return "non-response value"

    result: object = retry_with_backoff(
        callable_,
        sleep=_no_sleep,
    )
    assert result == "non-response value"
    assert calls == 1


def test_on_retry_hook_records_each_attempt() -> None:
    captured: list[RetryAttempt] = []
    counter = {"calls": 0}

    def callable_() -> _FakeResponse:
        counter["calls"] += 1
        if counter["calls"] < 3:
            return _FakeResponse(status_code=503, headers={})
        return _FakeResponse(status_code=200, headers={})

    retry_with_backoff(
        callable_,
        sleep=_no_sleep,
        rng=random.Random(0),
        on_retry=captured.append,
    )
    assert len(captured) == 2
    assert all(a.status_code == 503 for a in captured)
    assert captured[0].attempt_number == 1
    assert captured[1].attempt_number == 2
