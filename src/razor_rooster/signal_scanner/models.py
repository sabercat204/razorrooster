"""Typed dataclasses for signal_scanner outputs (T-SCAN-010 / T-SCAN-011).

Two layers of types live here:

- :class:`ScanSummary` mirrors one ``scan_summaries`` row.
- :class:`ScanRecord` mirrors one ``scan_records`` row.
- :class:`Trace` mirrors one ``scan_traces`` row (the JSON payload is
  represented as a typed dict for readability).

These dataclasses are frozen (immutable) so callers can pass them
across the persistence/computation boundary without worrying about
accidental mutation. The persistence helpers in
:mod:`signal_scanner.persistence.operations` consume / produce them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ScanSummary:
    """One scan execution's aggregate stats."""

    scan_id: str
    scan_started_at: datetime
    scan_completed_at: datetime | None
    pattern_library_version: int
    classes_total: int
    classes_succeeded: int
    classes_failed: int
    classes_skipped: int
    candidates_count: int
    library_stale_warning: bool = False
    config_snapshot: Mapping[str, Any] | None = None
    error_summary: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ScanRecord:
    """Per-class scan output (one row per (scan_id, class_id))."""

    scan_id: str
    class_id: str
    class_definition_version: int
    pattern_library_version: int
    data_as_of: datetime
    scan_started_at: datetime
    scan_completed_at: datetime | None
    base_rate: float
    base_rate_ci_lower: float
    base_rate_ci_upper: float
    posterior: float
    posterior_ci_lower: float
    posterior_ci_upper: float
    log_odds_shift: float
    is_candidate: bool = False
    candidate_direction: str | None = None
    signature_confidence: float | None = None
    low_signature_confidence: bool = False
    source_stale_warning: bool = False
    library_stale_warning: bool = False
    definition_drift_warning: bool = False
    no_update_applied: bool = False
    no_update_reason: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Trace:
    """Reasoning trace persisted to ``scan_traces`` (one row per record).

    The trace JSON shape is documented in design §3.6. We keep the
    payload typed as a generic mapping here; the trace builder
    (T-SCAN-021) emits a stable schema.
    """

    scan_id: str
    class_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
