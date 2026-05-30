"""T-PMC-030 — token-bucket rate limiter tests."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator

import pytest

from razor_rooster.polymarket_connector.client.rate_limit import (
    DEFAULT_BUCKET_CAPACITY,
    DEFAULT_REFILL_PER_SECOND,
    RateLimitTimeout,
    TokenBucket,
    get_shared_bucket,
    reset_shared_bucket,
)


@pytest.fixture(autouse=True)
def _reset_shared() -> Iterator[None]:
    reset_shared_bucket()
    yield
    reset_shared_bucket()


def test_initial_bucket_is_full() -> None:
    bucket = TokenBucket(capacity=10.0, refill_per_second=10.0)
    stats = bucket.stats()
    assert stats.capacity == 10.0
    assert stats.tokens_available == 10.0


def test_acquire_drains_bucket() -> None:
    bucket = TokenBucket(capacity=5.0, refill_per_second=5.0)
    for _ in range(5):
        bucket.acquire()
    stats = bucket.stats()
    assert stats.tokens_available <= 0.5  # may have accumulated minor refill


def test_acquire_blocks_when_empty_then_returns() -> None:
    bucket = TokenBucket(capacity=2.0, refill_per_second=10.0)  # 100ms per token
    bucket.acquire()
    bucket.acquire()
    start = time.monotonic()
    bucket.acquire()  # forces a wait of ~0.1s
    elapsed = time.monotonic() - start
    assert 0.05 <= elapsed <= 0.5  # generous bound for CI flakiness


def test_acquire_timeout_raises() -> None:
    bucket = TokenBucket(capacity=1.0, refill_per_second=0.1)  # very slow refill
    bucket.acquire()
    with pytest.raises(RateLimitTimeout):
        bucket.acquire(timeout=0.05)


def test_acquire_invalid_tokens_raises() -> None:
    bucket = TokenBucket(capacity=10.0, refill_per_second=10.0)
    with pytest.raises(ValueError, match="must be > 0"):
        bucket.acquire(tokens=0)
    with pytest.raises(ValueError, match="exceeds capacity"):
        bucket.acquire(tokens=11)


def test_invalid_construction_rejected() -> None:
    with pytest.raises(ValueError, match="capacity"):
        TokenBucket(capacity=0, refill_per_second=10.0)
    with pytest.raises(ValueError, match="refill_per_second"):
        TokenBucket(capacity=10.0, refill_per_second=0)


def test_concurrent_acquirers_do_not_exceed_cap() -> None:
    """Burst of N requests across threads completes inside the bucket budget."""
    capacity = 10.0
    refill = 50.0  # 50 tokens/sec — refills 1 token per 20ms
    request_count = 30
    bucket = TokenBucket(capacity=capacity, refill_per_second=refill)
    barrier = threading.Barrier(request_count)
    completion_times: list[float] = [0.0] * request_count
    start = time.monotonic()

    def worker(i: int) -> None:
        barrier.wait()  # release everyone simultaneously
        bucket.acquire()
        completion_times[i] = time.monotonic() - start

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(request_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 30 requests divided by 50/sec refill ≈ 0.6 sec for the requests
    # beyond the initial 10. Allow generous margin for thread-scheduling
    # noise.
    assert max(completion_times) <= 1.5
    # And no request finished after a budget-violating burst — sanity:
    # at t=0 we expect ~10 acquires to succeed; the rest should be
    # spread out by refill.
    early = sum(1 for ct in completion_times if ct <= 0.05)
    assert early <= int(capacity) + 2  # some slack for refill while burst clears


def test_get_shared_bucket_returns_singleton() -> None:
    a = get_shared_bucket()
    b = get_shared_bucket()
    assert a is b
    assert a.capacity == DEFAULT_BUCKET_CAPACITY
    assert a.refill_per_second == DEFAULT_REFILL_PER_SECOND


def test_reset_shared_bucket_rebuilds() -> None:
    a = get_shared_bucket()
    reset_shared_bucket()
    b = get_shared_bucket()
    assert a is not b


def test_get_shared_bucket_ignores_args_when_already_built() -> None:
    a = get_shared_bucket(capacity=12.0, refill_per_second=20.0)
    # Second call with different args should still return the original.
    b = get_shared_bucket(capacity=999.0, refill_per_second=999.0)
    assert a is b
    assert a.capacity == 12.0


def test_pending_waiters_count_reflects_blocking_acquirers() -> None:
    """Pending-waiter stat increments while threads are blocked acquiring."""
    bucket = TokenBucket(capacity=1.0, refill_per_second=0.5)  # ~2s per token
    bucket.acquire()  # drain
    started = threading.Event()

    def waiter() -> None:
        started.set()
        with contextlib.suppress(RateLimitTimeout):
            bucket.acquire(timeout=0.5)

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    started.wait(timeout=1.0)
    # Give the waiter a moment to enter the wait state.
    time.sleep(0.05)
    stats = bucket.stats()
    assert stats.pending_waiters >= 1
    t.join(timeout=1.0)
