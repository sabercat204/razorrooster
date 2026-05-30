"""Core event-class definition (T-PL-010; design §3.3).

An :class:`EventClass` is the unit of pattern_library content. Each
class is a Python module under ``classes/`` exposing a module-level
``CLASS = EventClass(...)``. The class registry auto-discovers them.

Two enum families:

- :class:`Sector` — the six Razor-Rooster domain sectors plus a
  ``CROSS_CUTTING`` sentinel for meta-classes.
- Configuration enums (:class:`BaselineStrategy`, :class:`Normalization`,
  :class:`ThresholdMethod`) for per-class knobs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import duckdb
    import pandas as pd


class Sector(StrEnum):
    """The six Razor-Rooster sectors plus a cross-cutting sentinel."""

    PUBLIC_HEALTH = "public_health"
    GEOPOLITICAL = "geopolitical"
    REGULATORY = "regulatory"
    COMMODITY = "commodity"
    CLIMATE = "climate"
    INFRASTRUCTURE_ENERGY = "infrastructure_energy"
    CROSS_CUTTING = "cross_cutting"


class BaselineStrategy(StrEnum):
    """How to sample non-event windows for signature comparison (OQ-PL-003)."""

    STRATIFIED_RANDOM = "stratified_random"
    UNIFORM_RANDOM = "uniform_random"
    REGULAR_GRID = "regular_grid"


class Normalization(StrEnum):
    """How to normalize an analogue feature within a population (design §3.5)."""

    ZSCORE = "zscore"
    PERCENTILE_RANK = "percentile_rank"
    NONE = "none"


class ThresholdMethod(StrEnum):
    """How to discover the threshold for a precursor variable (OQ-PL-002)."""

    YOUDEN_J = "youden_j"
    F1 = "f1"
    QUANTILE_95 = "quantile_95"
    MANUAL = "manual"


# Type aliases for the callables embedded in class definitions. Using
# ``Any`` for the DuckDB connection avoids forcing test fixtures to use
# the real library at module-import time; the registry validation step
# catches incorrect signatures at class-registration time.
OccurrenceQuery = Callable[["duckdb.DuckDBPyConnection"], "pd.DataFrame"]


@dataclass(frozen=True, slots=True)
class PrecursorVariable:
    """One precursor variable in an event class's signature (design §3.3)."""

    variable_id: str
    title: str
    query: Callable[..., Any]  # (conn, window_start, window_end) -> pd.Series
    direction: Literal["high_signals_event", "low_signals_event"]
    lead_time_window: timedelta = field(default_factory=lambda: timedelta(days=180))
    threshold_method: ThresholdMethod = ThresholdMethod.YOUDEN_J
    manual_threshold: float | None = None

    def __post_init__(self) -> None:
        if not self.variable_id:
            raise ValueError("PrecursorVariable.variable_id must be non-empty")
        if not self.title:
            raise ValueError("PrecursorVariable.title must be non-empty")
        if self.lead_time_window.total_seconds() <= 0:
            raise ValueError(
                f"PrecursorVariable {self.variable_id!r}: lead_time_window must be positive"
            )
        if self.threshold_method == ThresholdMethod.MANUAL and self.manual_threshold is None:
            raise ValueError(
                f"PrecursorVariable {self.variable_id!r}: threshold_method='manual' "
                "requires manual_threshold to be set"
            )
        if self.threshold_method != ThresholdMethod.MANUAL and self.manual_threshold is not None:
            raise ValueError(
                f"PrecursorVariable {self.variable_id!r}: manual_threshold may only "
                "be set when threshold_method='manual'"
            )


@dataclass(frozen=True, slots=True)
class AnalogueFeature:
    """One feature in an event class's analogue feature space (design §3.3)."""

    feature_id: str
    query: Callable[..., Any]  # (conn, ts) -> float
    normalization: Normalization = Normalization.ZSCORE
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not self.feature_id:
            raise ValueError("AnalogueFeature.feature_id must be non-empty")
        if self.weight <= 0:
            raise ValueError(
                f"AnalogueFeature {self.feature_id!r}: weight must be > 0, got {self.weight!r}"
            )


@dataclass(frozen=True, slots=True)
class EventClass:
    """One operator-defined event class (design §3.3, OQ-PL-006).

    v1 supports binary outcome classes only; ``outcome_type`` is fixed
    to ``'binary'`` and the registry rejects other values.
    """

    class_id: str
    title: str
    description: str
    domain_sector: Sector
    occurrence_query: OccurrenceQuery
    secondary_sectors: tuple[Sector, ...] = ()
    definition_version: int = 1
    outcome_type: Literal["binary"] = "binary"
    precursors: tuple[PrecursorVariable, ...] = ()
    analogue_features: tuple[AnalogueFeature, ...] = ()
    base_rate_window_default: timedelta = field(default_factory=lambda: timedelta(days=365 * 10))
    refractory_months: int = 12
    baseline_strategy: BaselineStrategy = BaselineStrategy.STRATIFIED_RANDOM
    baseline_sample_size: int = 1_000
    prior_alpha: float = 0.5
    prior_beta: float = 0.5

    def __post_init__(self) -> None:
        if not self.class_id:
            raise ValueError("EventClass.class_id must be non-empty")
        # class_id should be a valid identifier so it doubles as a Python module name.
        if not self.class_id.replace("_", "").isalnum():
            raise ValueError(
                f"EventClass.class_id must be alphanumeric+underscore, got {self.class_id!r}"
            )
        if not self.title:
            raise ValueError(f"EventClass {self.class_id!r}: title must be non-empty")
        if not self.description:
            raise ValueError(f"EventClass {self.class_id!r}: description must be non-empty")
        if self.definition_version < 1:
            raise ValueError(
                f"EventClass {self.class_id!r}: definition_version must be >= 1, "
                f"got {self.definition_version!r}"
            )
        if self.refractory_months < 1:
            raise ValueError(
                f"EventClass {self.class_id!r}: refractory_months must be >= 1, "
                f"got {self.refractory_months!r}"
            )
        if self.baseline_sample_size < 10:
            raise ValueError(
                f"EventClass {self.class_id!r}: baseline_sample_size must be >= 10, "
                f"got {self.baseline_sample_size!r}"
            )
        if self.prior_alpha <= 0 or self.prior_beta <= 0:
            raise ValueError(
                f"EventClass {self.class_id!r}: prior_alpha and prior_beta must be > 0"
            )
        if self.outcome_type != "binary":
            raise ValueError(
                f"EventClass {self.class_id!r}: outcome_type must be 'binary' in v1; "
                "see OQ-PL-006 resolution"
            )
        # secondary_sectors must not duplicate the primary
        if self.domain_sector in self.secondary_sectors:
            raise ValueError(
                f"EventClass {self.class_id!r}: domain_sector {self.domain_sector.value!r} "
                "must not appear in secondary_sectors"
            )
        # precursor and feature ids must be unique within the class
        precursor_ids = [p.variable_id for p in self.precursors]
        if len(set(precursor_ids)) != len(precursor_ids):
            raise ValueError(f"EventClass {self.class_id!r}: precursor variable_ids must be unique")
        feature_ids = [f.feature_id for f in self.analogue_features]
        if len(set(feature_ids)) != len(feature_ids):
            raise ValueError(f"EventClass {self.class_id!r}: analogue feature_ids must be unique")
