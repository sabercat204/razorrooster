"""Alert tier ranking (T-MON-022; design §3.7).

Ordered list of tiers from highest priority to lowest:

1. ``resolution`` — market resolved (any outcome).
2. ``invalidation_triggered`` — a stated invalidation criterion fired.
3. ``material_shift`` — model or market probability moved by a
   ``material`` or ``major`` band.
4. ``precursor_shift`` — at least one precursor crossed its threshold.
5. ``time_decay`` — days_to_resolution at or below the configured
   threshold.

A follow-up may match multiple tiers; the highest-priority match is
the ``primary_alert_tier``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from razor_rooster.monitor.models import (
    AlertTier,
    PrecursorSnapshot,
    ShiftResult,
)

TIER_PRIORITY: Final[tuple[AlertTier, ...]] = (
    "resolution",
    "invalidation_triggered",
    "material_shift",
    "precursor_shift",
    "time_decay",
)


def compute_alert_tiers(
    *,
    resolution_status: str,
    invalidation_triggered_count: int,
    model_shift: ShiftResult,
    market_shift: ShiftResult,
    precursor_snapshot: Sequence[PrecursorSnapshot],
    time_decay_alert: bool,
) -> tuple[AlertTier | None, tuple[AlertTier, ...]]:
    """Return ``(primary_tier, all_applicable_tiers)``.

    The applicable tiers are returned in priority order so callers
    can persist them as a list for trace queries.
    """
    applicable: list[AlertTier] = []
    if resolution_status != "unresolved":
        applicable.append("resolution")
    if invalidation_triggered_count > 0:
        applicable.append("invalidation_triggered")
    if model_shift.band in ("material", "major") or market_shift.band in ("material", "major"):
        applicable.append("material_shift")
    if any(p.threshold_crossed for p in precursor_snapshot):
        applicable.append("precursor_shift")
    if time_decay_alert:
        applicable.append("time_decay")

    primary: AlertTier | None = applicable[0] if applicable else None
    return primary, tuple(applicable)


__all__ = ["TIER_PRIORITY", "compute_alert_tiers"]
