"""Calibration_backtest typed exception hierarchy (T-CB-003).

All recoverable and fatal failure modes raised by the
calibration_backtest subsystem inherit from
``CalibrationBacktestError``, allowing callers (replay loop, CLI,
persistence layer) to discriminate subsystem-internal failures from
unexpected exceptions and to map specific failure types onto closed
``skip_reason`` enumeration values (design Section 3.13).

Each exception class includes a structured ``__repr__`` so log
aggregators can surface error context without bespoke formatters. The
root class stores the message on ``self.message`` for convenient
structured-log capture.

Failure-mode summary (cross-references to design doc / requirements):

* ``RecentWindowError`` — backtest window crosses the recent-window
  guard and ``--allow-recent`` was not supplied (REQ-CB-RUN-002).
* ``DiskBudgetError`` — persistence would exceed the configured
  ``disk_cap_mb`` cap (REQ-CB-PERSIST-003).
* ``InvalidLagError`` — observed ``resolution_ts - prediction_ts``
  fails the lag-floor invariant (REQ-CB-FREEZE-002); maps to
  ``skip_reason='insufficient_lag'``.
* ``NoPolarityError`` — neither ``comparison_resolutions`` nor the
  current-mapping fallback resolved a polarity for a prediction (D5);
  maps to ``skip_reason='no_polarity_resolution'``.
* ``InsufficientPrecursorData`` — the freezer could not assemble a
  precursor row at ``prediction_ts``; maps to
  ``skip_reason='insufficient_data'``.
* ``MappingNotFoundError`` — no ``class_market_mappings`` row exists
  for the (class_id, condition_id) pair; maps to
  ``skip_reason='mapping_missing'``.
* ``InvalidResolutionError`` — ``polymarket_resolutions.invalidated``
  is true for the row; maps to ``skip_reason='invalid_resolution'``.
* ``BacktestSchemaError`` — persistence schema invariant violated
  (e.g., enum value outside closed set, FK orphan).
* ``BacktestPersistenceError`` — DB layer raised during a backtest
  write (wraps the underlying DuckDB error message).
* ``BacktestConfigError`` — invalid configuration, parameter, or CLI
  flag combination detected before the run starts.
"""

from __future__ import annotations


class CalibrationBacktestError(Exception):
    """Base class for all calibration_backtest exceptions."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.message!r})"


class BacktestConfigError(CalibrationBacktestError):
    """Raised when configuration or CLI parameters are invalid."""


class BacktestPersistenceError(CalibrationBacktestError):
    """Raised when the persistence layer fails during a backtest write."""


class BacktestSchemaError(CalibrationBacktestError):
    """Raised when a persistence schema invariant is violated."""


class DiskBudgetError(CalibrationBacktestError):
    """Raised when persistence would exceed the configured disk cap."""


class InsufficientPrecursorData(CalibrationBacktestError):
    """Raised when the freezer cannot assemble precursor data."""


class InvalidLagError(CalibrationBacktestError):
    """Raised when the resolution/prediction pair violates the lag floor."""


class InvalidResolutionError(CalibrationBacktestError):
    """Raised when a Polymarket resolution row is marked invalidated."""


class MappingNotFoundError(CalibrationBacktestError):
    """Raised when no class/market mapping exists for the prediction."""


class NoPolarityError(CalibrationBacktestError):
    """Raised when polarity resolution fails for a prediction.

    The error stores optional structured context (``prediction_ts``,
    ``condition_id``, ``class_id``, ``venue``) so callers can surface
    actionable diagnostics in skip-row metadata and structured logs
    without re-deriving them. Constructor accepts either a positional
    ``message`` or keyword-only context fields; when no message is
    supplied a deterministic message is composed from the kwargs so the
    base ``CalibrationBacktestError.__init__`` invariant is preserved.
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        prediction_ts: object = None,
        condition_id: str | None = None,
        class_id: str | None = None,
        venue: str | None = None,
    ) -> None:
        if message is None:
            message = (
                "no polarity resolution for "
                f"class_id={class_id!r} condition_id={condition_id!r} "
                f"venue={venue!r} prediction_ts={prediction_ts!r}"
            )
        super().__init__(message)
        self.prediction_ts = prediction_ts
        self.condition_id = condition_id
        self.class_id = class_id
        self.venue = venue


class RecentWindowError(CalibrationBacktestError):
    """Raised when a backtest window crosses the recent-window guard.

    Carries optional structured context (``until_ts``, ``cutoff``,
    ``recommended_until_ts``) so the CLI and replay loop can surface a
    deterministic remediation message — "the operator could re-run with
    ``--until <recommended>`` if they choose to". The constructor accepts
    either a positional ``message`` or keyword-only context fields; when no
    message is supplied a deterministic message is composed from the
    kwargs so the base ``CalibrationBacktestError.__init__`` invariant is
    preserved (REQ-CB-RUN-002, design §3.5).
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        until_ts: object = None,
        cutoff: object = None,
        recommended_until_ts: object = None,
    ) -> None:
        if message is None:
            message = (
                "until_ts crosses the recent-window guard: "
                f"until_ts={until_ts!r} cutoff={cutoff!r} "
                f"recommended_until_ts={recommended_until_ts!r}"
            )
        super().__init__(message)
        self.until_ts = until_ts
        self.cutoff = cutoff
        self.recommended_until_ts = recommended_until_ts


__all__ = [
    "BacktestConfigError",
    "BacktestPersistenceError",
    "BacktestSchemaError",
    "CalibrationBacktestError",
    "DiskBudgetError",
    "InsufficientPrecursorData",
    "InvalidLagError",
    "InvalidResolutionError",
    "MappingNotFoundError",
    "NoPolarityError",
    "RecentWindowError",
]
