"""Base rate computation result (T-PL-010; design §3.4, §3.5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class BaseRateResult:
    """Output of the base-rate engine for one (class, window) pair.

    The credible interval is computed against a Beta(alpha, beta) posterior
    over per-year occurrence rate; alpha/beta come from the class's
    Jeffreys prior or its override (OQ-PL-001).
    """

    class_id: str
    window_start: datetime
    window_end: datetime
    occurrences: int
    rate_per_year: float
    credible_interval_lower: float
    credible_interval_upper: float
    prior_alpha: float
    prior_beta: float
    library_version: int
    definition_version: int
    data_as_of: datetime
    computed_at: datetime
    low_sample_warning: bool = False
    source_stale_warning: bool = False
    stale: bool = False

    def __post_init__(self) -> None:
        if self.window_start >= self.window_end:
            raise ValueError(
                f"BaseRateResult {self.class_id!r}: window_start must precede window_end"
            )
        if self.occurrences < 0:
            raise ValueError(f"BaseRateResult {self.class_id!r}: occurrences must be >= 0")
        if self.credible_interval_lower > self.credible_interval_upper:
            raise ValueError(f"BaseRateResult {self.class_id!r}: CI lower must be <= upper")
        if self.rate_per_year < 0:
            raise ValueError(f"BaseRateResult {self.class_id!r}: rate_per_year must be >= 0")
        if self.library_version < 1 or self.definition_version < 1:
            raise ValueError(f"BaseRateResult {self.class_id!r}: version columns must be >= 1")
