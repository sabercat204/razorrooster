"""Typed dataclasses for position_engine outputs (T-PE-010 / T-PE-011)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

WatchStateValue = Literal["watching", "acted_on", "dismissed", "expired"]
SetBy = Literal["operator", "system"]

# Venue discriminator (T-PE-101 / cross-subsystem migration for the
# Kalshi connector). Mirrors ``mispricing_detector.models.Venue``;
# duplicated here rather than imported to avoid a hard dependency
# direction between position_engine and mispricing_detector. Kept in
# lockstep — if ``Venue`` gains a third value, both modules must update.
Venue = Literal["polymarket", "kalshi"]


@dataclass(frozen=True, slots=True)
class BankrollConfig:
    """One row of ``bankroll_config``. Latest by ``effective_at`` wins."""

    config_id: str
    analytical_bankroll_usd: float
    max_single_position_pct: float
    kelly_fraction_default: float
    min_edge_threshold: float
    effective_at: datetime
    updated_by: str = "operator"
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class AnalysisCycle:
    """One row of ``analysis_cycles``."""

    cycle_id: str
    started_at: datetime
    completed_at: datetime | None
    bankroll_config_id: str
    analyses_total: int
    analyses_with_positive_kelly: int
    analyses_clamped_by_cap: int
    analyses_clamped_by_liquidity: int
    duration_seconds: float | None = None
    error_summary: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Analysis:
    """One row of ``analyses``.

    ``condition_id`` is a venue-specific market identifier per the
    ``Venue`` literal docstring above: a Polymarket condition_id when
    ``venue='polymarket'``, a Kalshi ticker when ``venue='kalshi'``.
    """

    analysis_id: str
    cycle_id: str
    comparison_id: str
    class_id: str
    condition_id: str
    bankroll_config_id: str
    model_probability: float
    market_probability: float | None
    kelly_unclamped: float
    kelly_negative: bool
    kelly_clamped_by_max_cap: bool
    kelly_clamped_by_liquidity: bool
    suggested_fraction: float
    suggested_dollar_size: float
    ev_per_dollar: float | None
    bankroll_after_1_loss_pct: float
    bankroll_after_3_losses_pct: float
    bankroll_after_5_losses_pct: float
    suggested_pct_of_24h_volume: float | None
    days_to_resolution: int | None
    long_time_to_resolution: bool
    sub_threshold: bool
    sensitivity_analysis: Mapping[str, Any] | None
    invalidation_criteria: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    low_signature_confidence: bool = False
    source_stale_warning: bool = False
    library_stale_warning: bool = False
    definition_drift_warning: bool = False
    low_mapping_confidence: bool = False
    low_liquidity: bool = False
    error: str | None = None
    computed_at: datetime | None = None
    venue: Venue = "polymarket"


@dataclass(frozen=True, slots=True)
class AnalysisTrace:
    """One row of ``analysis_traces`` — the rendered + structured form."""

    analysis_id: str
    rendered_text: str
    structured_dict: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WatchState:
    """One row of ``watch_states`` — append-only log per analysis."""

    state_id: str
    analysis_id: str
    state: WatchStateValue
    set_at: datetime
    set_by: SetBy = "operator"
    notes: str | None = None
