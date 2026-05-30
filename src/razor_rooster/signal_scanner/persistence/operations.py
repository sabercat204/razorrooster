"""Signal-scanner persistence helpers (T-SCAN-011; design §3.3).

Typed write/read helpers against the three ``scan_*`` tables. Callers
pass in a DuckDB connection; the helpers do not acquire connections
from a store. All operations are idempotent at the
``(scan_id, class_id)`` primary key — repeated insertion of the same
record updates rather than duplicates (REQ-SCAN-PERSIST-003 holds
because each scan generates a fresh ``scan_id``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import duckdb

from razor_rooster.signal_scanner.models import (
    ScanRecord,
    ScanSummary,
    Trace,
)

logger = logging.getLogger(__name__)


# -- scan_summaries ---------------------------------------------------------


def write_summary(
    conn: duckdb.DuckDBPyConnection,
    summary: ScanSummary,
) -> None:
    """Insert a fresh scan_summaries row at scan start.

    Idempotent on ``scan_id``: re-running with the same id replaces
    the prior row. In practice each ``run_scan`` allocates a new UUID
    so collisions don't happen in the wild.
    """
    config_payload = json.dumps(dict(summary.config_snapshot)) if summary.config_snapshot else None
    error_payload = json.dumps(dict(summary.error_summary)) if summary.error_summary else None
    existing = conn.execute(
        "SELECT 1 FROM scan_summaries WHERE scan_id = ?", [summary.scan_id]
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO scan_summaries ("
            "scan_id, scan_started_at, scan_completed_at, pattern_library_version, "
            "classes_total, classes_succeeded, classes_failed, classes_skipped, "
            "candidates_count, library_stale_warning, config_snapshot, error_summary"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                summary.scan_id,
                summary.scan_started_at,
                summary.scan_completed_at,
                summary.pattern_library_version,
                summary.classes_total,
                summary.classes_succeeded,
                summary.classes_failed,
                summary.classes_skipped,
                summary.candidates_count,
                summary.library_stale_warning,
                config_payload,
                error_payload,
            ],
        )
    else:
        conn.execute(
            "UPDATE scan_summaries SET "
            "scan_started_at = ?, scan_completed_at = ?, pattern_library_version = ?, "
            "classes_total = ?, classes_succeeded = ?, classes_failed = ?, "
            "classes_skipped = ?, candidates_count = ?, library_stale_warning = ?, "
            "config_snapshot = ?, error_summary = ? "
            "WHERE scan_id = ?",
            [
                summary.scan_started_at,
                summary.scan_completed_at,
                summary.pattern_library_version,
                summary.classes_total,
                summary.classes_succeeded,
                summary.classes_failed,
                summary.classes_skipped,
                summary.candidates_count,
                summary.library_stale_warning,
                config_payload,
                error_payload,
                summary.scan_id,
            ],
        )


def complete_summary(
    conn: duckdb.DuckDBPyConnection,
    *,
    scan_id: str,
    completed_at: datetime,
    classes_succeeded: int,
    classes_failed: int,
    classes_skipped: int,
    candidates_count: int,
    error_summary: dict[str, Any] | None = None,
) -> None:
    """Stamp a scan as complete; fills in the aggregate fields."""
    error_payload = json.dumps(error_summary) if error_summary else None
    conn.execute(
        "UPDATE scan_summaries SET scan_completed_at = ?, "
        "classes_succeeded = ?, classes_failed = ?, classes_skipped = ?, "
        "candidates_count = ?, error_summary = COALESCE(?, error_summary) "
        "WHERE scan_id = ?",
        [
            completed_at,
            classes_succeeded,
            classes_failed,
            classes_skipped,
            candidates_count,
            error_payload,
            scan_id,
        ],
    )


# -- scan_records -----------------------------------------------------------


def persist_record(
    conn: duckdb.DuckDBPyConnection,
    record: ScanRecord,
) -> None:
    """Idempotent upsert of one scan_records row.

    The (scan_id, class_id) primary key means a re-run of the same
    class within the same scan replaces the prior row. New scans
    always allocate a fresh scan_id so historical scans stay
    immutable (REQ-SCAN-PERSIST-003).
    """
    existing = conn.execute(
        "SELECT 1 FROM scan_records WHERE scan_id = ? AND class_id = ?",
        [record.scan_id, record.class_id],
    ).fetchone()
    params = [
        record.class_definition_version,
        record.pattern_library_version,
        record.data_as_of,
        record.scan_started_at,
        record.scan_completed_at,
        record.base_rate,
        record.base_rate_ci_lower,
        record.base_rate_ci_upper,
        record.posterior,
        record.posterior_ci_lower,
        record.posterior_ci_upper,
        record.log_odds_shift,
        record.is_candidate,
        record.candidate_direction,
        record.signature_confidence,
        record.low_signature_confidence,
        record.source_stale_warning,
        record.library_stale_warning,
        record.definition_drift_warning,
        record.no_update_applied,
        record.no_update_reason,
        record.error,
    ]
    if existing is None:
        conn.execute(
            "INSERT INTO scan_records ("
            "scan_id, class_id, class_definition_version, pattern_library_version, "
            "data_as_of, scan_started_at, scan_completed_at, base_rate, "
            "base_rate_ci_lower, base_rate_ci_upper, posterior, posterior_ci_lower, "
            "posterior_ci_upper, log_odds_shift, is_candidate, candidate_direction, "
            "signature_confidence, low_signature_confidence, source_stale_warning, "
            "library_stale_warning, definition_drift_warning, no_update_applied, "
            "no_update_reason, error"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [record.scan_id, record.class_id, *params],
        )
    else:
        conn.execute(
            "UPDATE scan_records SET "
            "class_definition_version = ?, pattern_library_version = ?, "
            "data_as_of = ?, scan_started_at = ?, scan_completed_at = ?, "
            "base_rate = ?, base_rate_ci_lower = ?, base_rate_ci_upper = ?, "
            "posterior = ?, posterior_ci_lower = ?, posterior_ci_upper = ?, "
            "log_odds_shift = ?, is_candidate = ?, candidate_direction = ?, "
            "signature_confidence = ?, low_signature_confidence = ?, "
            "source_stale_warning = ?, library_stale_warning = ?, "
            "definition_drift_warning = ?, no_update_applied = ?, "
            "no_update_reason = ?, error = ? "
            "WHERE scan_id = ? AND class_id = ?",
            [*params, record.scan_id, record.class_id],
        )


def persist_trace(
    conn: duckdb.DuckDBPyConnection,
    trace: Trace,
) -> None:
    """Persist a reasoning trace JSON row, idempotent on PK."""
    payload = json.dumps(dict(trace.payload))
    existing = conn.execute(
        "SELECT 1 FROM scan_traces WHERE scan_id = ? AND class_id = ?",
        [trace.scan_id, trace.class_id],
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO scan_traces (scan_id, class_id, trace_json) VALUES (?, ?, ?)",
            [trace.scan_id, trace.class_id, payload],
        )
    else:
        conn.execute(
            "UPDATE scan_traces SET trace_json = ? WHERE scan_id = ? AND class_id = ?",
            [payload, trace.scan_id, trace.class_id],
        )


# -- queries ----------------------------------------------------------------


def query_recent_candidates(
    conn: duckdb.DuckDBPyConnection,
    *,
    since: datetime | None = None,
    sector: str | None = None,
) -> tuple[ScanRecord, ...]:
    """Return scan records flagged as candidates, ordered by recency.

    ``since`` filters by ``scan_started_at >= since``. ``sector`` is a
    convenience filter that joins against ``pl_event_classes`` to
    restrict by class domain sector.
    """
    if sector is not None:
        query = (
            "SELECT r.* FROM scan_records r "
            "JOIN pl_event_classes c ON r.class_id = c.class_id "
            "WHERE r.is_candidate = TRUE AND c.domain_sector = ?"
        )
        params: list[Any] = [sector]
    else:
        query = "SELECT * FROM scan_records WHERE is_candidate = TRUE"
        params = []
    if since is not None:
        query += " AND scan_started_at >= ?"
        params.append(since)
    query += " ORDER BY scan_started_at DESC, class_id"
    rows = conn.execute(query, params).fetchall()
    return tuple(_record_from_row(r) for r in rows)


def query_scan_records(
    conn: duckdb.DuckDBPyConnection,
    *,
    scan_id: str,
) -> tuple[ScanRecord, ...]:
    """Return all records for a given scan, ordered by class_id."""
    rows = conn.execute(
        "SELECT * FROM scan_records WHERE scan_id = ? ORDER BY class_id",
        [scan_id],
    ).fetchall()
    return tuple(_record_from_row(r) for r in rows)


def query_scan_summary(
    conn: duckdb.DuckDBPyConnection,
    *,
    scan_id: str,
) -> ScanSummary | None:
    """Return the summary row for a scan, or None if not present."""
    row = conn.execute(
        "SELECT scan_id, scan_started_at, scan_completed_at, pattern_library_version, "
        "classes_total, classes_succeeded, classes_failed, classes_skipped, "
        "candidates_count, library_stale_warning, config_snapshot, error_summary "
        "FROM scan_summaries WHERE scan_id = ?",
        [scan_id],
    ).fetchone()
    if row is None:
        return None
    config_snap: dict[str, Any] | None = None
    if isinstance(row[10], str) and row[10]:
        try:
            decoded = json.loads(row[10])
            if isinstance(decoded, dict):
                config_snap = decoded
        except json.JSONDecodeError:
            config_snap = None
    error_snap: dict[str, Any] | None = None
    if isinstance(row[11], str) and row[11]:
        try:
            decoded = json.loads(row[11])
            if isinstance(decoded, dict):
                error_snap = decoded
        except json.JSONDecodeError:
            error_snap = None
    return ScanSummary(
        scan_id=str(row[0]),
        scan_started_at=row[1],
        scan_completed_at=row[2],
        pattern_library_version=int(row[3]),
        classes_total=int(row[4]),
        classes_succeeded=int(row[5]),
        classes_failed=int(row[6]),
        classes_skipped=int(row[7]),
        candidates_count=int(row[8]),
        library_stale_warning=bool(row[9]),
        config_snapshot=config_snap,
        error_summary=error_snap,
    )


def query_trace(
    conn: duckdb.DuckDBPyConnection,
    *,
    scan_id: str,
    class_id: str,
) -> Trace | None:
    """Return the trace JSON for a (scan, class) pair, or None."""
    row = conn.execute(
        "SELECT trace_json FROM scan_traces WHERE scan_id = ? AND class_id = ?",
        [scan_id, class_id],
    ).fetchone()
    if row is None:
        return None
    payload: dict[str, Any] = {}
    if isinstance(row[0], str) and row[0]:
        try:
            decoded = json.loads(row[0])
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            payload = {}
    return Trace(scan_id=scan_id, class_id=class_id, payload=payload)


# -- pruning ----------------------------------------------------------------


class PruneConfirmationError(RuntimeError):
    """Pruning attempted without explicit confirmation."""


def prune_before(
    conn: duckdb.DuckDBPyConnection,
    *,
    before: datetime,
    confirm: bool,
) -> int:
    """Delete scan_summaries / scan_records / scan_traces older than ``before``.

    Requires ``confirm=True``; raises :class:`PruneConfirmationError`
    otherwise. Returns the number of summary rows deleted.
    """
    if not confirm:
        raise PruneConfirmationError(
            "prune_before requires confirm=True; this is intentionally hard"
        )
    scan_ids = [
        str(r[0])
        for r in conn.execute(
            "SELECT scan_id FROM scan_summaries WHERE scan_started_at < ?", [before]
        ).fetchall()
    ]
    if not scan_ids:
        return 0
    placeholders = ", ".join("?" for _ in scan_ids)
    conn.execute(f"DELETE FROM scan_traces WHERE scan_id IN ({placeholders})", scan_ids)
    conn.execute(f"DELETE FROM scan_records WHERE scan_id IN ({placeholders})", scan_ids)
    conn.execute(f"DELETE FROM scan_summaries WHERE scan_id IN ({placeholders})", scan_ids)
    return len(scan_ids)


# -- internals --------------------------------------------------------------


def _record_from_row(row: tuple[Any, ...]) -> ScanRecord:
    """Materialize a ScanRecord from a SELECT * row."""
    # Column order matches scan_records DDL.
    return ScanRecord(
        scan_id=str(row[0]),
        class_id=str(row[1]),
        class_definition_version=int(row[2]),
        pattern_library_version=int(row[3]),
        data_as_of=row[4],
        scan_started_at=row[5],
        scan_completed_at=row[6],
        base_rate=float(row[7]),
        base_rate_ci_lower=float(row[8]),
        base_rate_ci_upper=float(row[9]),
        posterior=float(row[10]),
        posterior_ci_lower=float(row[11]),
        posterior_ci_upper=float(row[12]),
        log_odds_shift=float(row[13]),
        is_candidate=bool(row[14]),
        candidate_direction=(str(row[15]) if row[15] is not None else None),
        signature_confidence=(float(row[16]) if row[16] is not None else None),
        low_signature_confidence=bool(row[17]),
        source_stale_warning=bool(row[18]),
        library_stale_warning=bool(row[19]),
        definition_drift_warning=bool(row[20]),
        no_update_applied=bool(row[21]),
        no_update_reason=(str(row[22]) if row[22] is not None else None),
        error=(str(row[23]) if row[23] is not None else None),
    )


__all__ = [
    "PruneConfirmationError",
    "complete_summary",
    "persist_record",
    "persist_trace",
    "prune_before",
    "query_recent_candidates",
    "query_scan_records",
    "query_scan_summary",
    "query_trace",
    "write_summary",
]


# Reserved imports for future use.
_RESERVED: tuple[Any, ...] = (Iterable,)
