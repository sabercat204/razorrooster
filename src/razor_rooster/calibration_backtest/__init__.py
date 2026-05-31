"""``calibration_backtest`` — Replay-based calibration backtesting (T-CB-001).

Replays historical Polymarket resolutions against the frozen state of
data sources at each prediction timestamp, scores the model under the
same library_version and class definition_versions that produced live
predictions, and emits per-sector / per-class Brier and reliability
diagnostics.

Outputs are decision-support analysis only: the subsystem does not place
orders, recommend trades, or size positions. Paper-analysis remains the
v1 contract regardless of calibration outcome.

Public API placeholders (``run_backtest``, ``compare``, ``list_runs``,
``show_run``) are wired in subsequent tasks (T-CB-002 onward); this
module currently exposes only the version constant and the typed
exception hierarchy (T-CB-003).
"""

from __future__ import annotations

from razor_rooster.calibration_backtest.errors import (
    BacktestConfigError,
    BacktestPersistenceError,
    BacktestSchemaError,
    CalibrationBacktestError,
    DiskBudgetError,
    InsufficientPrecursorData,
    InvalidLagError,
    InvalidResolutionError,
    MappingNotFoundError,
    NoPolarityError,
    RecentWindowError,
)
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
    BacktestTrace,
    CompressionAlgorithm,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
    ReliabilityBin,
    ReliabilityDiagram,
    RunParameters,
    ScoreSummary,
    SkipReason,
)
from razor_rooster.calibration_backtest.run_id import (
    RunIdInputs,
    canonicalize,
    compute_run_id,
)
from razor_rooster.calibration_backtest.version import CALIBRATION_BACKTEST_VERSION

__version__ = CALIBRATION_BACKTEST_VERSION

__all__ = [
    "CALIBRATION_BACKTEST_VERSION",
    "BacktestConfigError",
    "BacktestPersistenceError",
    "BacktestPrediction",
    "BacktestRun",
    "BacktestSchemaError",
    "BacktestStatus",
    "BacktestTrace",
    "CalibrationBacktestError",
    "CompressionAlgorithm",
    "DiskBudgetError",
    "InsufficientPrecursorData",
    "InvalidLagError",
    "InvalidResolutionError",
    "MappingNotFoundError",
    "NoPolarityError",
    "PolaritySource",
    "PolarityValue",
    "PredictionStatus",
    "RecentWindowError",
    "ReliabilityBin",
    "ReliabilityDiagram",
    "RunIdInputs",
    "RunParameters",
    "ScoreSummary",
    "SkipReason",
    "canonicalize",
    "compute_run_id",
]
