"""Analogue feature space and match results (T-PL-010; design §3.4, §3.5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class AnalogueFeatureSpace:
    """Persisted analogue feature space for one class.

    Stored row-wise in ``pl_analogue_features``; this dataclass is the
    in-memory shape returned by ``populate_feature_space()``.
    """

    class_id: str
    library_version: int
    definition_version: int
    feature_ids: tuple[str, ...]
    point_count: int
    event_count: int
    normalization_params: dict[str, dict[str, float]]  # feature_id → {mean, std} (or similar)

    def __post_init__(self) -> None:
        if self.point_count < self.event_count:
            raise ValueError(
                f"AnalogueFeatureSpace {self.class_id!r}: point_count ({self.point_count}) "
                f"< event_count ({self.event_count})"
            )
        if not self.feature_ids:
            raise ValueError(
                f"AnalogueFeatureSpace {self.class_id!r}: feature_ids must be non-empty"
            )


@dataclass(frozen=True, slots=True)
class AnalogueMatch:
    """One historical analogue returned from ``find_analogues``."""

    point_id: str  # 'event:<occurrence_id>' or 'baseline:<hash>'
    timestamp: datetime
    is_event: bool
    distance: float
    feature_vector_normalized: dict[str, float]

    def __post_init__(self) -> None:
        if self.distance < 0:
            raise ValueError(f"AnalogueMatch {self.point_id!r}: distance must be >= 0")


@dataclass(frozen=True, slots=True)
class AnalogueResults:
    """Aggregate result of ``find_analogues``."""

    class_id: str
    library_version: int
    definition_version: int
    query_timestamp: datetime
    matches: tuple[AnalogueMatch, ...] = field(default_factory=tuple)
