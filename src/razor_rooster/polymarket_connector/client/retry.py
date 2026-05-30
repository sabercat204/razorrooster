"""Retry / backoff helper (T-PMC-031; REQ-PMC-RATE-002).

Wraps a callable with jittered exponential backoff for HTTP responses
that indicate the request should be retried (429 Too Many Requests, 5xx
server errors, low-level transport errors). Persistent failures surface
the original exception after exhausting retries.

The detection is duck-typed against ``httpx.Response`` so callers can
plug in mocks for tests without inheriting from httpx.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeVar

import httpx

logger = logging.getLogger(__name__)


# Defaults align with config/polymarket.yaml; callers in production
# pull these values from the loaded PolymarketConfig.
DEFAULT_MAX_RETRIES: Final[int] = 5
DEFAULT_BASE_SECONDS: Final[float] = 1.0
DEFAULT_MAX_SECONDS: Final[float] = 60.0


T = TypeVar("T")


# HTTP status codes that should be retried. 429 is rate-limit; 5xx
# (except 501/505 which indicate fundamental misuse) are transient.
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset(
    {429, 500, 502, 503, 504, 507, 508, 509, 510}
)


# Transport-level exceptions that warrant a retry. ``httpx.HTTPError`` is
# the base for all httpx-raised errors; we narrow to the transient
# subset to avoid retrying on programmer error.
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


def retry_with_backoff(
    callable_: Callable[[], T],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_seconds: float = DEFAULT_BASE_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    on_retry: Callable[[RetryAttempt], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """Call ``callable_`` with retry + jittered exponential backoff.

    A response is retried if its status code is in ``_RETRYABLE_STATUS_CODES``;
    a transport exception is retried if it is one of
    ``_RETRYABLE_TRANSPORT_EXCEPTIONS``. Any other exception propagates
    immediately.

    The backoff schedule for attempt ``k`` (0-indexed) is::

        sleep = min(max_seconds, base_seconds * 2**k) * uniform(0.5, 1.0)

    The final factor ("decorrelated jitter") spreads retries across
    callers so a thundering herd doesn't all retry on the same instant.

    Returns:
        The successful return value.

    Raises:
        :class:`RetryExhaustedError`: ``max_retries`` exhausted with the
            last failure attached as ``__cause__``.
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
                    "retry exhausted for transport error after %d attempts: %s",
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
                "retrying after transport error (attempt %d, sleep %.2fs): %s",
                attempt + 1,
                sleep_for,
                exc,
            )
            sleep(sleep_for)
            continue

        # Success path: did the response indicate a retryable status?
        status_code = _extract_status(result)
        if status_code is not None and status_code in _RETRYABLE_STATUS_CODES:
            if attempt >= max_retries:
                logger.warning(
                    "retry exhausted for status %d after %d attempts",
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
                "retrying after status %d (attempt %d, sleep %.2fs)",
                status_code,
                attempt + 1,
                sleep_for,
            )
            sleep(sleep_for)
            continue

        return result

    # Unreachable: the loop always returns or raises.
    raise RetryExhaustedError("retry_with_backoff fell off the end of its loop") from last_exc


class RateLimitError(RuntimeError):
    """The upstream returned 429 and our retry budget is exhausted."""


class ServerError(RuntimeError):
    """The upstream returned a 5xx error and our retry budget is exhausted."""


def _extract_status(value: object) -> int | None:
    """Best-effort status-code extraction without coupling to httpx types.

    Returns ``None`` if the value doesn't look like an HTTP response;
    the calling retry harness then treats it as success.
    """
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
    # Cap exponential growth so 2**attempt doesn't overflow on misuse.
    capped_attempt = min(attempt, 20)
    raw = base_seconds * (2**capped_attempt)
    bounded = min(max_seconds, raw)
    return float(bounded * rng.uniform(0.5, 1.0))
