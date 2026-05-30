"""Token-bucket rate limiter (T-KSI-030; REQ-KSI-RATE-001 / REQ-KSI-RATE-003; design §3.6).

Thread-safe leaky-token-bucket sized to the operator's Kalshi tier
budget. Where the Polymarket limiter charges 1 token per request, the
Kalshi limiter charges :func:`endpoint_costs.cost_for` tokens per
request — Kalshi documents per-endpoint costs (default 10), so a
single bucket sees varying drains.

Tier-aware sizing: ``config/kalshi.yaml`` records the operator's tier;
:meth:`from_config` builds a bucket whose capacity and refill match
``headroom_pct * tier_budget_tokens_per_sec[tier]`` (default 50% of the
Basic-tier 200 = 100 tokens/sec).

Design rationale for thread-safety: the connector mixes synchronous
HTTP calls today (REST client returns blocking httpx responses) with
potential async usage later. A threading-based limiter works for both;
async callers can wrap acquires in ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Final

from razor_rooster.kalshi_connector.client import endpoint_costs
from razor_rooster.kalshi_connector.config.loader import KalshiConfig

logger = logging.getLogger(__name__)


# Defaults applied when a caller constructs the bucket without a config
# (tests, ad-hoc scripts). Match design §3.6: 100 tokens = 50% of Basic
# tier 200 read tokens/sec.
DEFAULT_BUCKET_CAPACITY: Final[float] = 100.0
DEFAULT_REFILL_PER_SECOND: Final[float] = 100.0


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
        self._condition = threading.Condition(self._lock)
        self._pending_waiters: int = 0

    @classmethod
    def from_config(cls, config: KalshiConfig) -> TokenBucket:
        """Build a bucket sized to ``headroom_pct * tier_budget`` from config.

        The bucket capacity equals the per-second headroom (so a fresh
        bucket can sustain a one-second burst at the headroom rate).
        Refill is the same headroom rate so steady-state matches.
        """
        headroom = config.headroom_tokens_per_sec()
        if headroom <= 0:
            raise ValueError(
                f"computed headroom must be > 0, got {headroom!r}; "
                "check tier and headroom_pct in kalshi.yaml"
            )
        return cls(capacity=headroom, refill_per_second=headroom)

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

        For the typical Kalshi call path use :meth:`acquire_for_endpoint`
        instead; it consults the per-endpoint cost map automatically.

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
                    self._condition.wait(timeout=max(wait_for, 0.001))
            finally:
                self._pending_waiters -= 1
                self._condition.notify()

    def acquire_for_endpoint(self, endpoint_path: str, *, timeout: float | None = None) -> int:
        """Acquire ``cost_for(endpoint_path)`` tokens.

        Returns the integer cost that was charged. The cost is sourced
        from :mod:`endpoint_costs`; an unrecognized path drains the
        documented default (10 tokens) and emits a structured log so
        the operator can update the map. The structured log spells out
        the unrecognized path so updating the map is obvious.
        """
        template = endpoint_costs.template_for_path(endpoint_path)
        if template is None:
            cost = endpoint_costs.DEFAULT_TOKEN_COST
            logger.warning(
                "kalshi rate limiter using default cost for unrecognized path: %s",
                endpoint_path,
            )
        else:
            cost = endpoint_costs.ENDPOINT_COSTS[template]
        self.acquire(float(cost), timeout=timeout)
        return cost

    def drain(self) -> None:
        """Empty the bucket immediately.

        Called after a 429 response — Kalshi does not return
        ``Retry-After`` headers, so the safest reaction is to refill
        from zero against the bucket's configured rate while the retry
        helper applies its own backoff schedule on top.
        """
        with self._condition:
            self._refill_locked()
            self._tokens = 0.0
            self._last_refill_monotonic = time.monotonic()
            self._condition.notify_all()


# -- module-level shared bucket --------------------------------------------


_shared_bucket: TokenBucket | None = None
_shared_bucket_lock = threading.Lock()


def get_shared_bucket(
    *,
    config: KalshiConfig | None = None,
) -> TokenBucket:
    """Return the process-wide shared Kalshi bucket.

    On first call, builds the bucket from ``config`` (if supplied) or
    the module defaults. Subsequent calls reuse the same bucket; the
    ``config`` argument is ignored after the first construction. Tests
    that need a fresh bucket should call :func:`reset_shared_bucket` in
    a fixture teardown.
    """
    global _shared_bucket
    with _shared_bucket_lock:
        if _shared_bucket is None:
            _shared_bucket = TokenBucket() if config is None else TokenBucket.from_config(config)
        return _shared_bucket


def reset_shared_bucket() -> None:
    """Discard the shared bucket so the next ``get_shared_bucket`` rebuilds it."""
    global _shared_bucket
    with _shared_bucket_lock:
        _shared_bucket = None


__all__ = [
    "DEFAULT_BUCKET_CAPACITY",
    "DEFAULT_REFILL_PER_SECOND",
    "RateLimitTimeout",
    "TokenBucket",
    "TokenBucketStats",
    "get_shared_bucket",
    "reset_shared_bucket",
]
