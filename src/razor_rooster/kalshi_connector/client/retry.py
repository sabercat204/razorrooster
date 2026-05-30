"""Retry / backoff helper for Kalshi (T-KSI-031; REQ-KSI-RATE-002).

Wraps a callable with jittered exponential backoff for HTTP responses
that indicate the request should be retried (429, 5xx, transport
errors). Persistent failures surface a typed exception after the
retry budget is exhausted.

**Important Kalshi-specific behavior**: Kalshi 429 responses do NOT
include a ``Retry-After`` or ``X-RateLimit-*`` header per Kalshi's
documentation. The retry helper relies entirely on its own backoff
schedule and explicitly ignores any presence/absence of those headers.
A test asserts that header presence is ignored so silent dependence
cannot creep in.

This module differs from
:mod:`razor_rooster.polymarket_connector.client.retry` in two ways:

1. The retry path can drain the configured rate-limit bucket on 429 so
   the next attempt does not race against a still-cold rate budget.
   The Polymarket equivalent does not because Polymarket honors
   Retry-After.
2. Default `max_retries` is sourced from ``config/kalshi.yaml`` rather
   than a constant.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeVar

import httpx

from razor_rooster.kalshi_connector.client.rate_limit import TokenBucket

logger = logging.getLogger(__name__)


# Defaults align with config/kalshi.yaml; callers in production should
# pull these values from the loaded :class:`KalshiConfig`.
DEFAULT_MAX_RETRIES: Final[int] = 5
DEFAULT_BASE_SECONDS: Final[float] = 1.0
DEFAULT_MAX_SECONDS: Final[float] = 60.0


T = TypeVar("T")


# Status codes that warrant a retry. 429 plus the transient 5xx subset.
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset(
    {429, 500, 502, 503, 504, 507, 508, 509, 510}
)

# Transport-level exceptions that warrant a retry. Narrowed so we don't
# retry programmer error.
_RETRYABLE_TRANSPORT_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


@dataclass(frozen=True, slots=True)
class RetryAttempt:
    """One attempt's outcome, surfaced via the on_retry hook for logging."""

    attempt_number: int
    sleep_seconds: float
    reason: str
    status_code: int | None = None


class RetryExhaustedError(RuntimeError):
    """Raised when ``retry_with_backoff`` exhausts its retry budget.

    The original failing exception (or the failing response's status
    description) is attached as ``__cause__`` for callers that need it.
    """


class RateLimitError(RuntimeError):
    """The upstream returned 429 and our retry budget is exhausted."""


class ServerError(RuntimeError):
    """The upstream returned a 5xx error and our retry budget is exhausted."""


def retry_with_backoff(
    callable_: Callable[[], T],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_seconds: float = DEFAULT_BASE_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    on_retry: Callable[[RetryAttempt], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    bucket: TokenBucket | None = None,
) -> T:
    """Call ``callable_`` with retry + jittered exponential backoff.

    Retries on:

    - Transport errors in :data:`_RETRYABLE_TRANSPORT_EXCEPTIONS`.
    - Responses with status in :data:`_RETRYABLE_STATUS_CODES`.

    On a 429 specifically, if ``bucket`` is supplied the bucket is
    drained before the sleep so the next attempt does not race against
    a partially-refilled budget. Kalshi does not return a
    ``Retry-After`` header; this helper ignores that header even if a
    test fixture surfaces one.

    Backoff schedule for attempt ``k`` (0-indexed)::

        sleep = min(max_seconds, base_seconds * 2**k) * uniform(0.5, 1.0)

    Returns:
        The successful return value.

    Raises:
        :class:`RetryExhaustedError`: budget exhausted with the last
            failure attached as ``__cause__``.
    """
    if max_retries < 0:
        raise ValueError(f"max_retries must be >= 0, got {max_retries!r}")
    if base_seconds <= 0 or max_seconds <= 0:
        raise ValueError("base_seconds and max_seconds must both be > 0")
    if base_seconds > max_seconds:
        raise ValueError("base_seconds must be <= max_seconds")

    rng_local = rng if rng is not None else random.Random()

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            result = callable_()
        except _RETRYABLE_TRANSPORT_EXCEPTIONS as exc:
            last_exc = exc
            if attempt >= max_retries:
                logger.warning(
                    "kalshi retry exhausted for transport error after %d attempts: %s",
                    attempt + 1,
                    exc,
                )
                raise RetryExhaustedError(
                    f"transport-level retry exhausted after {attempt + 1} attempts"
                ) from exc
            sleep_for = _backoff_seconds(attempt, base_seconds, max_seconds, rng_local)
            if on_retry is not None:
                on_retry(
                    RetryAttempt(
                        attempt_number=attempt + 1,
                        sleep_seconds=sleep_for,
                        reason=f"transport: {type(exc).__name__}",
                    )
                )
            logger.info(
                "kalshi retrying after transport error (attempt %d, sleep %.2fs): %s",
                attempt + 1,
                sleep_for,
                exc,
            )
            sleep(sleep_for)
            continue

        # Success path: did the response indicate a retryable status?
        status_code = _extract_status(result)
        if status_code is not None and status_code in _RETRYABLE_STATUS_CODES:
            # Note: Kalshi does not return Retry-After. Even if a test
            # response carries one, we ignore it deliberately.
            if status_code == 429 and bucket is not None:
                bucket.drain()
            if attempt >= max_retries:
                logger.warning(
                    "kalshi retry exhausted for status %d after %d attempts",
                    status_code,
                    attempt + 1,
                )
                exc_class: type[BaseException] = (
                    RateLimitError if status_code == 429 else ServerError
                )
                surfaced = exc_class(
                    f"retryable status {status_code} surfaced after {attempt + 1} attempts"
                )
                raise RetryExhaustedError(
                    f"status-{status_code} retry exhausted after {attempt + 1} attempts"
                ) from surfaced
            sleep_for = _backoff_seconds(attempt, base_seconds, max_seconds, rng_local)
            if on_retry is not None:
                on_retry(
                    RetryAttempt(
                        attempt_number=attempt + 1,
                        sleep_seconds=sleep_for,
                        reason=f"status: {status_code}",
                        status_code=status_code,
                    )
                )
            logger.info(
                "kalshi retrying after status %d (attempt %d, sleep %.2fs)",
                status_code,
                attempt + 1,
                sleep_for,
            )
            sleep(sleep_for)
            continue

        return result

    # Unreachable.
    raise RetryExhaustedError("retry_with_backoff fell off the end of its loop") from last_exc


def _extract_status(value: object) -> int | None:
    """Best-effort status-code extraction without coupling to httpx types."""
    status = getattr(value, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _backoff_seconds(
    attempt: int,
    base_seconds: float,
    max_seconds: float,
    rng: random.Random,
) -> float:
    """Jittered exponential backoff for retry attempt ``attempt`` (0-indexed)."""
    capped_attempt = min(attempt, 20)
    raw = base_seconds * (2**capped_attempt)
    bounded = min(max_seconds, raw)
    return float(bounded * rng.uniform(0.5, 1.0))


__all__ = [
    "DEFAULT_BASE_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MAX_SECONDS",
    "RateLimitError",
    "RetryAttempt",
    "RetryExhaustedError",
    "ServerError",
    "retry_with_backoff",
]
