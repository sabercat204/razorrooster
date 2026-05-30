"""Typed dataclasses for monitor outputs (T-MON-010 / T-MON-011)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

ShiftBand = Literal["none", "minor", "material", "major"]
ResolutionStatus = Literal["unresolved", "resolved_yes", "resolved_no", "resolved_invalid"]
AlertTier = Literal[
    "resolution",
    "invalidation_triggered",
    "material_shift",
    "precursor_shift",
    "time_decay",
]
InvalidationStatus = Literal["triggered", "not_triggered", "cannot_evaluate"]

# Venue discriminator (T-MON-101 / cross-subsystem migration for the
# Kalshi connector). Mirrors ``mispricing_detector.models.Venue`` and
# ``position_engine.models.Venue``; duplicated rather than imported to
# keep the dependency direction one-way (monitor reads upstream, never
# imports). Kept in lockstep with the other two definitions.
Venue = Literal["polymarket", "kalshi"]


@dataclass(frozen=True, slots=True)
class MonitorCycle:
    """One row of ``monitor_cycles``."""

    cycle_id: str
    started_at: datetime
    completed_at: datetime | None
    follow_ups_total: int
    follow_ups_with_alerts: int
    alerts_by_tier: Mapping[str, int]
    duration_seconds: float | None = None
    error_summary: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class FollowUp:
    """One row of ``follow_ups``."""

    follow_up_id: str
    cycle_id: str
    analysis_id: str
    analysis_model_p: float
    analysis_market_p: float | None
    analysis_computed_at: datetime
    current_scan_id: str | None
    current_model_p: float | None
    current_model_ci: tuple[float, float] | None
    current_market_p: float | None
    current_market_snapshot_ts: datetime | None
    model_probability_shift: float | None
    model_shift_band: ShiftBand | None
    market_probability_shift: float | None
    market_shift_band: ShiftBand | None
    precursor_snapshot: Sequence[Mapping[str, Any]]
    days_since_analysis: int
    days_to_resolution: int | None
    time_decay_alert: bool
    invalidation_evaluations: Sequence[Mapping[str, Any]]
    invalidation_triggered_count: int
    resolution_status: ResolutionStatus
    recommended_review: bool
    primary_alert_tier: AlertTier | None
    alert_tiers: Sequence[AlertTier] = field(default_factory=tuple)
    reasoning_text: str = ""
    source_stale_warning: bool = False
    library_stale_warning: bool = False
    error: str | None = None
    computed_at: datetime | None = None
    venue: Venue = "polymarket"


@dataclass(frozen=True, slots=True)
class FollowUpNote:
    """One row of ``follow_up_notes``."""

    note_id: str
    follow_up_id: str
    note_text: str
    set_at: datetime
    set_by: str = "operator"


@dataclass(frozen=True, slots=True)
class ShiftResult:
    """Per-dimension change-detection result."""

    value: float | None
    band: ShiftBand | None


@dataclass(frozen=True, slots=True)
class PrecursorSnapshot:
    """One precursor's analysis-time + current-time observation."""

    variable_id: str
    title: str
    threshold: float | None
    direction: str
    analysis_value: float | None
    current_value: float | None
    analysis_fired: bool
    current_fired: bool

    @property
    def threshold_crossed(self) -> bool:
        """True when the variable changed which side of the threshold it sits on."""
        return self.analysis_fired != self.current_fired


@dataclass(frozen=True, slots=True)
class InvalidationEvaluation:
    """One invalidation criterion + its current evaluation."""

    criterion: Mapping[str, Any]
    status: InvalidationStatus
    current_value: float | None = None
    reason: str | None = None
