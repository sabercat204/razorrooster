"""Precursor signature result (T-PL-010; design §3.4, §3.5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class SignatureResult:
    """Output of the signature engine for one (class, variable) pair.

    Carries the discovered threshold + the distributional summary that
    drove the choice + a confidence score combining sample size,
    distributional separation, and threshold-bootstrap uncertainty.
    """

    class_id: str
    variable_id: str
    library_version: int
    definition_version: int
    threshold_method: str
    threshold_value: float | None
    direction: Literal["high_signals_event", "low_signals_event"]
    lead_time_window_days: int
    pre_event_mean: float | None
    pre_event_p25: float | None
    pre_event_p50: float | None
    pre_event_p75: float | None
    baseline_mean: float | None
    baseline_p25: float | None
    baseline_p50: float | None
    baseline_p75: float | None
    hit_rate: float | None
    false_positive_rate: float | None
    sample_size_events: int
    sample_size_baseline: int
    confidence_score: float
    computed_at: datetime
    low_confidence_warning: bool = False

    def __post_init__(self) -> None:
        if self.sample_size_events < 0 or self.sample_size_baseline < 0:
            raise ValueError(f"SignatureResult {self.variable_id!r}: sample sizes must be >= 0")
        if not 0.0 <= self.confidence_score <= 1.0:
            raise ValueError(
                f"SignatureResult {self.variable_id!r}: confidence_score must be in [0, 1]"
            )
        for label, value in (
            ("hit_rate", self.hit_rate),
            ("false_positive_rate", self.false_positive_rate),
        ):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"SignatureResult {self.variable_id!r}: {label} must be in [0, 1]")
        if self.library_version < 1 or self.definition_version < 1:
            raise ValueError(f"SignatureResult {self.variable_id!r}: version columns must be >= 1")
