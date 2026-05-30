"""T-KSI-030 — token-bucket rate limiter acceptance tests."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from razor_rooster.kalshi_connector.client.rate_limit import (
    DEFAULT_BUCKET_CAPACITY,
    DEFAULT_REFILL_PER_SECOND,
    RateLimitTimeout,
    TokenBucket,
    get_shared_bucket,
    reset_shared_bucket,
)
from razor_rooster.kalshi_connector.config.loader import KalshiConfig


@pytest.fixture(autouse=True)
def _reset_shared() -> Iterator[None]:
    yield
    reset_shared_bucket()


def test_capacity_and_refill_are_configurable() -> None:
    bucket = TokenBucket(capacity=200.0, refill_per_second=200.0)
    assert bucket.capacity == 200.0
    assert bucket.refill_per_second == 200.0


def test_bucket_drains_under_load_and_refills() -> None:
    """Bucket capacity 50 / refill 50 per sec; burst then wait."""
    bucket = TokenBucket(capacity=50.0, refill_per_second=50.0)
    # Burst-drain.
    for _ in range(50):
        bucket.acquire(1)
    stats = bucket.stats()
    assert stats.tokens_available <= 1.0
    # Refill window ~0.5s should add ~25 tokens back.
    time.sleep(0.6)
    stats_after = bucket.stats()
    assert stats_after.tokens_available > 20.0


def test_acquire_blocks_when_bucket_drained() -> None:
    """A fresh acquire after draining waits for refill."""
    bucket = TokenBucket(capacity=2.0, refill_per_second=10.0)
    bucket.acquire(2.0)  # drains to 0
    start = time.monotonic()
    bucket.acquire(1.0)  # must wait ~0.1s for refill
    elapsed = time.monotonic() - start
    assert elapsed >= 0.05


def test_acquire_timeout_raises() -> None:
    bucket = TokenBucket(capacity=1.0, refill_per_second=0.01)
    bucket.acquire(1.0)
    with pytest.raises(RateLimitTimeout):
        bucket.acquire(1.0, timeout=0.1)


def test_acquire_zero_or_negative_rejected() -> None:
    bucket = TokenBucket(capacity=10.0, refill_per_second=10.0)
    with pytest.raises(ValueError, match="tokens must be > 0"):
        bucket.acquire(0)
    with pytest.raises(ValueError, match="tokens must be > 0"):
        bucket.acquire(-1)


def test_acquire_more_than_capacity_rejected() -> None:
    bucket = TokenBucket(capacity=10.0, refill_per_second=10.0)
    with pytest.raises(ValueError, match="exceeds capacity"):
        bucket.acquire(11.0)


def test_constructor_validates_inputs() -> None:
    with pytest.raises(ValueError, match="capacity must be > 0"):
        TokenBucket(capacity=0)
    with pytest.raises(ValueError, match="refill_per_second must be > 0"):
        TokenBucket(capacity=10, refill_per_second=0)


def test_endpoint_aware_acquire_charges_per_endpoint_cost() -> None:
    """acquire_for_endpoint charges the documented cost (10) for known endpoints."""
    bucket = TokenBucket(capacity=100.0, refill_per_second=100.0)
    cost = bucket.acquire_for_endpoint("/markets")
    assert cost == 10
    stats = bucket.stats()
    # 100 - 10 = 90, allow small float tolerance
    assert stats.tokens_available == pytest.approx(90.0, abs=1.0)


def test_endpoint_aware_acquire_unknown_path_uses_default(caplog: pytest.LogCaptureFixture) -> None:
    """Unknown paths charge default cost and log the unrecognized path."""
    bucket = TokenBucket(capacity=100.0, refill_per_second=100.0)
    import logging as _logging

    with caplog.at_level(_logging.WARNING):
        cost = bucket.acquire_for_endpoint("/something/new")
    assert cost == 10
    assert any("/something/new" in r.message for r in caplog.records)


def test_drain_zeros_tokens() -> None:
    bucket = TokenBucket(capacity=100.0, refill_per_second=10.0)
    bucket.drain()
    stats = bucket.stats()
    # stats() refills lazily on entry; allow a sub-millisecond tolerance.
    assert stats.tokens_available < 0.01


def test_parallel_acquirers_serialize() -> None:
    """Many threads acquiring simultaneously do not exceed the bucket cap."""
    bucket = TokenBucket(capacity=10.0, refill_per_second=1000.0)
    completed: list[float] = []
    completed_lock = threading.Lock()

    def worker() -> None:
        bucket.acquire(1.0)
        with completed_lock:
            completed.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(50)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    elapsed = time.monotonic() - start
    # 50 acquires from a bucket capped at 10 with 1000/sec refill should
    # complete in well under a second.
    assert len(completed) == 50
    assert elapsed < 2.0


def test_from_config_sizes_to_headroom_at_basic_tier() -> None:
    cfg = KalshiConfig(version=1)  # default Basic tier, headroom 0.5
    bucket = TokenBucket.from_config(cfg)
    # 200 * 0.5 = 100
    assert bucket.capacity == 100.0
    assert bucket.refill_per_second == 100.0


def test_from_config_scales_with_tier() -> None:
    """Switching tier from Basic (200) to Advanced (300) reconfigures bucket."""
    basic = KalshiConfig(version=1)  # tier=Basic default
    basic_bucket = TokenBucket.from_config(basic)
    advanced = KalshiConfig(version=1, tier="Advanced")
    advanced_bucket = TokenBucket.from_config(advanced)
    assert basic_bucket.capacity == 100.0  # 50% of 200
    assert advanced_bucket.capacity == 150.0  # 50% of 300


def test_from_config_premier_tier() -> None:
    cfg = KalshiConfig(version=1, tier="Premier")
    bucket = TokenBucket.from_config(cfg)
    assert bucket.capacity == 500.0  # 50% of 1000


def test_get_shared_bucket_singleton() -> None:
    """get_shared_bucket returns the same instance across calls."""
    a = get_shared_bucket()
    b = get_shared_bucket()
    assert a is b


def test_get_shared_bucket_uses_default_when_no_config() -> None:
    bucket = get_shared_bucket()
    assert bucket.capacity == DEFAULT_BUCKET_CAPACITY
    assert bucket.refill_per_second == DEFAULT_REFILL_PER_SECOND


def test_reset_shared_bucket_lets_next_call_reconfigure() -> None:
    cfg = KalshiConfig(version=1, tier="Advanced")
    a = get_shared_bucket()
    reset_shared_bucket()
    b = get_shared_bucket(config=cfg)
    assert a is not b
    assert b.capacity == 150.0  # Advanced headroom


def test_stats_snapshot_after_acquire() -> None:
    bucket = TokenBucket(capacity=10.0, refill_per_second=10.0)
    bucket.acquire(3.0)
    stats = bucket.stats()
    assert stats.capacity == 10.0
    assert stats.tokens_available <= 7.5  # may have refilled slightly
    assert stats.pending_waiters == 0
