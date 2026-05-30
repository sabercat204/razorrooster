"""Time-to-resolution helpers (T-PE-035; REQ-PE-CMP-008)."""

from __future__ import annotations

from datetime import datetime


def days_remaining(end_date: datetime | None, *, now: datetime) -> int | None:
    """Return whole days remaining until ``end_date``, or None when missing.

    Negative values are clamped to zero (resolution already passed).
    """
    if end_date is None:
        return None
    delta = end_date - now
    return max(0, delta.days)


def is_long(days: int | None, threshold: int) -> bool:
    """True when ``days`` is set and exceeds the threshold."""
    if days is None:
        return False
    return days > threshold
