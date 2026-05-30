"""Report-generator persistence helpers (T-RG-011; design §3.3)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import duckdb

from razor_rooster.report_generator.models import ReportRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ThresholdMeasurementRecord:
    """One row of ``report_threshold_measurements`` (T-RG-COMPAT-MEAS-001)."""

    report_id: str
    measurement_kind: str
    measured_at: datetime
    n_observations: int
    n_above_threshold: int
    configured_threshold: float
    distribution: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ThresholdTuningLogEntry:
    """One row of ``threshold_tuning_log`` (T-RG-COMPAT-TUNINGLOG-001)."""

    log_id: str
    applied_at: datetime
    measurement_kind: str
    knob: str
    previous_value: float | None
    new_value: float
    target_percentile: float | None
    backup_path: str | None
    note: str | None


def persist_report(conn: duckdb.DuckDBPyConnection, record: ReportRecord) -> None:
    """Idempotent upsert of one ``report_log`` row."""
    sections_enabled_payload = json.dumps(list(record.sections_enabled))
    sections_rendered_payload = json.dumps(list(record.sections_rendered))
    sections_failed_payload = json.dumps([dict(s) for s in record.sections_failed])
    existing = conn.execute(
        "SELECT 1 FROM report_log WHERE report_id = ?", [record.report_id]
    ).fetchone()
    params = [
        record.generated_at,
        record.since_ts,
        record.until_ts,
        sections_enabled_payload,
        sections_rendered_payload,
        sections_failed_payload,
        record.library_version,
        record.disclaimer_version_hash,
        record.rendered_terminal_text,
        record.rendered_markdown_text,
        record.markdown_path,
        record.rendered_html_text,
        record.html_path,
        record.duration_seconds,
    ]
    if existing is None:
        conn.execute(
            "INSERT INTO report_log ("
            "report_id, generated_at, since_ts, until_ts, "
            "sections_enabled, sections_rendered, sections_failed, "
            "library_version, disclaimer_version_hash, "
            "rendered_terminal_text, rendered_markdown_text, "
            "markdown_path, rendered_html_text, html_path, "
            "duration_seconds"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [record.report_id, *params],
        )
    else:
        conn.execute(
            "UPDATE report_log SET "
            "generated_at = ?, since_ts = ?, until_ts = ?, "
            "sections_enabled = ?, sections_rendered = ?, sections_failed = ?, "
            "library_version = ?, disclaimer_version_hash = ?, "
            "rendered_terminal_text = ?, rendered_markdown_text = ?, "
            "markdown_path = ?, rendered_html_text = ?, html_path = ?, "
            "duration_seconds = ? "
            "WHERE report_id = ?",
            [*params, record.report_id],
        )


def get_report(conn: duckdb.DuckDBPyConnection, *, report_id: str) -> ReportRecord | None:
    row = conn.execute(
        "SELECT report_id, generated_at, since_ts, until_ts, "
        "sections_enabled, sections_rendered, sections_failed, "
        "library_version, disclaimer_version_hash, "
        "rendered_terminal_text, rendered_markdown_text, "
        "markdown_path, rendered_html_text, html_path, "
        "duration_seconds "
        "FROM report_log WHERE report_id = ?",
        [report_id],
    ).fetchone()
    if row is None:
        return None
    return _record_from_row(row)


def query_last_report(
    conn: duckdb.DuckDBPyConnection,
) -> ReportRecord | None:
    """Return the most recent report_log row by ``generated_at``."""
    row = conn.execute(
        "SELECT report_id, generated_at, since_ts, until_ts, "
        "sections_enabled, sections_rendered, sections_failed, "
        "library_version, disclaimer_version_hash, "
        "rendered_terminal_text, rendered_markdown_text, "
        "markdown_path, rendered_html_text, html_path, "
        "duration_seconds "
        "FROM report_log ORDER BY generated_at DESC LIMIT 1"
    ).fetchone()
    return _record_from_row(row) if row is not None else None


def list_reports(
    conn: duckdb.DuckDBPyConnection,
    *,
    since: datetime | None = None,
    limit: int | None = None,
) -> tuple[ReportRecord, ...]:
    base_query = (
        "SELECT report_id, generated_at, since_ts, until_ts, "
        "sections_enabled, sections_rendered, sections_failed, "
        "library_version, disclaimer_version_hash, "
        "rendered_terminal_text, rendered_markdown_text, "
        "markdown_path, rendered_html_text, html_path, "
        "duration_seconds "
        "FROM report_log"
    )
    params: list[Any] = []
    if since is not None:
        base_query += " WHERE generated_at >= ?"
        params.append(since)
    base_query += " ORDER BY generated_at DESC"
    if limit is not None:
        base_query += f" LIMIT {int(limit)}"
    rows = conn.execute(base_query, params).fetchall()
    return tuple(_record_from_row(r) for r in rows)


# -- internals --------------------------------------------------------------


def _record_from_row(row: tuple[Any, ...]) -> ReportRecord:
    sections_enabled = _decode_list(row[4])
    sections_rendered = _decode_list(row[5])
    sections_failed_raw = _decode_list(row[6])
    sections_failed = tuple(
        s if isinstance(s, dict) else {"section": str(s)} for s in sections_failed_raw
    )
    return ReportRecord(
        report_id=str(row[0]),
        generated_at=row[1],
        since_ts=row[2],
        until_ts=row[3],
        sections_enabled=tuple(str(s) for s in sections_enabled),
        sections_rendered=tuple(str(s) for s in sections_rendered),
        sections_failed=sections_failed,
        library_version=int(row[7]),
        disclaimer_version_hash=str(row[8]),
        rendered_terminal_text=str(row[9]),
        rendered_markdown_text=(str(row[10]) if row[10] is not None else None),
        markdown_path=(str(row[11]) if row[11] is not None else None),
        rendered_html_text=(str(row[12]) if row[12] is not None else None),
        html_path=(str(row[13]) if row[13] is not None else None),
        duration_seconds=(float(row[14]) if row[14] is not None else None),
    )


def _decode_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(decoded, list):
            return decoded
    return []


# -- threshold measurements (T-RG-COMPAT-MEAS-001) -------------------------


def persist_threshold_measurement(
    conn: duckdb.DuckDBPyConnection,
    *,
    report_id: str,
    measurement_kind: str,
    measured_at: datetime,
    distribution: dict[str, Any],
) -> None:
    """Idempotent upsert of one ``report_threshold_measurements`` row.

    The ``distribution`` payload is the ``compute_distribution``
    return value from ``engines.measurements``; we extract the
    three top-level columns (n_observations, n_above_threshold,
    configured_threshold) for cheap querying and persist the
    full payload as JSON for richer inspection.
    """
    n = int(distribution.get("n", 0))
    n_above = int(distribution.get("n_above_threshold", 0))
    threshold = float(distribution.get("configured_threshold", 0.0))
    payload = json.dumps(distribution, default=_json_default)
    existing = conn.execute(
        "SELECT 1 FROM report_threshold_measurements WHERE report_id = ? AND measurement_kind = ?",
        [report_id, measurement_kind],
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO report_threshold_measurements ("
            "report_id, measurement_kind, measured_at, n_observations, "
            "n_above_threshold, configured_threshold, distribution_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            [report_id, measurement_kind, measured_at, n, n_above, threshold, payload],
        )
    else:
        conn.execute(
            "UPDATE report_threshold_measurements SET "
            "measured_at = ?, n_observations = ?, n_above_threshold = ?, "
            "configured_threshold = ?, distribution_json = ? "
            "WHERE report_id = ? AND measurement_kind = ?",
            [measured_at, n, n_above, threshold, payload, report_id, measurement_kind],
        )


def list_threshold_measurements(
    conn: duckdb.DuckDBPyConnection,
    *,
    measurement_kind: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
) -> tuple[ThresholdMeasurementRecord, ...]:
    """Return historical threshold measurements, newest first.

    Filtering is purely additive — pass none, one, or any combination
    of ``measurement_kind`` and ``since``.
    """
    base = (
        "SELECT report_id, measurement_kind, measured_at, n_observations, "
        "n_above_threshold, configured_threshold, distribution_json "
        "FROM report_threshold_measurements"
    )
    where: list[str] = []
    params: list[Any] = []
    if measurement_kind is not None:
        where.append("measurement_kind = ?")
        params.append(measurement_kind)
    if since is not None:
        where.append("measured_at >= ?")
        params.append(since)
    if where:
        base += " WHERE " + " AND ".join(where)
    base += " ORDER BY measured_at DESC"
    if limit is not None:
        base += f" LIMIT {int(limit)}"
    rows = conn.execute(base, params).fetchall()
    return tuple(_measurement_from_row(r) for r in rows)


class PruneConfirmationError(RuntimeError):
    """Raised when ``prune_threshold_measurements`` is called without confirm=True."""


def prune_threshold_measurements(
    conn: duckdb.DuckDBPyConnection,
    *,
    before: datetime | None = None,
    keep_last: int | None = None,
    measurement_kind: str | None = None,
    confirm: bool = False,
) -> int:
    """Delete old ``report_threshold_measurements`` rows.

    Either ``before`` or ``keep_last`` (or both) must be supplied.
    The two strategies stack: if both are passed, rows are deleted
    when *either* condition fires (older than ``before`` OR beyond
    the ``keep_last`` newest rows for the matching kind).

    ``measurement_kind`` scopes the prune to one kind. Without it,
    every kind is considered.

    Returns the number of rows actually deleted.

    The ``confirm`` parameter is a guard so a careless caller
    can't blow away historical data without saying so. Mirrors
    ``signal_scanner.persistence.operations.prune_before``.

    The ``report_threshold_measurements`` table is informational
    only — pruning never affects report generation or measurement
    recording. The CLI default keeps everything; operators opt
    in.
    """
    if not confirm:
        raise PruneConfirmationError("prune_threshold_measurements: confirm=True required")
    if before is None and keep_last is None:
        raise ValueError(
            "prune_threshold_measurements: at least one of `before` or `keep_last` must be supplied"
        )

    deleted_rows: int = 0

    # Strategy 1: delete by absolute cutoff.
    if before is not None:
        if measurement_kind is not None:
            cur = conn.execute(
                "DELETE FROM report_threshold_measurements "
                "WHERE measured_at < ? AND measurement_kind = ? "
                "RETURNING 1",
                [before, measurement_kind],
            ).fetchall()
        else:
            cur = conn.execute(
                "DELETE FROM report_threshold_measurements WHERE measured_at < ? RETURNING 1",
                [before],
            ).fetchall()
        deleted_rows += len(cur)

    # Strategy 2: delete everything beyond the newest ``keep_last`` per kind.
    if keep_last is not None:
        if keep_last < 0:
            raise ValueError("prune_threshold_measurements: keep_last must be >= 0")
        # Scope by kind if requested, else loop over distinct kinds.
        if measurement_kind is not None:
            kinds_to_consider: list[str] = [measurement_kind]
        else:
            kind_rows = conn.execute(
                "SELECT DISTINCT measurement_kind FROM report_threshold_measurements"
            ).fetchall()
            kinds_to_consider = [str(r[0]) for r in kind_rows]
        for k in kinds_to_consider:
            # Find the cut date: the measured_at of the
            # ``keep_last``-th newest row. Anything older goes.
            cutoff_row = conn.execute(
                "SELECT measured_at FROM report_threshold_measurements "
                "WHERE measurement_kind = ? "
                "ORDER BY measured_at DESC "
                "LIMIT 1 OFFSET ?",
                [k, keep_last],
            ).fetchone()
            if cutoff_row is None:
                continue
            cur = conn.execute(
                "DELETE FROM report_threshold_measurements "
                "WHERE measurement_kind = ? AND measured_at <= ? "
                "RETURNING 1",
                [k, cutoff_row[0]],
            ).fetchall()
            deleted_rows += len(cur)

    return deleted_rows


def _measurement_from_row(row: tuple[Any, ...]) -> ThresholdMeasurementRecord:
    distribution_raw = row[6]
    if isinstance(distribution_raw, dict):
        distribution = distribution_raw
    else:
        try:
            decoded = json.loads(str(distribution_raw)) if distribution_raw else {}
        except json.JSONDecodeError:
            decoded = {}
        distribution = decoded if isinstance(decoded, dict) else {}
    return ThresholdMeasurementRecord(
        report_id=str(row[0]),
        measurement_kind=str(row[1]),
        measured_at=row[2],
        n_observations=int(row[3]),
        n_above_threshold=int(row[4]),
        configured_threshold=float(row[5]),
        distribution=distribution,
    )


def _json_default(value: Any) -> Any:
    """JSON encoder fallback for any types compute_distribution may emit."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set | tuple):
        return list(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


# -- threshold tuning log (T-RG-COMPAT-TUNINGLOG-001) ---------------------


def persist_tuning_log_entry(
    conn: duckdb.DuckDBPyConnection,
    *,
    log_id: str,
    applied_at: datetime,
    measurement_kind: str,
    knob: str,
    previous_value: float | None,
    new_value: float,
    target_percentile: float | None,
    backup_path: str | None,
    note: str | None,
) -> None:
    """Insert one ``threshold_tuning_log`` row.

    Append-only by design — there's no upsert path. The caller
    should generate a fresh ``log_id`` per entry (typically a
    UUID4 or a deterministic ``applied_at + kind`` hash). Failures
    raise; the caller decides whether to swallow them.
    """
    conn.execute(
        "INSERT INTO threshold_tuning_log ("
        "log_id, applied_at, measurement_kind, knob, "
        "previous_value, new_value, target_percentile, "
        "backup_path, note"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            log_id,
            applied_at,
            measurement_kind,
            knob,
            previous_value,
            new_value,
            target_percentile,
            backup_path,
            note,
        ],
    )


def list_tuning_log_entries(
    conn: duckdb.DuckDBPyConnection,
    *,
    measurement_kind: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
) -> tuple[ThresholdTuningLogEntry, ...]:
    """Return historical tuning-log entries, newest first."""
    base = (
        "SELECT log_id, applied_at, measurement_kind, knob, "
        "previous_value, new_value, target_percentile, "
        "backup_path, note "
        "FROM threshold_tuning_log"
    )
    where: list[str] = []
    params: list[Any] = []
    if measurement_kind is not None:
        where.append("measurement_kind = ?")
        params.append(measurement_kind)
    if since is not None:
        where.append("applied_at >= ?")
        params.append(since)
    if where:
        base += " WHERE " + " AND ".join(where)
    base += " ORDER BY applied_at DESC"
    if limit is not None:
        base += f" LIMIT {int(limit)}"
    rows = conn.execute(base, params).fetchall()
    return tuple(_tuning_log_from_row(r) for r in rows)


def get_tuning_log_entry(
    conn: duckdb.DuckDBPyConnection,
    *,
    log_id: str,
) -> ThresholdTuningLogEntry | None:
    """Return one tuning-log entry by log_id, or None if not found."""
    row = conn.execute(
        "SELECT log_id, applied_at, measurement_kind, knob, "
        "previous_value, new_value, target_percentile, "
        "backup_path, note "
        "FROM threshold_tuning_log WHERE log_id = ?",
        [log_id],
    ).fetchone()
    if row is None:
        return None
    return _tuning_log_from_row(row)


def _tuning_log_from_row(row: tuple[Any, ...]) -> ThresholdTuningLogEntry:
    return ThresholdTuningLogEntry(
        log_id=str(row[0]),
        applied_at=row[1],
        measurement_kind=str(row[2]),
        knob=str(row[3]),
        previous_value=(float(row[4]) if row[4] is not None else None),
        new_value=float(row[5]),
        target_percentile=(float(row[6]) if row[6] is not None else None),
        backup_path=(str(row[7]) if row[7] is not None else None),
        note=(str(row[8]) if row[8] is not None else None),
    )


__all__ = [
    "PruneConfirmationError",
    "ThresholdMeasurementRecord",
    "ThresholdTuningLogEntry",
    "get_report",
    "get_tuning_log_entry",
    "list_reports",
    "list_threshold_measurements",
    "list_tuning_log_entries",
    "persist_report",
    "persist_threshold_measurement",
    "persist_tuning_log_entry",
    "prune_threshold_measurements",
    "query_last_report",
]


_RESERVED: tuple[Any, ...] = (Iterable,)
