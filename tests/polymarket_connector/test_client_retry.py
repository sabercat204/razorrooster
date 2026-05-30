"""T-PMC-031 — retry / backoff helper tests."""

from __future__ import annotations

import random
from collections.abc import Iterator

import httpx
import pytest

from razor_rooster.polymarket_connector.client.retry import (
    RetryAttempt,
    RetryExhaustedError,
    retry_with_backoff,
)


class _FakeResponse:
    """Just enough surface-area to look like an httpx.Response to the harness."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _seq_callable(responses: Iterator[object]) -> object:
    return next(responses)


def test_success_path_returns_immediately() -> None:
    response = _FakeResponse(200)
    result = retry_with_backoff(
        lambda: response,
        max_retries=3,
        base_seconds=0.001,
        max_seconds=0.01,
        sleep=lambda _s: None,
    )
    assert result is response


def test_retries_on_429_then_succeeds() -> None:
    sequence: list[object] = [_FakeResponse(429), _FakeResponse(200)]
    seen_attempts: list[RetryAttempt] = []

    result = retry_with_backoff(
        lambda: sequence.pop(0),
        max_retries=3,
        base_seconds=0.001,
        max_seconds=0.01,
        on_retry=seen_attempts.append,
        sleep=lambda _s: None,
    )
    assert isinstance(result, _FakeResponse)
    assert result.status_code == 200
    assert len(seen_attempts) == 1
    assert seen_attempts[0].status_code == 429


def test_retries_on_503_then_succeeds() -> None:
    sequence: list[object] = [_FakeResponse(503), _FakeResponse(503), _FakeResponse(200)]
    result = retry_with_backoff(
        lambda: sequence.pop(0),
        max_retries=3,
        base_seconds=0.001,
        max_seconds=0.01,
        sleep=lambda _s: None,
    )
    assert isinstance(result, _FakeResponse)
    assert result.status_code == 200


def test_persistent_429_exhausts_budget() -> None:
    def always_429() -> _FakeResponse:
        return _FakeResponse(429)

    with pytest.raises(RetryExhaustedError):
        retry_with_backoff(
            always_429,
            max_retries=2,
            base_seconds=0.001,
            max_seconds=0.01,
            sleep=lambda _s: None,
        )


def test_does_not_retry_on_non_retryable_status() -> None:
    """A 400 is not retryable — the harness returns it directly."""
    response = _FakeResponse(400)
    result = retry_with_backoff(
        lambda: response,
        max_retries=3,
        base_seconds=0.001,
        max_seconds=0.01,
        sleep=lambda _s: None,
    )
    assert result is response  # surfaced as-is


def test_retries_on_transport_error_then_succeeds() -> None:
    response = _FakeResponse(200)

    class _Sequence:
        attempts = 0

        def __call__(self) -> _FakeResponse:
            self.attempts += 1
            if self.attempts < 3:
                raise httpx.ConnectError("boom")
            return response

    sequence = _Sequence()
    result = retry_with_backoff(
        sequence,
        max_retries=5,
        base_seconds=0.001,
        max_seconds=0.01,
        sleep=lambda _s: None,
    )
    assert result is response


def test_persistent_transport_error_exhausts_budget() -> None:
    def always_fail() -> _FakeResponse:
        raise httpx.ConnectError("perma-down")

    with pytest.raises(RetryExhaustedError) as exc_info:
        retry_with_backoff(
            always_fail,
            max_retries=2,
            base_seconds=0.001,
            max_seconds=0.01,
            sleep=lambda _s: None,
        )
    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)


def test_non_retryable_exception_propagates_immediately() -> None:
    """ValueError is not in the retryable set — it should propagate verbatim."""

    def raise_value_error() -> _FakeResponse:
        raise ValueError("programmer error")

    with pytest.raises(ValueError, match="programmer error"):
        retry_with_backoff(
            raise_value_error,
            max_retries=3,
            base_seconds=0.001,
            max_seconds=0.01,
            sleep=lambda _s: None,
        )


def test_invalid_arguments_rejected() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        retry_with_backoff(
            lambda: None,
            max_retries=-1,
            base_seconds=0.001,
            max_seconds=0.01,
        )
    with pytest.raises(ValueError, match="must both be > 0"):
        retry_with_backoff(
            lambda: None,
            max_retries=2,
            base_seconds=0,
            max_seconds=0.01,
        )
    with pytest.raises(ValueError, match="base_seconds must be <="):
        retry_with_backoff(
            lambda: None,
            max_retries=2,
            base_seconds=10.0,
            max_seconds=1.0,
        )


def test_backoff_jitter_uses_provided_rng() -> None:
    """The seeded RNG produces deterministic sleep durations."""
    sleeps: list[float] = []
    seed = random.Random(42)
    sequence: list[object] = [_FakeResponse(429), _FakeResponse(429), _FakeResponse(200)]

    retry_with_backoff(
        lambda: sequence.pop(0),
        max_retries=3,
        base_seconds=1.0,
        max_seconds=10.0,
        sleep=sleeps.append,
        rng=seed,
    )
    assert len(sleeps) == 2
    # Same RNG seed → same sleep durations on rerun.
    sleeps2: list[float] = []
    seed2 = random.Random(42)
    sequence2: list[object] = [_FakeResponse(429), _FakeResponse(429), _FakeResponse(200)]
    retry_with_backoff(
        lambda: sequence2.pop(0),
        max_retries=3,
        base_seconds=1.0,
        max_seconds=10.0,
        sleep=sleeps2.append,
        rng=seed2,
    )
    assert sleeps == sleeps2


def test_zero_max_retries_means_one_attempt() -> None:
    """max_retries=0 → only the initial call, no retries."""
    response = _FakeResponse(429)
    with pytest.raises(RetryExhaustedError):
        retry_with_backoff(
            lambda: response,
            max_retries=0,
            base_seconds=0.001,
            max_seconds=0.01,
            sleep=lambda _s: None,
        )
