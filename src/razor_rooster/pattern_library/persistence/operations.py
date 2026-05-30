"""Pattern-library persistence helpers (T-PL-012; design §3.4).

Typed write/read operations against the eight ``pl_*`` tables. Callers
pass in a DuckDB connection; the helpers do not acquire connections
from a store. Each helper carries the relevant version columns so
downstream consumers can detect mismatches.

The helpers are intentionally thin wrappers around SQL — the
computation engines (T-PL-041 through T-PL-046) build typed result
objects and pass them through these helpers for storage.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import duckdb

from razor_rooster.pattern_library.models.analogue import AnalogueFeatureSpace
from razor_rooster.pattern_library.models.base_rate import BaseRateResult
from razor_rooster.pattern_library.models.calibration import CalibrationOutput
from razor_rooster.pattern_library.models.event_class import EventClass
from razor_rooster.pattern_library.models.outcomes import OutcomeRecord
from razor_rooster.pattern_library.models.signature import SignatureResult

logger = logging.getLogger(__name__)


# -- pl_event_classes -------------------------------------------------------


def upsert_event_class(
    conn: duckdb.DuckDBPyConnection,
    cls: EventClass,
    *,
    when: datetime | None = None,
) -> None:
    """Register or refresh an event class row in ``pl_event_classes``.

    Re-running for an unchanged class is a no-op on the row; the class's
    ``last_evaluated_at`` and ``library_version_at_last_eval`` columns
    are written separately by :func:`record_class_evaluation`.
    """
    ts = when or datetime.now(tz=UTC)
    secondary = json.dumps([s.value for s in cls.secondary_sectors])
    existing = conn.execute(
        "SELECT 1 FROM pl_event_classes WHERE class_id = ?", [cls.class_id]
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO pl_event_classes ("
            "class_id, title, description, domain_sector, secondary_sectors, "
            "definition_version, outcome_type, registered_at, "
            "last_evaluated_at, library_version_at_last_eval, removed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
            [
                cls.class_id,
                cls.title,
                cls.description,
                cls.domain_sector.value,
                secondary,
                cls.definition_version,
                cls.outcome_type,
                ts,
            ],
        )
    else:
        conn.execute(
            "UPDATE pl_event_classes SET title = ?, description = ?, "
            "domain_sector = ?, secondary_sectors = ?, "
            "definition_version = ?, outcome_type = ?, removed_at = NULL "
            "WHERE class_id = ?",
            [
                cls.title,
                cls.description,
                cls.domain_sector.value,
                secondary,
                cls.definition_version,
                cls.outcome_type,
                cls.class_id,
            ],
        )


def mark_event_class_removed(
    conn: duckdb.DuckDBPyConnection,
    class_id: str,
    *,
    when: datetime | None = None,
) -> None:
    """Mark a class as removed (REQ-PL-VER-002 — keep historic rows)."""
    ts = when or datetime.now(tz=UTC)
    conn.execute(
        "UPDATE pl_event_classes SET removed_at = ? WHERE class_id = ? AND removed_at IS NULL",
        [ts, class_id],
    )


def record_class_evaluation(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str,
    library_version: int,
    when: datetime | None = None,
) -> None:
    """Stamp a class with its most recent successful refresh."""
    ts = when or datetime.now(tz=UTC)
    conn.execute(
        "UPDATE pl_event_classes SET last_evaluated_at = ?, "
        "library_version_at_last_eval = ? WHERE class_id = ?",
        [ts, library_version, class_id],
    )


# -- pl_outcomes ------------------------------------------------------------


def upsert_outcomes(
    conn: duckdb.DuckDBPyConnection,
    outcomes: Iterable[OutcomeRecord],
    *,
    library_version: int,
    definition_version: int,
    when: datetime | None = None,
) -> int:
    """Upsert a batch of OutcomeRecord rows.

    Returns the number of rows written. Idempotent on
    ``(class_id, occurrence_id)``: re-inserting an identical record
    overwrites in place.
    """
    ts = when or datetime.now(tz=UTC)
    rows = list(outcomes)
    if not rows:
        return 0

    # First delete any existing rows for the (class_id, occurrence_id)
    # tuples we're about to insert. DuckDB doesn't have native ON
    # CONFLICT for table-defined PKs at v1.5; explicit delete-then-
    # insert is the simplest correct path here.
    keys = [(r.class_id, r.occurrence_id) for r in rows]
    if keys:
        conn.execute(
            "DELETE FROM pl_outcomes WHERE (class_id, occurrence_id) IN ("
            + ", ".join(["(?, ?)"] * len(keys))
            + ")",
            [v for pair in keys for v in pair],
        )

    for r in rows:
        conn.execute(
            "INSERT INTO pl_outcomes ("
            "class_id, occurrence_id, occurrence_ts, end_ts, description, "
            "source_records, library_version, definition_version, computed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                r.class_id,
                r.occurrence_id,
                r.occurrence_ts,
                r.end_ts,
                r.description,
                json.dumps(list(r.source_records)),
                library_version,
                definition_version,
                ts,
            ],
        )
    return len(rows)


def query_outcomes(
    conn: duckdb.DuckDBPyConnection,
    class_id: str,
) -> tuple[OutcomeRecord, ...]:
    """Return all OutcomeRecord rows for a class, sorted by occurrence_ts."""
    rows = conn.execute(
        "SELECT class_id, occurrence_id, occurrence_ts, end_ts, description, "
        "source_records FROM pl_outcomes WHERE class_id = ? ORDER BY occurrence_ts",
        [class_id],
    ).fetchall()
    out: list[OutcomeRecord] = []
    for r in rows:
        source_records: tuple[dict[str, str], ...] = ()
        if r[5]:
            try:
                decoded = json.loads(r[5])
                if isinstance(decoded, list):
                    source_records = tuple(
                        {str(k): str(v) for k, v in item.items()}
                        for item in decoded
                        if isinstance(item, dict)
                    )
            except json.JSONDecodeError:
                source_records = ()
        out.append(
            OutcomeRecord(
                class_id=str(r[0]),
                occurrence_id=str(r[1]),
                occurrence_ts=r[2],
                end_ts=r[3],
                description=str(r[4]) if r[4] is not None else None,
                source_records=source_records,
            )
        )
    return tuple(out)


# -- pl_base_rates ---------------------------------------------------------


def upsert_base_rate(
    conn: duckdb.DuckDBPyConnection,
    result: BaseRateResult,
) -> None:
    """Insert or replace a BaseRateResult row."""
    conn.execute(
        "DELETE FROM pl_base_rates WHERE class_id = ? AND window_start = ? "
        "AND window_end = ? AND library_version = ?",
        [result.class_id, result.window_start, result.window_end, result.library_version],
    )
    conn.execute(
        "INSERT INTO pl_base_rates ("
        "class_id, window_start, window_end, occurrences, rate_per_year, "
        "credible_interval_lower, credible_interval_upper, prior_alpha, prior_beta, "
        "library_version, definition_version, data_as_of, computed_at, "
        "low_sample_warning, source_stale_warning, stale"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            result.class_id,
            result.window_start,
            result.window_end,
            result.occurrences,
            result.rate_per_year,
            result.credible_interval_lower,
            result.credible_interval_upper,
            result.prior_alpha,
            result.prior_beta,
            result.library_version,
            result.definition_version,
            result.data_as_of,
            result.computed_at,
            result.low_sample_warning,
            result.source_stale_warning,
            result.stale,
        ],
    )


def query_latest_base_rate(
    conn: duckdb.DuckDBPyConnection,
    class_id: str,
    *,
    library_version: int | None = None,
) -> BaseRateResult | None:
    """Return the most recent base rate for a class (latest computed_at)."""
    if library_version is not None:
        row = conn.execute(
            "SELECT class_id, window_start, window_end, occurrences, rate_per_year, "
            "credible_interval_lower, credible_interval_upper, prior_alpha, prior_beta, "
            "library_version, definition_version, data_as_of, computed_at, "
            "low_sample_warning, source_stale_warning, stale "
            "FROM pl_base_rates WHERE class_id = ? AND library_version = ? "
            "ORDER BY computed_at DESC LIMIT 1",
            [class_id, library_version],
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT class_id, window_start, window_end, occurrences, rate_per_year, "
            "credible_interval_lower, credible_interval_upper, prior_alpha, prior_beta, "
            "library_version, definition_version, data_as_of, computed_at, "
            "low_sample_warning, source_stale_warning, stale "
            "FROM pl_base_rates WHERE class_id = ? ORDER BY computed_at DESC LIMIT 1",
            [class_id],
        ).fetchone()
    if row is None:
        return None
    return BaseRateResult(
        class_id=str(row[0]),
        window_start=row[1],
        window_end=row[2],
        occurrences=int(row[3]),
        rate_per_year=float(row[4]),
        credible_interval_lower=float(row[5]),
        credible_interval_upper=float(row[6]),
        prior_alpha=float(row[7]),
        prior_beta=float(row[8]),
        library_version=int(row[9]),
        definition_version=int(row[10]),
        data_as_of=row[11],
        computed_at=row[12],
        low_sample_warning=bool(row[13]),
        source_stale_warning=bool(row[14]),
        stale=bool(row[15]),
    )


# -- pl_precursor_signatures -----------------------------------------------


def upsert_signature(
    conn: duckdb.DuckDBPyConnection,
    result: SignatureResult,
) -> None:
    conn.execute(
        "DELETE FROM pl_precursor_signatures WHERE class_id = ? AND variable_id = ? "
        "AND library_version = ?",
        [result.class_id, result.variable_id, result.library_version],
    )
    conn.execute(
        "INSERT INTO pl_precursor_signatures ("
        "class_id, variable_id, library_version, definition_version, "
        "threshold_method, threshold_value, direction, lead_time_window_days, "
        "pre_event_mean, pre_event_p25, pre_event_p50, pre_event_p75, "
        "baseline_mean, baseline_p25, baseline_p50, baseline_p75, "
        "hit_rate, false_positive_rate, sample_size_events, sample_size_baseline, "
        "confidence_score, low_confidence_warning, computed_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            result.class_id,
            result.variable_id,
            result.library_version,
            result.definition_version,
            result.threshold_method,
            result.threshold_value,
            result.direction,
            result.lead_time_window_days,
            result.pre_event_mean,
            result.pre_event_p25,
            result.pre_event_p50,
            result.pre_event_p75,
            result.baseline_mean,
            result.baseline_p25,
            result.baseline_p50,
            result.baseline_p75,
            result.hit_rate,
            result.false_positive_rate,
            result.sample_size_events,
            result.sample_size_baseline,
            result.confidence_score,
            result.low_confidence_warning,
            result.computed_at,
        ],
    )


def query_signatures(
    conn: duckdb.DuckDBPyConnection,
    class_id: str,
    *,
    library_version: int | None = None,
) -> tuple[SignatureResult, ...]:
    """Return all signatures for a class. Filters by library_version when given."""
    if library_version is not None:
        rows = conn.execute(
            _SIGNATURE_SELECT + "WHERE class_id = ? AND library_version = ? ORDER BY variable_id",
            [class_id, library_version],
        ).fetchall()
    else:
        rows = conn.execute(
            _SIGNATURE_SELECT + "WHERE class_id = ? ORDER BY variable_id",
            [class_id],
        ).fetchall()
    return tuple(_signature_row_to_dataclass(r) for r in rows)


_SIGNATURE_SELECT = (
    "SELECT class_id, variable_id, library_version, definition_version, "
    "threshold_method, threshold_value, direction, lead_time_window_days, "
    "pre_event_mean, pre_event_p25, pre_event_p50, pre_event_p75, "
    "baseline_mean, baseline_p25, baseline_p50, baseline_p75, "
    "hit_rate, false_positive_rate, sample_size_events, sample_size_baseline, "
    "confidence_score, low_confidence_warning, computed_at "
    "FROM pl_precursor_signatures "
)


def _signature_row_to_dataclass(row: tuple[Any, ...]) -> SignatureResult:
    return SignatureResult(
        class_id=str(row[0]),
        variable_id=str(row[1]),
        library_version=int(row[2]),
        definition_version=int(row[3]),
        threshold_method=str(row[4]),
        threshold_value=float(row[5]) if row[5] is not None else None,
        direction=str(row[6]),  # type: ignore[arg-type]
        lead_time_window_days=int(row[7]),
        pre_event_mean=float(row[8]) if row[8] is not None else None,
        pre_event_p25=float(row[9]) if row[9] is not None else None,
        pre_event_p50=float(row[10]) if row[10] is not None else None,
        pre_event_p75=float(row[11]) if row[11] is not None else None,
        baseline_mean=float(row[12]) if row[12] is not None else None,
        baseline_p25=float(row[13]) if row[13] is not None else None,
        baseline_p50=float(row[14]) if row[14] is not None else None,
        baseline_p75=float(row[15]) if row[15] is not None else None,
        hit_rate=float(row[16]) if row[16] is not None else None,
        false_positive_rate=float(row[17]) if row[17] is not None else None,
        sample_size_events=int(row[18]),
        sample_size_baseline=int(row[19]),
        confidence_score=float(row[20]),
        low_confidence_warning=bool(row[21]),
        computed_at=row[22],
    )


# -- pl_analogue_features --------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AnalogueRow:
    """One row to write to pl_analogue_features."""

    point_id: str
    timestamp: datetime
    is_event: bool
    feature_vector_raw: dict[str, float]
    feature_vector_normalized: dict[str, float]


def upsert_analogue_features(
    conn: duckdb.DuckDBPyConnection,
    *,
    space: AnalogueFeatureSpace,
    rows: Iterable[_AnalogueRow],
    when: datetime | None = None,
) -> int:
    """Persist a batch of analogue feature rows for one class."""
    ts = when or datetime.now(tz=UTC)
    rows_list = list(rows)
    if not rows_list:
        return 0

    # Replace prior rows for this (class_id, library_version) so the
    # space is consistent. Older library versions are kept in case
    # downstream consumers still reference them.
    conn.execute(
        "DELETE FROM pl_analogue_features WHERE class_id = ? AND library_version = ?",
        [space.class_id, space.library_version],
    )
    for r in rows_list:
        conn.execute(
            "INSERT INTO pl_analogue_features ("
            "class_id, point_id, timestamp, is_event, feature_vector_raw, "
            "feature_vector_normalized, library_version, definition_version, computed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                space.class_id,
                r.point_id,
                r.timestamp,
                r.is_event,
                json.dumps(r.feature_vector_raw),
                json.dumps(r.feature_vector_normalized),
                space.library_version,
                space.definition_version,
                ts,
            ],
        )
    return len(rows_list)


def query_analogue_population(
    conn: duckdb.DuckDBPyConnection,
    class_id: str,
    *,
    library_version: int,
) -> tuple[_AnalogueRow, ...]:
    rows = conn.execute(
        "SELECT point_id, timestamp, is_event, feature_vector_raw, feature_vector_normalized "
        "FROM pl_analogue_features WHERE class_id = ? AND library_version = ?",
        [class_id, library_version],
    ).fetchall()
    out: list[_AnalogueRow] = []
    for r in rows:
        try:
            raw = json.loads(r[3])
            normalized = json.loads(r[4])
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict) or not isinstance(normalized, dict):
            continue
        out.append(
            _AnalogueRow(
                point_id=str(r[0]),
                timestamp=r[1],
                is_event=bool(r[2]),
                feature_vector_raw={str(k): float(v) for k, v in raw.items()},
                feature_vector_normalized={str(k): float(v) for k, v in normalized.items()},
            )
        )
    return tuple(out)


# -- pl_calibration --------------------------------------------------------


def upsert_calibration(
    conn: duckdb.DuckDBPyConnection,
    output: CalibrationOutput,
) -> None:
    bins_json = json.dumps(
        [
            {
                "bin_low": b.bin_low,
                "bin_high": b.bin_high,
                "predicted_mean": b.predicted_mean,
                "observed_freq": b.observed_freq,
                "count": b.count,
            }
            for b in output.reliability_bins
        ]
    )
    conn.execute(
        "DELETE FROM pl_calibration WHERE class_id = ? AND library_version = ? AND method = ?",
        [output.class_id, output.library_version, output.method],
    )
    conn.execute(
        "INSERT INTO pl_calibration ("
        "class_id, library_version, definition_version, method, brier_score, "
        "reliability_bins, prediction_trace_path, computed_at, notes"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            output.class_id,
            output.library_version,
            output.definition_version,
            output.method,
            output.brier_score,
            bins_json,
            output.prediction_trace_path,
            output.computed_at,
            output.notes,
        ],
    )


# -- pl_library_versions + pl_refresh_log ----------------------------------


def record_library_version_bump(
    conn: duckdb.DuckDBPyConnection,
    *,
    library_version: int,
    bump_reason: str,
    affected_class_ids: tuple[str, ...] = (),
    notes: str | None = None,
    when: datetime | None = None,
) -> None:
    """Record a library version bump in ``pl_library_versions`` (REQ-PL-VER-002)."""
    ts = when or datetime.now(tz=UTC)
    existing = conn.execute(
        "SELECT 1 FROM pl_library_versions WHERE library_version = ?",
        [library_version],
    ).fetchone()
    if existing is not None:
        return  # already recorded
    conn.execute(
        "INSERT INTO pl_library_versions ("
        "library_version, bumped_at, bump_reason, affected_class_ids, notes"
        ") VALUES (?, ?, ?, ?, ?)",
        [
            library_version,
            ts,
            bump_reason,
            json.dumps(list(affected_class_ids)) if affected_class_ids else None,
            notes,
        ],
    )


def record_refresh(
    conn: duckdb.DuckDBPyConnection,
    *,
    refresh_id: str | None = None,
    started_at: datetime,
    ended_at: datetime | None,
    library_version: int,
    classes_processed: list[dict[str, Any]],
    error_summary: dict[str, Any] | None = None,
) -> str:
    """Append a row to ``pl_refresh_log``. Returns the refresh_id used."""
    rid = refresh_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO pl_refresh_log ("
        "refresh_id, started_at, ended_at, library_version, "
        "classes_processed, error_summary"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        [
            rid,
            started_at,
            ended_at,
            library_version,
            json.dumps(classes_processed, default=str),
            json.dumps(error_summary, default=str) if error_summary else None,
        ],
    )
    return rid
