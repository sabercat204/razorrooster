"""T-PE-035 — time-to-resolution tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from razor_rooster.position_engine.engines.time_to_resolution import (
    days_remaining,
    is_long,
)


def test_days_remaining_basic() -> None:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    end = now + timedelta(days=180)
    assert days_remaining(end, now=now) == 180


def test_days_remaining_none_when_no_end_date() -> None:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    assert days_remaining(None, now=now) is None


def test_days_remaining_clamps_negative_to_zero() -> None:
    """End date already passed → 0 days remaining."""
    now = datetime(2026, 5, 15, tzinfo=UTC)
    end = now - timedelta(days=10)
    assert days_remaining(end, now=now) == 0


def test_is_long_true_when_above_threshold() -> None:
    assert is_long(400, threshold=365) is True


def test_is_long_false_when_below_threshold() -> None:
    assert is_long(180, threshold=365) is False


def test_is_long_at_threshold_exact() -> None:
    """Exactly at threshold counts as not long."""
    assert is_long(365, threshold=365) is False


def test_is_long_none_returns_false() -> None:
    """No end date → not flagged as long."""
    assert is_long(None, threshold=365) is False
