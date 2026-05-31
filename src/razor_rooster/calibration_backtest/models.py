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

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    """Polarity applied to align a market resolution with the model class.

    The string values mirror the v1 ``backtest_predictions.polarity``
    CHECK constraint (``'direct' | 'inverted'``) so a
    :class:`BacktestPrediction` can be persisted without a string
    rewrite at the persistence-layer boundary. The replay loop's
    :data:`razor_rooster.calibration_backtest.engines.replay._POLARITY_DIRECT`
    set normalises the upstream synonym ``'aligned'`` (the literal
    ``mispricing_detector`` writes today) to :data:`FORWARD`.
    """

    FORWARD = "direct"
    INVERTED = "inverted"


class PolaritySource(StrEnum):
    """Origin of the polarity decision recorded on a prediction."""

    COMPARISON_RESOLUTIONS = "comparison_resolutions"
    CURRENT_MAPPING_FALLBACK = "current_mapping_fallback"
    NO_POLARITY = "no_polarity"


class SkipReason(StrEnum):
    """Closed enumeration of reasons a prediction may be skipped (design §3.13).

    String values mirror the v1 ``backtest_predictions.skip_reason`` CHECK
    constraint (design §3.13) so a :class:`BacktestPrediction` can be
    persisted without a string rewrite at the persistence-layer
    boundary. The Python identifier
    :data:`INSUFFICIENT_PRECURSOR_DATA` carries the descriptive name used
    in code paths and error messages, while the on-disk value
    ``'insufficient_data'`` matches the design doc.
    """

    INSUFFICIENT_LAG = "insufficient_lag"
    SOURCE_DATA_NOT_FROZEN = "source_data_not_frozen"
    NO_POLARITY_RESOLUTION = "no_polarity_resolution"
    INVALID_RESOLUTION = "invalid_resolution"
    EXCEPTION = "exception"
    MAPPING_NOT_FOUND = "mapping_not_found"
    INSUFFICIENT_PRECURSOR_DATA = "insufficient_data"


class CompressionAlgorithm(StrEnum):
    """Compression algorithm marker for ``backtest_traces`` rows."""

    ZSTD = "zstd"


class PresentIn(StrEnum):
    """Cell presence marker for run-vs-run comparisons (T-CB-024; design §3.7).

    Each :class:`CompareCell` carries a ``present_in`` discriminator
    indicating whether the underlying ``(sector, class_id)`` cell was
    observed in run A only, run B only, or both. Asymmetric cells
    (``A_ONLY`` / ``B_ONLY``) carry ``None`` for the delta fields per
    REQ-CB-SCORE-005.
    """

    BOTH = "both"
    A_ONLY = "a_only"
    B_ONLY = "b_only"


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
    breakdowns, the per-sector reliability diagrams, and the
    fallback-polarity provenance counters surfaced by the run-summary
    header (design §3.4). Sectors and classes that produced zero
    scoreable resolutions are reported via ``zero_resolutions_sectors``
    / ``zero_resolutions_classes`` so the renderer can flag them in
    operator-facing output (REQ-CB-RENDER-002).

    The persisted ``backtest_runs.summary_json`` payload is produced by
    :meth:`to_json`; the same structure is available as a plain mapping
    via :meth:`as_mapping` for callers that want to feed it through
    :func:`persistence.operations.complete_run` (which re-serialises with
    the canonical encoder). Both surfaces sort keys for determinism so a
    self-compare across two runs with identical inputs produces
    byte-identical JSON.
    """

    overall_brier: float
    per_sector_brier: Mapping[str, float]
    per_class_brier: Mapping[str, float]
    reliability_diagrams: Mapping[str, ReliabilityDiagram]
    zero_resolutions_sectors: tuple[str, ...]
    zero_resolutions_classes: tuple[str, ...]
    fallback_polarity_count: int
    fallback_polarity_rate: float

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
        if self.fallback_polarity_count < 0:
            raise BacktestConfigError(
                "ScoreSummary.fallback_polarity_count must be >= 0, "
                f"got {self.fallback_polarity_count!r}"
            )
        if not (0.0 <= self.fallback_polarity_rate <= 1.0):
            raise BacktestConfigError(
                "ScoreSummary.fallback_polarity_rate must be in [0.0, 1.0], "
                f"got {self.fallback_polarity_rate!r}"
            )

    def as_mapping(self) -> dict[str, Any]:
        """Return a deterministic plain-dict representation of the summary.

        Sectors and classes are emitted as sorted dicts; reliability
        diagrams are decomposed into nested dict / list structure with
        ``bin_count`` and an ordered ``bins`` list whose entries carry
        the ``ReliabilityBin`` field names. ``zero_resolutions_*`` are
        emitted as sorted lists. The shape is stable across calls so
        :meth:`to_json` produces byte-identical output for identical
        inputs (REQ-CB-PERSIST-001 determinism gate).
        """
        return {
            "fallback_polarity_count": self.fallback_polarity_count,
            "fallback_polarity_rate": self.fallback_polarity_rate,
            "overall_brier": self.overall_brier,
            "per_class_brier": dict(sorted(self.per_class_brier.items())),
            "per_sector_brier": dict(sorted(self.per_sector_brier.items())),
            "reliability_diagrams": {
                sector: _reliability_diagram_to_mapping(self.reliability_diagrams[sector])
                for sector in sorted(self.reliability_diagrams)
            },
            "zero_resolutions_classes": sorted(self.zero_resolutions_classes),
            "zero_resolutions_sectors": sorted(self.zero_resolutions_sectors),
        }

    def to_json(self) -> str:
        """Return the canonical JSON encoding of the summary.

        Uses ``json.dumps(sort_keys=True)`` so identical
        :class:`ScoreSummary` inputs produce byte-identical output
        across runs and platforms (T-CB-023 determinism gate). The
        compact separator tuple matches
        :func:`persistence.operations._dumps_canonical` so the
        round-trip through DuckDB's ``JSON`` column preserves the
        on-disk byte sequence.
        """
        return json.dumps(
            self.as_mapping(),
            sort_keys=True,
            separators=(",", ":"),
        )


def _reliability_diagram_to_mapping(diagram: ReliabilityDiagram) -> dict[str, Any]:
    """Decompose a :class:`ReliabilityDiagram` into a deterministic mapping.

    Bins are emitted in their stored order (which mirrors the bin index
    from low to high probability) so the renderer can consume the JSON
    payload without re-sorting.
    """
    return {
        "bin_count": diagram.bin_count,
        "bins": [
            {
                "count": bin_.count,
                "empirical_rate": bin_.empirical_rate,
                "lower_p": bin_.lower_p,
                "mean_predicted_p": bin_.mean_predicted_p,
                "upper_p": bin_.upper_p,
            }
            for bin_ in diagram.bins
        ],
    }


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
class RunParameters:
    """Operator-supplied inputs for one backtest run (design §3.5).

    The replay loop (T-CB-018, :func:`engines.replay.run_backtest`)
    consumes a ``RunParameters`` instance to (a) compute the deterministic
    ``run_id`` (via :class:`razor_rooster.calibration_backtest.run_id.RunIdInputs`),
    (b) enforce the recent-window guard (REQ-CB-RUN-002), and (c) drive
    the ``iter_mapped_resolutions`` SQL prefilter that selects the
    in-scope ``polymarket_resolutions`` rows. Sequence fields are stored
    as immutable ``tuple[str, ...]`` because the dataclass is frozen and
    because run-id canonicalization sorts them anyway; passing a
    ``set``/``list``/``frozenset`` at the call site is fine — the caller
    converts to tuple before construction.

    Validation (``__post_init__``) rejects configurations the replay loop
    must never accept: lag below the floor, an empty or inverted replay
    window, naive datetimes, an empty ``class_ids`` tuple, and an empty
    ``venues`` tuple. ``sectors`` may be empty (the seed library exposes
    only one sector today; an empty filter means "all sectors").

    The optional ``bin_count`` and ``bin_count_per_sector`` overrides
    flow from ``--bin-count`` / ``--bin-count-per-sector`` CLI flags
    (T-CB-026). They are **excluded** from the canonical ``run_id``
    hash because reliability bins are a display concern (design §3.4):
    two runs with identical replay parameters but different bin counts
    must share a ``run_id``. Operators see the resolved bin counts on
    :class:`BacktestRun` (``bin_count_global`` / ``bin_count_per_sector``)
    for auditability. Per-sector overrides default to an empty mapping;
    the resolver (:func:`engines.scoring.resolve_bin_counts`) layers
    in the per-sector + global + module-default fallback chain.
    """

    since_ts: datetime
    until_ts: datetime
    lag_days: int
    class_ids: tuple[str, ...]
    sectors: tuple[str, ...]
    venues: tuple[str, ...]
    allow_recent: bool
    bin_count: int | None = None
    bin_count_per_sector: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.since_ts.tzinfo is None:
            raise BacktestConfigError(
                "RunParameters.since_ts must be timezone-aware, "
                f"got naive datetime {self.since_ts.isoformat()!r}"
            )
        if self.until_ts.tzinfo is None:
            raise BacktestConfigError(
                "RunParameters.until_ts must be timezone-aware, "
                f"got naive datetime {self.until_ts.isoformat()!r}"
            )
        if self.since_ts >= self.until_ts:
            raise BacktestConfigError(
                "RunParameters.since_ts must precede until_ts "
                f"(since_ts={self.since_ts.isoformat()}, "
                f"until_ts={self.until_ts.isoformat()})"
            )
        if self.lag_days < 1:
            raise BacktestConfigError(f"RunParameters.lag_days must be >= 1, got {self.lag_days!r}")
        if not self.class_ids:
            raise BacktestConfigError("RunParameters.class_ids must be non-empty")
        if not self.venues:
            raise BacktestConfigError("RunParameters.venues must be non-empty")
        if self.bin_count is not None and self.bin_count < 2:
            raise BacktestConfigError(
                f"RunParameters.bin_count must be >= 2 when set, got {self.bin_count!r}"
            )
        for sector, count in self.bin_count_per_sector.items():
            if count < 2:
                raise BacktestConfigError(
                    f"RunParameters.bin_count_per_sector[{sector!r}] must be >= 2, got {count!r}"
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


@dataclass(frozen=True, slots=True)
class CompareCell:
    """One cell of a run-vs-run comparison (T-CB-024; design §3.7).

    Each cell summarises a ``(sector, class_id)`` group across two
    backtest runs A and B. ``brier_a`` and ``brier_b`` carry the
    aggregated Brier score for each side (``AVG(brier_contribution)``
    over ``status='scored'`` rows, mirroring T-CB-023's per-sector and
    per-class aggregation exactly so a self-compare yields zero deltas).
    Cells observed in only one run carry ``None`` for the missing side
    plus all delta fields per REQ-CB-SCORE-005.

    ``crossed_miscalibration_threshold`` is computed in Python (not SQL)
    so the threshold can be overridden per-call without re-issuing the
    aggregate query: it is ``True`` when ``abs(delta_absolute) >=
    threshold`` and ``False`` otherwise — but only when ``present_in``
    is :data:`PresentIn.BOTH`. Asymmetric cells carry ``None``.

    ``trace_diff_summary`` is reserved for v2 trace-diff work (DEFER) and
    is always ``None`` in v1.
    """

    sector: str
    class_id: str
    brier_a: float | None
    brier_b: float | None
    delta_absolute: float | None
    delta_percent: float | None
    crossed_miscalibration_threshold: bool | None
    present_in: PresentIn
    trace_diff_summary: str | None = None

    def __post_init__(self) -> None:
        if not self.sector:
            raise BacktestConfigError("CompareCell.sector must be non-empty")
        if not self.class_id:
            raise BacktestConfigError(f"CompareCell {self.sector!r}: class_id must be non-empty")
        if self.brier_a is not None and not (0.0 <= self.brier_a <= 1.0):
            raise BacktestConfigError(
                f"CompareCell ({self.sector!r}, {self.class_id!r}): brier_a must be in "
                f"[0.0, 1.0] when set, got {self.brier_a!r}"
            )
        if self.brier_b is not None and not (0.0 <= self.brier_b <= 1.0):
            raise BacktestConfigError(
                f"CompareCell ({self.sector!r}, {self.class_id!r}): brier_b must be in "
                f"[0.0, 1.0] when set, got {self.brier_b!r}"
            )
        if self.present_in is PresentIn.BOTH:
            if self.brier_a is None or self.brier_b is None:
                raise BacktestConfigError(
                    f"CompareCell ({self.sector!r}, {self.class_id!r}): "
                    "present_in='both' requires both brier_a and brier_b to be set"
                )
            if self.delta_absolute is None:
                raise BacktestConfigError(
                    f"CompareCell ({self.sector!r}, {self.class_id!r}): "
                    "present_in='both' requires delta_absolute to be set"
                )
            if self.crossed_miscalibration_threshold is None:
                raise BacktestConfigError(
                    f"CompareCell ({self.sector!r}, {self.class_id!r}): "
                    "present_in='both' requires crossed_miscalibration_threshold to be set"
                )
        else:
            if self.delta_absolute is not None:
                raise BacktestConfigError(
                    f"CompareCell ({self.sector!r}, {self.class_id!r}): "
                    "asymmetric cells must carry delta_absolute=None"
                )
            if self.delta_percent is not None:
                raise BacktestConfigError(
                    f"CompareCell ({self.sector!r}, {self.class_id!r}): "
                    "asymmetric cells must carry delta_percent=None"
                )
            if self.crossed_miscalibration_threshold is not None:
                raise BacktestConfigError(
                    f"CompareCell ({self.sector!r}, {self.class_id!r}): "
                    "asymmetric cells must carry crossed_miscalibration_threshold=None"
                )
            if self.present_in is PresentIn.A_ONLY:
                if self.brier_a is None or self.brier_b is not None:
                    raise BacktestConfigError(
                        f"CompareCell ({self.sector!r}, {self.class_id!r}): "
                        "present_in='a_only' requires brier_a set and brier_b=None"
                    )
            else:  # PresentIn.B_ONLY
                if self.brier_b is None or self.brier_a is not None:
                    raise BacktestConfigError(
                        f"CompareCell ({self.sector!r}, {self.class_id!r}): "
                        "present_in='b_only' requires brier_b set and brier_a=None"
                    )


__all__ = [
    "BacktestPrediction",
    "BacktestRun",
    "BacktestStatus",
    "BacktestTrace",
    "CompareCell",
    "CompressionAlgorithm",
    "PolaritySource",
    "PolarityValue",
    "PredictionStatus",
    "PresentIn",
    "ReliabilityBin",
    "ReliabilityDiagram",
    "RunParameters",
    "ScoreSummary",
    "SkipReason",
]
