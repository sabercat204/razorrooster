"""Monitor persistence helpers (T-MON-011; design §3.3)."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import duckdb

from razor_rooster.monitor.models import (
    AlertTier,
    FollowUp,
    FollowUpNote,
    MonitorCycle,
)

logger = logging.getLogger(__name__)


# -- monitor_cycles ---------------------------------------------------------


def write_cycle(conn: duckdb.DuckDBPyConnection, cycle: MonitorCycle) -> None:
    """Idempotent upsert of one monitor_cycles row."""
    alerts_payload = json.dumps(dict(cycle.alerts_by_tier))
    error_payload = json.dumps(dict(cycle.error_summary)) if cycle.error_summary else None
    existing = conn.execute(
        "SELECT 1 FROM monitor_cycles WHERE cycle_id = ?", [cycle.cycle_id]
    ).fetchone()
    params = [
        cycle.started_at,
        cycle.completed_at,
        cycle.follow_ups_total,
        cycle.follow_ups_with_alerts,
        alerts_payload,
        cycle.duration_seconds,
        error_payload,
    ]
    if existing is None:
        conn.execute(
            "INSERT INTO monitor_cycles ("
            "cycle_id, started_at, completed_at, follow_ups_total, "
            "follow_ups_with_alerts, alerts_by_tier, duration_seconds, error_summary"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [cycle.cycle_id, *params],
        )
    else:
        conn.execute(
            "UPDATE monitor_cycles SET "
            "started_at = ?, completed_at = ?, follow_ups_total = ?, "
            "follow_ups_with_alerts = ?, alerts_by_tier = ?, "
            "duration_seconds = ?, error_summary = ? "
            "WHERE cycle_id = ?",
            [*params, cycle.cycle_id],
        )


def complete_cycle(
    conn: duckdb.DuckDBPyConnection,
    *,
    cycle_id: str,
    completed_at: datetime,
    follow_ups_total: int,
    follow_ups_with_alerts: int,
    alerts_by_tier: dict[str, int],
    duration_seconds: float,
    error_summary: dict[str, Any] | None = None,
) -> None:
    alerts_payload = json.dumps(alerts_by_tier)
    error_payload = json.dumps(error_summary) if error_summary else None
    conn.execute(
        "UPDATE monitor_cycles SET "
        "completed_at = ?, follow_ups_total = ?, follow_ups_with_alerts = ?, "
        "alerts_by_tier = ?, duration_seconds = ?, "
        "error_summary = COALESCE(?, error_summary) "
        "WHERE cycle_id = ?",
        [
            completed_at,
            follow_ups_total,
            follow_ups_with_alerts,
            alerts_payload,
            duration_seconds,
            error_payload,
            cycle_id,
        ],
    )


def query_cycle(conn: duckdb.DuckDBPyConnection, *, cycle_id: str) -> MonitorCycle | None:
    row = conn.execute(
        "SELECT cycle_id, started_at, completed_at, follow_ups_total, "
        "follow_ups_with_alerts, alerts_by_tier, duration_seconds, error_summary "
        "FROM monitor_cycles WHERE cycle_id = ?",
        [cycle_id],
    ).fetchone()
    if row is None:
        return None
    alerts: dict[str, int] = {}
    if isinstance(row[5], str) and row[5]:
        try:
            decoded = json.loads(row[5])
            if isinstance(decoded, dict):
                alerts = {str(k): int(v) for k, v in decoded.items()}
        except (json.JSONDecodeError, ValueError, TypeError):
            alerts = {}
    error_summary: dict[str, Any] | None = None
    if isinstance(row[7], str) and row[7]:
        try:
            decoded_es = json.loads(row[7])
            if isinstance(decoded_es, dict):
                error_summary = decoded_es
        except json.JSONDecodeError:
            error_summary = None
    return MonitorCycle(
        cycle_id=str(row[0]),
        started_at=row[1],
        completed_at=row[2],
        follow_ups_total=int(row[3]),
        follow_ups_with_alerts=int(row[4]),
        alerts_by_tier=alerts,
        duration_seconds=(float(row[6]) if row[6] is not None else None),
        error_summary=error_summary,
    )


# -- follow_ups -------------------------------------------------------------


def persist_follow_up(conn: duckdb.DuckDBPyConnection, follow_up: FollowUp) -> None:
    """Idempotent upsert of one follow_ups row."""
    ci_payload = (
        json.dumps(list(follow_up.current_model_ci))
        if follow_up.current_model_ci is not None
        else None
    )
    precursor_payload = json.dumps([dict(p) for p in follow_up.precursor_snapshot])
    invalidation_payload = json.dumps([dict(e) for e in follow_up.invalidation_evaluations])
    alert_tiers_payload = json.dumps(list(follow_up.alert_tiers))
    existing = conn.execute(
        "SELECT 1 FROM follow_ups WHERE follow_up_id = ?", [follow_up.follow_up_id]
    ).fetchone()
    params = [
        follow_up.cycle_id,
        follow_up.analysis_id,
        follow_up.analysis_model_p,
        follow_up.analysis_market_p,
        follow_up.analysis_computed_at,
        follow_up.current_scan_id,
        follow_up.current_model_p,
        ci_payload,
        follow_up.current_market_p,
        follow_up.current_market_snapshot_ts,
        follow_up.model_probability_shift,
        follow_up.model_shift_band,
        follow_up.market_probability_shift,
        follow_up.market_shift_band,
        precursor_payload,
        follow_up.days_since_analysis,
        follow_up.days_to_resolution,
        follow_up.time_decay_alert,
        invalidation_payload,
        follow_up.invalidation_triggered_count,
        follow_up.resolution_status,
        follow_up.recommended_review,
        follow_up.primary_alert_tier,
        alert_tiers_payload,
        follow_up.reasoning_text,
        follow_up.source_stale_warning,
        follow_up.library_stale_warning,
        follow_up.error,
        follow_up.computed_at or datetime.now(tz=UTC),
        follow_up.venue,
    ]
    if existing is None:
        conn.execute(
            "INSERT INTO follow_ups ("
            "follow_up_id, cycle_id, analysis_id, analysis_model_p, "
            "analysis_market_p, analysis_computed_at, current_scan_id, "
            "current_model_p, current_model_ci, current_market_p, "
            "current_market_snapshot_ts, model_probability_shift, "
            "model_shift_band, market_probability_shift, market_shift_band, "
            "precursor_snapshot, days_since_analysis, days_to_resolution, "
            "time_decay_alert, invalidation_evaluations, "
            "invalidation_triggered_count, resolution_status, "
            "recommended_review, primary_alert_tier, alert_tiers, "
            "reasoning_text, source_stale_warning, library_stale_warning, "
            "error, computed_at, venue"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [follow_up.follow_up_id, *params],
        )
    else:
        conn.execute(
            "UPDATE follow_ups SET "
            "cycle_id = ?, analysis_id = ?, analysis_model_p = ?, "
            "analysis_market_p = ?, analysis_computed_at = ?, "
            "current_scan_id = ?, current_model_p = ?, current_model_ci = ?, "
            "current_market_p = ?, current_market_snapshot_ts = ?, "
            "model_probability_shift = ?, model_shift_band = ?, "
            "market_probability_shift = ?, market_shift_band = ?, "
            "precursor_snapshot = ?, days_since_analysis = ?, "
            "days_to_resolution = ?, time_decay_alert = ?, "
            "invalidation_evaluations = ?, invalidation_triggered_count = ?, "
            "resolution_status = ?, recommended_review = ?, "
            "primary_alert_tier = ?, alert_tiers = ?, reasoning_text = ?, "
            "source_stale_warning = ?, library_stale_warning = ?, "
            "error = ?, computed_at = ?, venue = ? "
            "WHERE follow_up_id = ?",
            [*params, follow_up.follow_up_id],
        )


def query_follow_ups(
    conn: duckdb.DuckDBPyConnection,
    *,
    cycle_id: str | None = None,
    analysis_id: str | None = None,
    primary_alert_tier: AlertTier | None = None,
    venue: str | None = None,
    since: datetime | None = None,
) -> tuple[FollowUp, ...]:
    """Return follow-ups matching the filter."""
    base_query = (
        "SELECT follow_up_id, cycle_id, analysis_id, analysis_model_p, "
        "analysis_market_p, analysis_computed_at, current_scan_id, "
        "current_model_p, current_model_ci, current_market_p, "
        "current_market_snapshot_ts, model_probability_shift, "
        "model_shift_band, market_probability_shift, market_shift_band, "
        "precursor_snapshot, days_since_analysis, days_to_resolution, "
        "time_decay_alert, invalidation_evaluations, "
        "invalidation_triggered_count, resolution_status, "
        "recommended_review, primary_alert_tier, alert_tiers, "
        "reasoning_text, source_stale_warning, library_stale_warning, "
        "error, computed_at, venue "
        "FROM follow_ups"
    )
    conditions: list[str] = []
    params: list[Any] = []
    if cycle_id is not None:
        conditions.append("cycle_id = ?")
        params.append(cycle_id)
    if analysis_id is not None:
        conditions.append("analysis_id = ?")
        params.append(analysis_id)
    if primary_alert_tier is not None:
        conditions.append("primary_alert_tier = ?")
        params.append(primary_alert_tier)
    if venue is not None:
        conditions.append("venue = ?")
        params.append(venue)
    if since is not None:
        conditions.append("computed_at >= ?")
        params.append(since)
    if conditions:
        base_query += " WHERE " + " AND ".join(conditions)
    base_query += " ORDER BY computed_at DESC, analysis_id"
    rows = conn.execute(base_query, params).fetchall()
    return tuple(_follow_up_from_row(r) for r in rows)


def get_follow_up(conn: duckdb.DuckDBPyConnection, *, follow_up_id: str) -> FollowUp | None:
    rows = conn.execute(
        "SELECT follow_up_id, cycle_id, analysis_id, analysis_model_p, "
        "analysis_market_p, analysis_computed_at, current_scan_id, "
        "current_model_p, current_model_ci, current_market_p, "
        "current_market_snapshot_ts, model_probability_shift, "
        "model_shift_band, market_probability_shift, market_shift_band, "
        "precursor_snapshot, days_since_analysis, days_to_resolution, "
        "time_decay_alert, invalidation_evaluations, "
        "invalidation_triggered_count, resolution_status, "
        "recommended_review, primary_alert_tier, alert_tiers, "
        "reasoning_text, source_stale_warning, library_stale_warning, "
        "error, computed_at, venue "
        "FROM follow_ups WHERE follow_up_id = ?",
        [follow_up_id],
    ).fetchone()
    return _follow_up_from_row(rows) if rows is not None else None


def query_alerts(
    conn: duckdb.DuckDBPyConnection,
    *,
    tier: AlertTier | None = None,
    since: datetime | None = None,
) -> tuple[FollowUp, ...]:
    """Return follow-ups with alerts, ordered by tier priority + recency."""
    base_query = (
        "SELECT follow_up_id, cycle_id, analysis_id, analysis_model_p, "
        "analysis_market_p, analysis_computed_at, current_scan_id, "
        "current_model_p, current_model_ci, current_market_p, "
        "current_market_snapshot_ts, model_probability_shift, "
        "model_shift_band, market_probability_shift, market_shift_band, "
        "precursor_snapshot, days_since_analysis, days_to_resolution, "
        "time_decay_alert, invalidation_evaluations, "
        "invalidation_triggered_count, resolution_status, "
        "recommended_review, primary_alert_tier, alert_tiers, "
        "reasoning_text, source_stale_warning, library_stale_warning, "
        "error, computed_at, venue "
        "FROM follow_ups WHERE primary_alert_tier IS NOT NULL"
    )
    params: list[Any] = []
    if tier is not None:
        base_query += " AND primary_alert_tier = ?"
        params.append(tier)
    if since is not None:
        base_query += " AND computed_at >= ?"
        params.append(since)
    # Order: tier priority first, then recency.
    base_query += (
        " ORDER BY CASE primary_alert_tier "
        "WHEN 'resolution' THEN 0 "
        "WHEN 'invalidation_triggered' THEN 1 "
        "WHEN 'material_shift' THEN 2 "
        "WHEN 'precursor_shift' THEN 3 "
        "WHEN 'time_decay' THEN 4 "
        "ELSE 5 END, computed_at DESC"
    )
    rows = conn.execute(base_query, params).fetchall()
    return tuple(_follow_up_from_row(r) for r in rows)


def query_trajectory(conn: duckdb.DuckDBPyConnection, *, analysis_id: str) -> tuple[FollowUp, ...]:
    """Return all follow-ups for an analysis ordered chronologically."""
    rows = conn.execute(
        "SELECT follow_up_id, cycle_id, analysis_id, analysis_model_p, "
        "analysis_market_p, analysis_computed_at, current_scan_id, "
        "current_model_p, current_model_ci, current_market_p, "
        "current_market_snapshot_ts, model_probability_shift, "
        "model_shift_band, market_probability_shift, market_shift_band, "
        "precursor_snapshot, days_since_analysis, days_to_resolution, "
        "time_decay_alert, invalidation_evaluations, "
        "invalidation_triggered_count, resolution_status, "
        "recommended_review, primary_alert_tier, alert_tiers, "
        "reasoning_text, source_stale_warning, library_stale_warning, "
        "error, computed_at, venue "
        "FROM follow_ups WHERE analysis_id = ? ORDER BY computed_at ASC",
        [analysis_id],
    ).fetchall()
    return tuple(_follow_up_from_row(r) for r in rows)


# -- follow_up_notes --------------------------------------------------------


def add_note(
    conn: duckdb.DuckDBPyConnection,
    *,
    follow_up_id: str,
    note_text: str,
    set_by: str = "operator",
    when: datetime | None = None,
) -> FollowUpNote:
    """Append a note to a follow-up."""
    note_id = str(uuid.uuid4())
    ts = when or datetime.now(tz=UTC)
    conn.execute(
        "INSERT INTO follow_up_notes (note_id, follow_up_id, note_text, set_at, set_by) "
        "VALUES (?, ?, ?, ?, ?)",
        [note_id, follow_up_id, note_text, ts, set_by],
    )
    return FollowUpNote(
        note_id=note_id,
        follow_up_id=follow_up_id,
        note_text=note_text,
        set_at=ts,
        set_by=set_by,
    )


def query_notes(conn: duckdb.DuckDBPyConnection, *, follow_up_id: str) -> tuple[FollowUpNote, ...]:
    rows = conn.execute(
        "SELECT note_id, follow_up_id, note_text, set_at, set_by "
        "FROM follow_up_notes WHERE follow_up_id = ? ORDER BY set_at DESC",
        [follow_up_id],
    ).fetchall()
    return tuple(
        FollowUpNote(
            note_id=str(r[0]),
            follow_up_id=str(r[1]),
            note_text=str(r[2]),
            set_at=r[3],
            set_by=str(r[4]),
        )
        for r in rows
    )


# -- internals --------------------------------------------------------------


def _follow_up_from_row(row: tuple[Any, ...]) -> FollowUp:
    ci_value: tuple[float, float] | None = None
    if isinstance(row[8], str) and row[8]:
        try:
            decoded = json.loads(row[8])
            if isinstance(decoded, list) and len(decoded) >= 2:
                ci_value = (float(decoded[0]), float(decoded[1]))
        except (json.JSONDecodeError, ValueError, TypeError):
            ci_value = None
    precursor_snapshot: tuple[Mapping[str, Any], ...] = ()
    if isinstance(row[15], str) and row[15]:
        try:
            decoded_p = json.loads(row[15])
            if isinstance(decoded_p, list):
                precursor_snapshot = tuple(
                    {str(k): v for k, v in entry.items()}
                    for entry in decoded_p
                    if isinstance(entry, dict)
                )
        except json.JSONDecodeError:
            precursor_snapshot = ()
    invalidation_evaluations: tuple[Mapping[str, Any], ...] = ()
    if isinstance(row[19], str) and row[19]:
        try:
            decoded_i = json.loads(row[19])
            if isinstance(decoded_i, list):
                invalidation_evaluations = tuple(
                    {str(k): v for k, v in entry.items()}
                    for entry in decoded_i
                    if isinstance(entry, dict)
                )
        except json.JSONDecodeError:
            invalidation_evaluations = ()
    alert_tiers: tuple[AlertTier, ...] = ()
    if isinstance(row[24], str) and row[24]:
        try:
            decoded_t = json.loads(row[24])
            if isinstance(decoded_t, list):
                alert_tiers = tuple(str(t) for t in decoded_t)  # type: ignore[misc]
        except json.JSONDecodeError:
            alert_tiers = ()
    return FollowUp(
        follow_up_id=str(row[0]),
        cycle_id=str(row[1]),
        analysis_id=str(row[2]),
        analysis_model_p=float(row[3]),
        analysis_market_p=(float(row[4]) if row[4] is not None else None),
        analysis_computed_at=row[5],
        current_scan_id=(str(row[6]) if row[6] is not None else None),
        current_model_p=(float(row[7]) if row[7] is not None else None),
        current_model_ci=ci_value,
        current_market_p=(float(row[9]) if row[9] is not None else None),
        current_market_snapshot_ts=row[10],
        model_probability_shift=(float(row[11]) if row[11] is not None else None),
        model_shift_band=(row[12] if row[12] is not None else None),
        market_probability_shift=(float(row[13]) if row[13] is not None else None),
        market_shift_band=(row[14] if row[14] is not None else None),
        precursor_snapshot=precursor_snapshot,
        days_since_analysis=int(row[16]),
        days_to_resolution=(int(row[17]) if row[17] is not None else None),
        time_decay_alert=bool(row[18]),
        invalidation_evaluations=invalidation_evaluations,
        invalidation_triggered_count=int(row[20]),
        resolution_status=row[21],
        recommended_review=bool(row[22]),
        primary_alert_tier=(row[23] if row[23] is not None else None),
        alert_tiers=alert_tiers,
        reasoning_text=str(row[25]),
        source_stale_warning=bool(row[26]),
        library_stale_warning=bool(row[27]),
        error=(str(row[28]) if row[28] is not None else None),
        computed_at=row[29],
        venue=row[30] if len(row) > 30 and row[30] is not None else "polymarket",
    )


__all__ = [
    "add_note",
    "complete_cycle",
    "get_follow_up",
    "persist_follow_up",
    "query_alerts",
    "query_cycle",
    "query_follow_ups",
    "query_notes",
    "query_trajectory",
    "write_cycle",
]


_RESERVED: tuple[Any, ...] = (Iterable,)
