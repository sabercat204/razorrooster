"""Typed dataclasses for mispricing_detector outputs (T-MD-010 / T-MD-011).

Frozen records mirror the persisted rows. The persistence helpers in
:mod:`mispricing_detector.persistence.operations` consume / produce
them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

MappingType = Literal["direct", "proxy", "aggregate"]
MappingConfidence = Literal["exact", "inferred", "low"]
Polarity = Literal["aligned", "inverted"]
MappedBy = Literal["operator", "auto"]
ResolutionOutcome = Literal["yes", "no", "invalid"]

# Venue discriminator (T-MD-101 / cross-subsystem migration for the
# Kalshi connector). 'polymarket' rows are pre-existing; 'kalshi' rows
# arrive once the Kalshi connector is wired into the mispricing cycle
# (T-KSI-061). The semantics of ``Comparison.condition_id`` and
# ``ClassMarketMapping.condition_id`` differ by venue:
#
# - venue='polymarket': condition_id holds a Polymarket condition_id
#   (0x-prefixed hex).
# - venue='kalshi': condition_id holds a Kalshi ticker (e.g.
#   'KXCPI-26AUG-T2.5').
#
# The column is not renamed because rename is a destructive
# backward-incompatible operation for downstream tools that may already
# query it. The dual semantics live in this comment and on each
# dataclass docstring.
Venue = Literal["polymarket", "kalshi"]


@dataclass(frozen=True, slots=True)
class ClassMarketMapping:
    """One row of ``class_market_mappings``.

    ``condition_id`` is a venue-specific market identifier: a Polymarket
    condition_id when ``venue='polymarket'``, a Kalshi ticker when
    ``venue='kalshi'``.
    """

    mapping_id: str
    class_id: str
    condition_id: str
    mapping_type: MappingType
    mapping_confidence: MappingConfidence
    polarity: Polarity = "aligned"
    mapped_by: MappedBy = "operator"
    mapped_at: datetime | None = None
    removed_at: datetime | None = None
    notes: str | None = None
    venue: Venue = "polymarket"


@dataclass(frozen=True, slots=True)
class ComparisonCycle:
    """One row of ``comparison_cycles``."""

    cycle_id: str
    started_at: datetime
    completed_at: datetime | None
    comparisons_total: int
    surfaced_count: int
    suppressed_breakdown: Mapping[str, int]
    library_version_at_cycle: int
    scan_id_consumed: str
    error_summary: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Comparison:
    """One row of ``comparisons`` (model-vs-market output).

    ``condition_id`` is a venue-specific market identifier per ``Venue``
    docstring above. ``outcome_token_id`` is Polymarket-specific and is
    expected to be empty (not NULL — column is NOT NULL in the schema)
    for Kalshi rows; convention is to set it to the same value as
    ``condition_id`` for Kalshi to keep the column populated.
    """

    comparison_id: str
    cycle_id: str
    mapping_id: str
    class_id: str
    condition_id: str
    outcome_token_id: str
    polarity: Polarity
    scan_id: str
    model_probability: float
    model_ci_lower: float
    model_ci_upper: float
    market_probability: float | None
    market_best_bid: float | None
    market_best_ask: float | None
    market_last_trade_price: float | None
    market_volume_24h: float | None
    market_spread_bps: int | None
    market_snapshot_ts: datetime | None
    delta: float | None
    log_odds_delta: float | None
    ci_overlap: bool
    expected_value: float | None
    confidence_weighted_score: float | None
    surfaced: bool
    suppression_reasons: tuple[str, ...] = ()
    low_signature_confidence: bool = False
    source_stale_warning: bool = False
    library_stale_warning: bool = False
    definition_drift_warning: bool = False
    stale_market_price: bool = False
    no_market_price: bool = False
    degenerate_orderbook: bool = False
    low_liquidity: bool = False
    low_mapping_confidence: bool = False
    error: str | None = None
    computed_at: datetime | None = None
    venue: Venue = "polymarket"


@dataclass(frozen=True, slots=True)
class ComparisonTrace:
    """One row of ``comparison_traces``."""

    comparison_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComparisonResolution:
    """One row of ``comparison_resolutions`` (calibration scaffolding).

    ``condition_id`` is a venue-specific market identifier per ``Venue``
    docstring above.
    """

    comparison_id: str
    condition_id: str
    resolution_outcome: ResolutionOutcome
    resolution_ts: datetime
    model_probability_at_comparison: float
    market_probability_at_comparison: float | None
    polarity_at_comparison: Polarity
    outcome_observed: int  # 1 or 0, after polarity adjustment
    linked_at: datetime
    venue: Venue = "polymarket"
