"""Token-bucket rate limiter (T-PMC-030; REQ-PMC-RATE-001; design §3.6).

A thread-safe token bucket sized at 50% of Polymarket's published 100
req/sec firm-wide cap by default. Workers acquire a token before each
request and block (with optional timeout) when the bucket is empty.

Why thread-safe rather than async-only: the connector mixes sync HTTP
calls (Gamma, CLOB public REST) with potential async use-cases later.
A threading-based limiter works for both because acquires are serialized
through a single lock; the cost is one mutex per request, which is
trivial relative to network latency.

Concurrency model:
- One module-level shared bucket per process (constructed via
  :func:`get_shared_bucket`). All HTTP clients in ``client/`` should
  use this bucket.
- :meth:`TokenBucket.acquire` blocks the calling thread; do not call
  it from inside an async event loop without offloading to a thread
  (httpx async clients can use a sync limiter via ``asyncio.to_thread``).

The bucket capacity and refill rate are floats so callers can tune the
limiter to fractional rates if Polymarket later publishes a more
restrictive cap.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)


# Default cap (50% of Polymarket's 100 req/sec) — matches
# config/polymarket.yaml. The constants here are fall-backs for callers
# that construct the limiter without a config (tests, ad-hoc scripts).
DEFAULT_BUCKET_CAPACITY: Final[float] = 50.0
DEFAULT_REFILL_PER_SECOND: Final[float] = 50.0


@dataclass(frozen=True, slots=True)
class TokenBucketStats:
    """Snapshot of bucket state at the time of the call.

    ``tokens_available`` may have a fractional component because the
    bucket refills continuously rather than in discrete ticks.
    """

    capacity: float
    refill_per_second: float
    tokens_available: float
    pending_waiters: int


class RateLimitTimeout(TimeoutError):
    """Raised when ``TokenBucket.acquire(timeout=...)`` cannot acquire in time."""


class TokenBucket:
    """Thread-safe leaky-token-bucket rate limiter.

    Capacity is the maximum burst the bucket will serve; refill_per_second
    is the steady-state rate. ``acquire(n)`` returns when the bucket has
    at least ``n`` tokens (default 1), draining them on success.

    The implementation uses a monotonic clock so it is unaffected by
    wall-clock adjustments. It refills lazily on each acquire rather than
    via a background thread; this keeps the limiter zero-overhead when
    not in use.
    """

    def __init__(
        self,
        *,
        capacity: float = DEFAULT_BUCKET_CAPACITY,
        refill_per_second: float = DEFAULT_REFILL_PER_SECOND,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity!r}")
        if refill_per_second <= 0:
            raise ValueError(f"refill_per_second must be > 0, got {refill_per_second!r}")
        self._capacity: float = float(capacity)
        self._refill_per_second: float = float(refill_per_second)
        self._tokens: float = float(capacity)
        self._last_refill_monotonic: float = time.monotonic()
        self._lock = threading.Lock()
        # Condition wakes blocked acquirers when tokens become available.
        self._condition = threading.Condition(self._lock)
        self._pending_waiters: int = 0

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def refill_per_second(self) -> float:
        return self._refill_per_second

    def stats(self) -> TokenBucketStats:
        """Return a snapshot of current bucket state. Useful in logs."""
        with self._lock:
            self._refill_locked()
            return TokenBucketStats(
                capacity=self._capacity,
                refill_per_second=self._refill_per_second,
                tokens_available=self._tokens,
                pending_waiters=self._pending_waiters,
            )

    def _refill_locked(self) -> None:
        """Refill tokens based on time since the last refill. Lock must be held."""
        now = time.monotonic()
        elapsed = now - self._last_refill_monotonic
        if elapsed > 0:
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._refill_per_second,
            )
            self._last_refill_monotonic = now

    def acquire(self, tokens: float = 1.0, *, timeout: float | None = None) -> None:
        """Acquire ``tokens`` tokens, blocking until they are available.

        Raises:
            ValueError: tokens <= 0 or > capacity.
            RateLimitTimeout: timeout elapsed before tokens available.
        """
        if tokens <= 0:
            raise ValueError(f"tokens must be > 0, got {tokens!r}")
        if tokens > self._capacity:
            raise ValueError(
                f"tokens={tokens} exceeds capacity={self._capacity}; "
                "the request can never be satisfied"
            )

        deadline: float | None = None
        if timeout is not None:
            if timeout < 0:
                raise ValueError(f"timeout must be >= 0, got {timeout!r}")
            deadline = time.monotonic() + timeout

        with self._condition:
            self._pending_waiters += 1
            try:
                while True:
                    self._refill_locked()
                    if self._tokens >= tokens:
                        self._tokens -= tokens
                        return
                    # Need to wait for refill.
                    deficit = tokens - self._tokens
                    seconds_until_ready = deficit / self._refill_per_second
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RateLimitTimeout(
                                f"could not acquire {tokens} tokens within timeout"
                            )
                        wait_for = min(seconds_until_ready, remaining)
                    else:
                        wait_for = seconds_until_ready
                    # condition.wait releases the lock and re-acquires on
                    # wakeup; a wait_for of <= 0 is illegal in some
                    # platforms so we floor at a tiny positive value.
                    self._condition.wait(timeout=max(wait_for, 0.001))
            finally:
                self._pending_waiters -= 1
                # Wake the next waiter if tokens are likely available now.
                self._condition.notify()


# Module-level shared bucket. Built lazily on first call so importers
# don't pay the cost (and don't lock in default capacity) at import time.
_shared_bucket: TokenBucket | None = None
_shared_bucket_lock = threading.Lock()


def get_shared_bucket(
    *,
    capacity: float | None = None,
    refill_per_second: float | None = None,
) -> TokenBucket:
    """Return the process-wide shared bucket, constructing on first call.

    If a bucket already exists, the ``capacity`` and ``refill_per_second``
    arguments are ignored — the bucket is constructed once and reused.
    Tests that need a fresh bucket should call :func:`reset_shared_bucket`
    in a fixture teardown.
    """
    global _shared_bucket
    with _shared_bucket_lock:
        if _shared_bucket is None:
            _shared_bucket = TokenBucket(
                capacity=capacity if capacity is not None else DEFAULT_BUCKET_CAPACITY,
                refill_per_second=(
                    refill_per_second
                    if refill_per_second is not None
                    else DEFAULT_REFILL_PER_SECOND
                ),
            )
        return _shared_bucket


def reset_shared_bucket() -> None:
    """Discard the shared bucket so the next ``get_shared_bucket`` rebuilds it.

    Test-only entry point; production code should never need to reset.
    """
    global _shared_bucket
    with _shared_bucket_lock:
        _shared_bucket = None
