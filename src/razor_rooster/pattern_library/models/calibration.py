"""Calibration output (T-PL-010; design §3.4, §3.5; OQ-PL-005)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    """One bin in the reliability diagram."""

    bin_low: float
    bin_high: float
    predicted_mean: float
    observed_freq: float
    count: int

    def __post_init__(self) -> None:
        if not 0.0 <= self.bin_low <= 1.0 or not 0.0 <= self.bin_high <= 1.0:
            raise ValueError("ReliabilityBin: bin edges must be in [0, 1]")
        if self.bin_low > self.bin_high:
            raise ValueError("ReliabilityBin: bin_low must be <= bin_high")
        if self.count < 0:
            raise ValueError("ReliabilityBin: count must be >= 0")


@dataclass(frozen=True, slots=True)
class CalibrationOutput:
    """Calibration result for one event class.

    Per OQ-PL-005, three artifacts: Brier score, reliability bins, and
    a per-event prediction trace stored at
    ``data/library/calibration/<class_id>.json`` (path captured here).
    Classes with <10 occurrences get ``method='insufficient_data'`` and
    ``brier_score=None``.
    """

    class_id: str
    library_version: int
    definition_version: int
    method: str
    brier_score: float | None
    reliability_bins: tuple[ReliabilityBin, ...]
    prediction_trace_path: str
    computed_at: datetime
    notes: str | None = field(default=None)

    def __post_init__(self) -> None:
        if not self.method:
            raise ValueError("CalibrationOutput: method must be non-empty")
        if self.brier_score is not None and not 0.0 <= self.brier_score <= 1.0:
            raise ValueError("CalibrationOutput: brier_score must be in [0, 1] when set")
        if self.library_version < 1 or self.definition_version < 1:
            raise ValueError("CalibrationOutput: version columns must be >= 1")
