"""Persisted occurrence record (T-PL-010; design §3.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One historical occurrence of an event class.

    ``source_records`` carries provenance back to the upstream
    ``data_ingest`` rows that drove the predicate match — a list of
    ``{table, source_record_id}`` dicts.
    """

    class_id: str
    occurrence_id: str  # deterministic hash of class + occurrence_ts
    occurrence_ts: datetime
    end_ts: datetime | None = None
    description: str | None = None
    source_records: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.class_id:
            raise ValueError("OutcomeRecord.class_id must be non-empty")
        if not self.occurrence_id:
            raise ValueError("OutcomeRecord.occurrence_id must be non-empty")
        if self.end_ts is not None and self.end_ts < self.occurrence_ts:
            raise ValueError(
                f"OutcomeRecord {self.occurrence_id!r}: end_ts ({self.end_ts}) "
                f"must be >= occurrence_ts ({self.occurrence_ts})"
            )
