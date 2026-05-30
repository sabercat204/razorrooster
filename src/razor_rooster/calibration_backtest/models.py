"""Frozen dataclasses for calibration_backtest (T-CB-004; design §3.4, §3.7).

Defines the persistence-aligned and engine-internal dataclasses used by
the calibration_backtest subsystem:

* :class:`BacktestRun` — one row of ``backtest_runs``, the per-run header
  recording parameters, library/system pinning, status, and aggregate
  scoring outcomes.
* :class:`BacktestPrediction` — one row of ``backtest_predictions``,
  capturing a single replayed (class_id, condition_id) prediction with
  its polarity-corrected ``observed`` outcome and Brier contribution.
* :class:`BacktestTrace` — zstd-compressed per-prediction trace
  persisted to ``backtest_traces`` for forensic inspection.
* :class:`ScoreSummary`, :class:`ReliabilityDiagram`,
  :class:`ReliabilityBin` — engine-internal scoring outputs aggregated
  by the scoring engine for in-memory consumption.

Closed enumerations that govern persistence and engine state are
expressed as :class:`enum.StrEnum` subclasses so they round-trip cleanly
through JSON and DuckDB string columns. Validators raise
:class:`razor_rooster.calibration_backtest.errors.BacktestConfigError`
with field-qualified messages so structured logging surfaces actionable
context (REQ-CB-PERSIST-001, REQ-CB-FREEZE-002).

All dataclasses use ``@dataclass(frozen=True, slots=True)`` per the
project's frozen-dataclass convention; immutable sequences use
``tuple[...]`` rather than ``list[...]``. Persistence-side JSON columns
are typed as :class:`collections.abc.Mapping` so callers may pass any
read-only mapping without coercing to ``dict``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from razor_rooster.calibration_backtest.errors import BacktestConfigError


class BacktestStatus(StrEnum):
    """Lifecycle status of a backtest run (design §3.7)."""

    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


class PredictionStatus(StrEnum):
    """Per-prediction terminal status after replay (design §3.7)."""

    SCORED = "scored"
    SKIPPED = "skipped"


class PolarityValue(StrEnum):
    """Polarity applied to align a market resolution with the model class."""

    FORWARD = "forward"
    INVERTED = "inverted"


class PolaritySource(StrEnum):
    """Origin of the polarity decision recorded on a prediction."""

    COMPARISON_RESOLUTIONS = "comparison_resolutions"
    CURRENT_MAPPING_FALLBACK = "current_mapping_fallback"
    NO_POLARITY = "no_polarity"


class SkipReason(StrEnum):
    """Closed enumeration of reasons a prediction may be skipped (design §3.13)."""

    INSUFFICIENT_LAG = "insufficient_lag"
    SOURCE_DATA_NOT_FROZEN = "source_data_not_frozen"
    NO_POLARITY_RESOLUTION = "no_polarity_resolution"
    INVALID_RESOLUTION = "invalid_resolution"
    EXCEPTION = "exception"
    MAPPING_NOT_FOUND = "mapping_not_found"
    INSUFFICIENT_PRECURSOR_DATA = "insufficient_precursor_data"


class CompressionAlgorithm(StrEnum):
    """Compression algorithm marker for ``backtest_traces`` rows."""

    ZSTD = "zstd"


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    """One bin of a reliability diagram (design §3.6).

    Captures the predicted-probability interval ``[lower_p, upper_p]``,
    the count of predictions falling in the bin, the mean predicted
    probability across those predictions, and the empirical resolution
    rate observed for the bin. ``mean_predicted_p`` and ``empirical_rate``
    are ``None`` when ``count == 0``.
    """

    lower_p: float
    upper_p: float
    count: int
    mean_predicted_p: float | None
    empirical_rate: float | None

    def __post_init__(self) -> None:
        if not (0.0 <= self.lower_p <= 1.0):
            raise BacktestConfigError(
                f"ReliabilityBin.lower_p must be in [0.0, 1.0], got {self.lower_p!r}"
            )
        if not (self.lower_p < self.upper_p <= 1.0):
            raise BacktestConfigError(
                "ReliabilityBin.upper_p must satisfy lower_p < upper_p <= 1.0, "
                f"got lower_p={self.lower_p!r}, upper_p={self.upper_p!r}"
            )
        if self.count < 0:
            raise BacktestConfigError(f"ReliabilityBin.count must be >= 0, got {self.count!r}")
        if self.mean_predicted_p is not None and not (0.0 <= self.mean_predicted_p <= 1.0):
            raise BacktestConfigError(
                "ReliabilityBin.mean_predicted_p must be in [0.0, 1.0] when set, "
                f"got {self.mean_predicted_p!r}"
            )
        if self.empirical_rate is not None and not (0.0 <= self.empirical_rate <= 1.0):
            raise BacktestConfigError(
                "ReliabilityBin.empirical_rate must be in [0.0, 1.0] when set, "
                f"got {self.empirical_rate!r}"
            )


@dataclass(frozen=True, slots=True)
class ReliabilityDiagram:
    """A reliability diagram: ordered bins covering ``[0.0, 1.0]``."""

    bin_count: int
    bins: tuple[ReliabilityBin, ...]

    def __post_init__(self) -> None:
        if self.bin_count < 2:
            raise BacktestConfigError(
                f"ReliabilityDiagram.bin_count must be >= 2, got {self.bin_count!r}"
            )
        if len(self.bins) != self.bin_count:
            raise BacktestConfigError(
                "ReliabilityDiagram.bins length must equal bin_count "
                f"(bin_count={self.bin_count!r}, len(bins)={len(self.bins)!r})"
            )


@dataclass(frozen=True, slots=True)
class ScoreSummary:
    """Aggregated scoring outputs returned by the scoring engine (design §3.6).

    Bundles the overall Brier score with per-sector and per-class Brier
    breakdowns and the per-sector reliability diagrams. Sectors and
    classes that produced zero scoreable resolutions are surfaced via
    ``zero_resolutions_sectors`` / ``zero_resolutions_classes`` so the
    renderer can flag them in operator-facing output (REQ-CB-RENDER-002).
    """

    overall_brier: float
    per_sector_brier: Mapping[str, float]
    per_class_brier: Mapping[str, float]
    reliability_per_sector: Mapping[str, ReliabilityDiagram]
    zero_resolutions_sectors: tuple[str, ...]
    zero_resolutions_classes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not (0.0 <= self.overall_brier <= 1.0):
            raise BacktestConfigError(
                f"ScoreSummary.overall_brier must be in [0.0, 1.0], got {self.overall_brier!r}"
            )
        for sector, value in self.per_sector_brier.items():
            if not (0.0 <= value <= 1.0):
                raise BacktestConfigError(
                    f"ScoreSummary.per_sector_brier[{sector!r}] must be in [0.0, 1.0], "
                    f"got {value!r}"
                )
        for class_id, value in self.per_class_brier.items():
            if not (0.0 <= value <= 1.0):
                raise BacktestConfigError(
                    f"ScoreSummary.per_class_brier[{class_id!r}] must be in [0.0, 1.0], "
                    f"got {value!r}"
                )


@dataclass(frozen=True, slots=True)
class BacktestRun:
    """One row of ``backtest_runs`` (design §3.7, REQ-CB-PERSIST-001).

    Records the parameters and outcomes of a single calibration backtest:
    the (since_ts, until_ts) replay window, lag policy, scoped class /
    sector / venue tuples, library and system pinning, lifecycle status,
    aggregate counters, and the operator disclaimer version applied to
    rendered outputs.
    """

    run_id: str
    since_ts: datetime
    until_ts: datetime
    lag_days: int
    class_ids: tuple[str, ...]
    sectors: tuple[str, ...]
    venues: tuple[str, ...]
    library_version: int
    system_revision: str
    started_at: datetime
    completed_at: datetime | None
    status: BacktestStatus
    error_summary: str | None
    predictions_total: int
    predictions_scored: int
    predictions_skipped: int
    overall_brier: float | None
    summary_json: Mapping[str, Any] | None
    bin_count_global: int
    bin_count_per_sector: Mapping[str, int]
    fallback_polarity_count: int
    allow_recent: bool
    disclaimer_version: str

    def __post_init__(self) -> None:
        if not self.run_id:
            raise BacktestConfigError("BacktestRun.run_id must be non-empty")
        if self.lag_days < 1:
            raise BacktestConfigError(f"BacktestRun.lag_days must be >= 1, got {self.lag_days!r}")
        if self.since_ts >= self.until_ts:
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: since_ts must precede until_ts "
                f"(since_ts={self.since_ts.isoformat()}, until_ts={self.until_ts.isoformat()})"
            )
        if self.library_version < 1:
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: library_version must be >= 1, "
                f"got {self.library_version!r}"
            )
        if not self.system_revision:
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: system_revision must be non-empty"
            )
        if self.predictions_total < 0:
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: predictions_total must be >= 0, "
                f"got {self.predictions_total!r}"
            )
        if self.predictions_scored < 0:
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: predictions_scored must be >= 0, "
                f"got {self.predictions_scored!r}"
            )
        if self.predictions_skipped < 0:
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: predictions_skipped must be >= 0, "
                f"got {self.predictions_skipped!r}"
            )
        if self.predictions_scored > self.predictions_total:
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: predictions_scored "
                f"({self.predictions_scored!r}) must be <= predictions_total "
                f"({self.predictions_total!r})"
            )
        if self.predictions_skipped > self.predictions_total:
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: predictions_skipped "
                f"({self.predictions_skipped!r}) must be <= predictions_total "
                f"({self.predictions_total!r})"
            )
        if self.overall_brier is not None and not (0.0 <= self.overall_brier <= 1.0):
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: overall_brier must be in [0.0, 1.0] when set, "
                f"got {self.overall_brier!r}"
            )
        if self.bin_count_global < 2:
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: bin_count_global must be >= 2, "
                f"got {self.bin_count_global!r}"
            )
        for sector, count in self.bin_count_per_sector.items():
            if count < 2:
                raise BacktestConfigError(
                    f"BacktestRun {self.run_id!r}: bin_count_per_sector[{sector!r}] "
                    f"must be >= 2, got {count!r}"
                )
        if self.fallback_polarity_count < 0:
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: fallback_polarity_count must be >= 0, "
                f"got {self.fallback_polarity_count!r}"
            )
        if not self.disclaimer_version:
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: disclaimer_version must be non-empty"
            )
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise BacktestConfigError(
                f"BacktestRun {self.run_id!r}: completed_at must be >= started_at "
                f"(started_at={self.started_at.isoformat()}, "
                f"completed_at={self.completed_at.isoformat()})"
            )


@dataclass(frozen=True, slots=True)
class BacktestPrediction:
    """One row of ``backtest_predictions`` (design §3.7, REQ-CB-PERSIST-001).

    Captures a single replayed prediction: the model probability,
    polarity-corrected observed outcome, polarity provenance, the class
    definition_version pinned at replay time (REQ-CB-FREEZE-003), and
    the terminal status. ``status='skipped'`` rows must carry a
    ``skip_reason`` from the closed :class:`SkipReason` enumeration; the
    converse — ``status='scored'`` — must not carry a ``skip_reason``.
    """

    run_id: str
    prediction_id: str
    class_id: str
    condition_id: str
    venue: str
    sector: str
    prediction_ts: datetime
    resolution_ts: datetime
    model_p: float | None
    observed: float | None
    polarity: PolarityValue | None
    polarity_source: PolaritySource
    mapping_mismatch_warning: bool
    definition_version: int
    status: PredictionStatus
    skip_reason: SkipReason | None
    brier_contribution: float | None

    def __post_init__(self) -> None:
        if not self.run_id:
            raise BacktestConfigError("BacktestPrediction.run_id must be non-empty")
        if not self.prediction_id:
            raise BacktestConfigError("BacktestPrediction.prediction_id must be non-empty")
        if not self.class_id:
            raise BacktestConfigError(
                f"BacktestPrediction {self.prediction_id!r}: class_id must be non-empty"
            )
        if not self.condition_id:
            raise BacktestConfigError(
                f"BacktestPrediction {self.prediction_id!r}: condition_id must be non-empty"
            )
        if not self.venue:
            raise BacktestConfigError(
                f"BacktestPrediction {self.prediction_id!r}: venue must be non-empty"
            )
        if not self.sector:
            raise BacktestConfigError(
                f"BacktestPrediction {self.prediction_id!r}: sector must be non-empty"
            )
        if self.definition_version < 1:
            raise BacktestConfigError(
                f"BacktestPrediction {self.prediction_id!r}: definition_version must be >= 1, "
                f"got {self.definition_version!r}"
            )
        if self.model_p is not None and not (0.0 <= self.model_p <= 1.0):
            raise BacktestConfigError(
                f"BacktestPrediction {self.prediction_id!r}: model_p must be in [0.0, 1.0] "
                f"when set, got {self.model_p!r}"
            )
        if self.observed is not None and not (0.0 <= self.observed <= 1.0):
            raise BacktestConfigError(
                f"BacktestPrediction {self.prediction_id!r}: observed must be in [0.0, 1.0] "
                f"when set, got {self.observed!r}"
            )
        if self.brier_contribution is not None and not (0.0 <= self.brier_contribution <= 1.0):
            raise BacktestConfigError(
                f"BacktestPrediction {self.prediction_id!r}: brier_contribution must be in "
                f"[0.0, 1.0] when set, got {self.brier_contribution!r}"
            )
        if self.status is PredictionStatus.SKIPPED and self.skip_reason is None:
            raise BacktestConfigError(
                f"BacktestPrediction {self.prediction_id!r}: status='skipped' requires "
                "skip_reason to be set"
            )
        if self.status is PredictionStatus.SCORED and self.skip_reason is not None:
            raise BacktestConfigError(
                f"BacktestPrediction {self.prediction_id!r}: status='scored' must not carry "
                f"skip_reason (got {self.skip_reason!r})"
            )


@dataclass(frozen=True, slots=True)
class BacktestTrace:
    """One row of ``backtest_traces`` (design §3.7).

    Stores a zstd-compressed JSON trace blob alongside the algorithm
    marker and the decompressed-size hint persisted for budgeting and
    integrity checks (REQ-CB-PERSIST-003).
    """

    run_id: str
    prediction_id: str
    trace_json_compressed: bytes
    decompressed_size_bytes: int
    compression_algorithm: CompressionAlgorithm = CompressionAlgorithm.ZSTD

    def __post_init__(self) -> None:
        if not self.run_id:
            raise BacktestConfigError("BacktestTrace.run_id must be non-empty")
        if not self.prediction_id:
            raise BacktestConfigError("BacktestTrace.prediction_id must be non-empty")
        if self.decompressed_size_bytes < 0:
            raise BacktestConfigError(
                f"BacktestTrace {self.prediction_id!r}: decompressed_size_bytes must be >= 0, "
                f"got {self.decompressed_size_bytes!r}"
            )


__all__ = [
    "BacktestPrediction",
    "BacktestRun",
    "BacktestStatus",
    "BacktestTrace",
    "CompressionAlgorithm",
    "PolaritySource",
    "PolarityValue",
    "PredictionStatus",
    "ReliabilityBin",
    "ReliabilityDiagram",
    "ScoreSummary",
    "SkipReason",
]
