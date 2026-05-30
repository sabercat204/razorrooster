"""Timestamp normalization helpers (T-031, REQ-NORM-002).

All canonical-schema timestamps are stored in UTC (REQ-NORM-002). Source
data arrives in many shapes:

- Already tz-aware UTC datetimes from JSON ISO-8601 parsers.
- Tz-aware datetimes in non-UTC zones (e.g., Eastern Time from a
  US-government source).
- Tz-naive datetimes carrying an implicit zone the source documents
  separately.

:func:`to_utc` accepts these and returns a tz-aware UTC datetime. For naive
inputs, the caller passes ``hint_tz`` declaring the implicit zone; if no
hint is given, we treat naive inputs as already-UTC and log a warning so
operator can spot accidental zone-stripping.

Normalization preserves the original source-native timezone string in the
record's payload (the connector's responsibility). This module's job is
just the conversion to UTC.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


def to_utc(value: datetime, *, hint_tz: str | None = None) -> datetime:
    """Convert a datetime to tz-aware UTC.

    Behavior matrix:

    - Tz-aware datetime → converted to UTC.
    - Tz-naive datetime + ``hint_tz`` → interpreted in ``hint_tz``, then UTC.
    - Tz-naive datetime + no hint → assumed UTC (with a warning logged).

    Raises:
        TypeError: if ``value`` is not a :class:`datetime`.
        ValueError: if ``hint_tz`` is given but doesn't resolve to a known zone.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"to_utc expected a datetime, got {type(value).__name__}")

    if value.tzinfo is not None:
        if value.tzinfo == UTC:
            return value
        return value.astimezone(UTC)

    if hint_tz is not None:
        try:
            zone = ZoneInfo(hint_tz)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {hint_tz!r}") from exc
        return value.replace(tzinfo=zone).astimezone(UTC)

    logger.warning(
        "to_utc called on a naive datetime with no hint_tz; treating as UTC. value=%s",
        value.isoformat(),
    )
    return value.replace(tzinfo=UTC)
