"""Shared helpers for Kalshi sync operations (T-KSI-041..T-KSI-045).

Connector version, JSON encoding, NULL-preservation timestamp coercion,
and the staging-merge schema-aware helpers used across the per-table
sync modules.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Final

# Connector version stamped on every persisted row's ``connector_version``
# column. Bump when parsing semantics change in a way downstream
# consumers must detect.
CONNECTOR_VERSION: Final[str] = "kalshi@0.1.0"


def encode_payload(payload: Any) -> str:
    """Serialize a Kalshi response object to ``source_payload_json``."""
    return json.dumps(payload, default=str)


def coerce_dt(value: Any) -> datetime | None:
    """Return ``value`` as an aware UTC ``datetime`` or None.

    Accepts ``datetime``, ISO 8601 strings, and epoch seconds. Naive
    datetimes are treated as UTC.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        cleaned = value.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


__all__ = ["CONNECTOR_VERSION", "coerce_dt", "encode_payload"]
