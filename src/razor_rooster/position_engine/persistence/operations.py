"""Position-engine persistence helpers (T-PE-011; design §3.3)."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import duckdb

from razor_rooster.position_engine.models import (
    Analysis,
    AnalysisCycle,
    AnalysisTrace,
    BankrollConfig,
    SetBy,
    WatchState,
    WatchStateValue,
)

logger = logging.getLogger(__name__)


# -- bankroll_config --------------------------------------------------------


def write_bankroll_config(conn: duckdb.DuckDBPyConnection, config: BankrollConfig) -> None:
    """Append a new bankroll_config row. Latest effective_at wins."""
    conn.execute(
        "INSERT INTO bankroll_config ("
        "config_id, analytical_bankroll_usd, max_single_position_pct, "
        "kelly_fraction_default, min_edge_threshold, effective_at, "
        "updated_by, notes"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            config.config_id,
            config.analytical_bankroll_usd,
            config.max_single_position_pct,
            config.kelly_fraction_default,
            config.min_edge_threshold,
            config.effective_at,
            config.updated_by,
            config.notes,
        ],
    )


def latest_bankroll_config(
    conn: duckdb.DuckDBPyConnection,
) -> BankrollConfig | None:
    """Return the most recent bankroll_config row by ``effective_at``."""
    row = conn.execute(
        "SELECT config_id, analytical_bankroll_usd, max_single_position_pct, "
        "kelly_fraction_default, min_edge_threshold, effective_at, "
        "updated_by, notes "
        "FROM bankroll_config ORDER BY effective_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return BankrollConfig(
        config_id=str(row[0]),
        analytical_bankroll_usd=float(row[1]),
        max_single_position_pct=float(row[2]),
        kelly_fraction_default=float(row[3]),
        min_edge_threshold=float(row[4]),
        effective_at=row[5],
        updated_by=str(row[6]),
        notes=(str(row[7]) if row[7] is not None else None),
    )


# -- analysis_cycles --------------------------------------------------------


def write_cycle(conn: duckdb.DuckDBPyConnection, cycle: AnalysisCycle) -> None:
    """Idempotent upsert of one analysis_cycles row."""
    error_payload = json.dumps(dict(cycle.error_summary)) if cycle.error_summary else None
    existing = conn.execute(
        "SELECT 1 FROM analysis_cycles WHERE cycle_id = ?", [cycle.cycle_id]
    ).fetchone()
    params = [
        cycle.started_at,
        cycle.completed_at,
        cycle.bankroll_config_id,
        cycle.analyses_total,
        cycle.analyses_with_positive_kelly,
        cycle.analyses_clamped_by_cap,
        cycle.analyses_clamped_by_liquidity,
        cycle.duration_seconds,
        error_payload,
    ]
    if existing is None:
        conn.execute(
            "INSERT INTO analysis_cycles ("
            "cycle_id, started_at, completed_at, bankroll_config_id, "
            "analyses_total, analyses_with_positive_kelly, "
            "analyses_clamped_by_cap, analyses_clamped_by_liquidity, "
            "duration_seconds, error_summary"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [cycle.cycle_id, *params],
        )
    else:
        conn.execute(
            "UPDATE analysis_cycles SET "
            "started_at = ?, completed_at = ?, bankroll_config_id = ?, "
            "analyses_total = ?, analyses_with_positive_kelly = ?, "
            "analyses_clamped_by_cap = ?, analyses_clamped_by_liquidity = ?, "
            "duration_seconds = ?, error_summary = ? "
            "WHERE cycle_id = ?",
            [*params, cycle.cycle_id],
        )


def complete_cycle(
    conn: duckdb.DuckDBPyConnection,
    *,
    cycle_id: str,
    completed_at: datetime,
    analyses_total: int,
    analyses_with_positive_kelly: int,
    analyses_clamped_by_cap: int,
    analyses_clamped_by_liquidity: int,
    duration_seconds: float,
    error_summary: dict[str, Any] | None = None,
) -> None:
    """Stamp a cycle as complete with aggregate counts."""
    error_payload = json.dumps(error_summary) if error_summary else None
    conn.execute(
        "UPDATE analysis_cycles SET "
        "completed_at = ?, analyses_total = ?, analyses_with_positive_kelly = ?, "
        "analyses_clamped_by_cap = ?, analyses_clamped_by_liquidity = ?, "
        "duration_seconds = ?, error_summary = COALESCE(?, error_summary) "
        "WHERE cycle_id = ?",
        [
            completed_at,
            analyses_total,
            analyses_with_positive_kelly,
            analyses_clamped_by_cap,
            analyses_clamped_by_liquidity,
            duration_seconds,
            error_payload,
            cycle_id,
        ],
    )


def query_cycle(conn: duckdb.DuckDBPyConnection, *, cycle_id: str) -> AnalysisCycle | None:
    row = conn.execute(
        "SELECT cycle_id, started_at, completed_at, bankroll_config_id, "
        "analyses_total, analyses_with_positive_kelly, analyses_clamped_by_cap, "
        "analyses_clamped_by_liquidity, duration_seconds, error_summary "
        "FROM analysis_cycles WHERE cycle_id = ?",
        [cycle_id],
    ).fetchone()
    if row is None:
        return None
    error_summary: dict[str, Any] | None = None
    if isinstance(row[9], str) and row[9]:
        try:
            decoded = json.loads(row[9])
            if isinstance(decoded, dict):
                error_summary = decoded
        except json.JSONDecodeError:
            error_summary = None
    return AnalysisCycle(
        cycle_id=str(row[0]),
        started_at=row[1],
        completed_at=row[2],
        bankroll_config_id=str(row[3]),
        analyses_total=int(row[4]),
        analyses_with_positive_kelly=int(row[5]),
        analyses_clamped_by_cap=int(row[6]),
        analyses_clamped_by_liquidity=int(row[7]),
        duration_seconds=(float(row[8]) if row[8] is not None else None),
        error_summary=error_summary,
    )


# -- analyses ---------------------------------------------------------------


def persist_analysis(conn: duckdb.DuckDBPyConnection, analysis: Analysis) -> None:
    """Idempotent upsert of one analyses row."""
    sensitivity_payload = (
        json.dumps(dict(analysis.sensitivity_analysis)) if analysis.sensitivity_analysis else None
    )
    invalidation_payload = json.dumps([dict(c) for c in analysis.invalidation_criteria])
    existing = conn.execute(
        "SELECT 1 FROM analyses WHERE analysis_id = ?", [analysis.analysis_id]
    ).fetchone()
    params = [
        analysis.cycle_id,
        analysis.comparison_id,
        analysis.class_id,
        analysis.condition_id,
        analysis.bankroll_config_id,
        analysis.model_probability,
        analysis.market_probability,
        analysis.kelly_unclamped,
        analysis.kelly_negative,
        analysis.kelly_clamped_by_max_cap,
        analysis.kelly_clamped_by_liquidity,
        analysis.suggested_fraction,
        analysis.suggested_dollar_size,
        analysis.ev_per_dollar,
        analysis.bankroll_after_1_loss_pct,
        analysis.bankroll_after_3_losses_pct,
        analysis.bankroll_after_5_losses_pct,
        analysis.suggested_pct_of_24h_volume,
        analysis.days_to_resolution,
        analysis.long_time_to_resolution,
        analysis.sub_threshold,
        sensitivity_payload,
        invalidation_payload,
        analysis.low_signature_confidence,
        analysis.source_stale_warning,
        analysis.library_stale_warning,
        analysis.definition_drift_warning,
        analysis.low_mapping_confidence,
        analysis.low_liquidity,
        analysis.error,
        analysis.computed_at or datetime.now(tz=UTC),
        analysis.venue,
    ]
    if existing is None:
        conn.execute(
            "INSERT INTO analyses ("
            "analysis_id, cycle_id, comparison_id, class_id, condition_id, "
            "bankroll_config_id, model_probability, market_probability, "
            "kelly_unclamped, kelly_negative, kelly_clamped_by_max_cap, "
            "kelly_clamped_by_liquidity, suggested_fraction, "
            "suggested_dollar_size, ev_per_dollar, "
            "bankroll_after_1_loss_pct, bankroll_after_3_losses_pct, "
            "bankroll_after_5_losses_pct, suggested_pct_of_24h_volume, "
            "days_to_resolution, long_time_to_resolution, sub_threshold, "
            "sensitivity_analysis, invalidation_criteria, "
            "low_signature_confidence, source_stale_warning, "
            "library_stale_warning, definition_drift_warning, "
            "low_mapping_confidence, low_liquidity, error, computed_at, venue"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [analysis.analysis_id, *params],
        )
    else:
        conn.execute(
            "UPDATE analyses SET "
            "cycle_id = ?, comparison_id = ?, class_id = ?, condition_id = ?, "
            "bankroll_config_id = ?, model_probability = ?, market_probability = ?, "
            "kelly_unclamped = ?, kelly_negative = ?, kelly_clamped_by_max_cap = ?, "
            "kelly_clamped_by_liquidity = ?, suggested_fraction = ?, "
            "suggested_dollar_size = ?, ev_per_dollar = ?, "
            "bankroll_after_1_loss_pct = ?, bankroll_after_3_losses_pct = ?, "
            "bankroll_after_5_losses_pct = ?, suggested_pct_of_24h_volume = ?, "
            "days_to_resolution = ?, long_time_to_resolution = ?, "
            "sub_threshold = ?, sensitivity_analysis = ?, "
            "invalidation_criteria = ?, low_signature_confidence = ?, "
            "source_stale_warning = ?, library_stale_warning = ?, "
            "definition_drift_warning = ?, low_mapping_confidence = ?, "
            "low_liquidity = ?, error = ?, computed_at = ?, venue = ? "
            "WHERE analysis_id = ?",
            [*params, analysis.analysis_id],
        )


def persist_analysis_trace(conn: duckdb.DuckDBPyConnection, trace: AnalysisTrace) -> None:
    """Idempotent upsert of one analysis_traces row."""
    payload = json.dumps(dict(trace.structured_dict))
    existing = conn.execute(
        "SELECT 1 FROM analysis_traces WHERE analysis_id = ?", [trace.analysis_id]
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO analysis_traces (analysis_id, rendered_text, structured_dict) "
            "VALUES (?, ?, ?)",
            [trace.analysis_id, trace.rendered_text, payload],
        )
    else:
        conn.execute(
            "UPDATE analysis_traces SET rendered_text = ?, structured_dict = ? "
            "WHERE analysis_id = ?",
            [trace.rendered_text, payload, trace.analysis_id],
        )


def query_analyses(
    conn: duckdb.DuckDBPyConnection,
    *,
    cycle_id: str | None = None,
    comparison_id: str | None = None,
    class_id: str | None = None,
    venue: str | None = None,
    since: datetime | None = None,
) -> tuple[Analysis, ...]:
    """Return analyses matching the filter."""
    base_query = (
        "SELECT analysis_id, cycle_id, comparison_id, class_id, condition_id, "
        "bankroll_config_id, model_probability, market_probability, "
        "kelly_unclamped, kelly_negative, kelly_clamped_by_max_cap, "
        "kelly_clamped_by_liquidity, suggested_fraction, suggested_dollar_size, "
        "ev_per_dollar, bankroll_after_1_loss_pct, bankroll_after_3_losses_pct, "
        "bankroll_after_5_losses_pct, suggested_pct_of_24h_volume, "
        "days_to_resolution, long_time_to_resolution, sub_threshold, "
        "sensitivity_analysis, invalidation_criteria, low_signature_confidence, "
        "source_stale_warning, library_stale_warning, definition_drift_warning, "
        "low_mapping_confidence, low_liquidity, error, computed_at, venue "
        "FROM analyses"
    )
    conditions: list[str] = []
    params: list[Any] = []
    if cycle_id is not None:
        conditions.append("cycle_id = ?")
        params.append(cycle_id)
    if comparison_id is not None:
        conditions.append("comparison_id = ?")
        params.append(comparison_id)
    if class_id is not None:
        conditions.append("class_id = ?")
        params.append(class_id)
    if venue is not None:
        conditions.append("venue = ?")
        params.append(venue)
    if since is not None:
        conditions.append("computed_at >= ?")
        params.append(since)
    if conditions:
        base_query += " WHERE " + " AND ".join(conditions)
    base_query += " ORDER BY computed_at DESC, class_id"
    rows = conn.execute(base_query, params).fetchall()
    return tuple(_analysis_from_row(r) for r in rows)


def get_analysis(conn: duckdb.DuckDBPyConnection, *, analysis_id: str) -> Analysis | None:
    rows = conn.execute(
        "SELECT analysis_id, cycle_id, comparison_id, class_id, condition_id, "
        "bankroll_config_id, model_probability, market_probability, "
        "kelly_unclamped, kelly_negative, kelly_clamped_by_max_cap, "
        "kelly_clamped_by_liquidity, suggested_fraction, suggested_dollar_size, "
        "ev_per_dollar, bankroll_after_1_loss_pct, bankroll_after_3_losses_pct, "
        "bankroll_after_5_losses_pct, suggested_pct_of_24h_volume, "
        "days_to_resolution, long_time_to_resolution, sub_threshold, "
        "sensitivity_analysis, invalidation_criteria, low_signature_confidence, "
        "source_stale_warning, library_stale_warning, definition_drift_warning, "
        "low_mapping_confidence, low_liquidity, error, computed_at, venue "
        "FROM analyses WHERE analysis_id = ?",
        [analysis_id],
    ).fetchone()
    return _analysis_from_row(rows) if rows is not None else None


def get_analysis_trace(
    conn: duckdb.DuckDBPyConnection, *, analysis_id: str
) -> AnalysisTrace | None:
    row = conn.execute(
        "SELECT rendered_text, structured_dict FROM analysis_traces WHERE analysis_id = ?",
        [analysis_id],
    ).fetchone()
    if row is None:
        return None
    structured: dict[str, Any] = {}
    if isinstance(row[1], str) and row[1]:
        try:
            decoded = json.loads(row[1])
            if isinstance(decoded, dict):
                structured = decoded
        except json.JSONDecodeError:
            structured = {}
    return AnalysisTrace(
        analysis_id=analysis_id,
        rendered_text=str(row[0]),
        structured_dict=structured,
    )


# -- watch_states -----------------------------------------------------------


def append_watch_state(
    conn: duckdb.DuckDBPyConnection,
    *,
    analysis_id: str,
    state: WatchStateValue,
    notes: str | None = None,
    set_by: SetBy = "operator",
    when: datetime | None = None,
) -> WatchState:
    """Append a new watch_states row. Latest by ``set_at`` wins."""
    state_id = str(uuid.uuid4())
    ts = when or datetime.now(tz=UTC)
    conn.execute(
        "INSERT INTO watch_states (state_id, analysis_id, state, notes, set_at, set_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [state_id, analysis_id, state, notes, ts, set_by],
    )
    return WatchState(
        state_id=state_id,
        analysis_id=analysis_id,
        state=state,
        notes=notes,
        set_at=ts,
        set_by=set_by,
    )


def latest_watch_state(conn: duckdb.DuckDBPyConnection, *, analysis_id: str) -> WatchState | None:
    """Return the most recent state for an analysis."""
    row = conn.execute(
        "SELECT state_id, analysis_id, state, notes, set_at, set_by "
        "FROM watch_states WHERE analysis_id = ? ORDER BY set_at DESC LIMIT 1",
        [analysis_id],
    ).fetchone()
    if row is None:
        return None
    return WatchState(
        state_id=str(row[0]),
        analysis_id=str(row[1]),
        state=row[2],
        notes=(str(row[3]) if row[3] is not None else None),
        set_at=row[4],
        set_by=row[5],
    )


def list_by_state(
    conn: duckdb.DuckDBPyConnection, *, state: WatchStateValue
) -> tuple[WatchState, ...]:
    """Return analyses whose latest watch state matches ``state``."""
    # Window function gives us the latest row per analysis_id.
    rows = conn.execute(
        "WITH ranked AS ("
        "  SELECT state_id, analysis_id, state, notes, set_at, set_by, "
        "    ROW_NUMBER() OVER (PARTITION BY analysis_id ORDER BY set_at DESC) AS rn "
        "  FROM watch_states"
        ") "
        "SELECT state_id, analysis_id, state, notes, set_at, set_by "
        "FROM ranked WHERE rn = 1 AND state = ? ORDER BY set_at DESC",
        [state],
    ).fetchall()
    out: list[WatchState] = []
    for r in rows:
        out.append(
            WatchState(
                state_id=str(r[0]),
                analysis_id=str(r[1]),
                state=r[2],
                notes=(str(r[3]) if r[3] is not None else None),
                set_at=r[4],
                set_by=r[5],
            )
        )
    return tuple(out)


# -- internals --------------------------------------------------------------


def _analysis_from_row(row: tuple[Any, ...]) -> Analysis:
    sensitivity: dict[str, Any] | None = None
    if isinstance(row[22], str) and row[22]:
        try:
            decoded = json.loads(row[22])
            if isinstance(decoded, dict):
                sensitivity = decoded
        except json.JSONDecodeError:
            sensitivity = None
    invalidation: tuple[Mapping[str, Any], ...] = ()
    if isinstance(row[23], str) and row[23]:
        try:
            decoded_list = json.loads(row[23])
            if isinstance(decoded_list, list):
                invalidation = tuple(
                    {str(k): v for k, v in entry.items()}
                    for entry in decoded_list
                    if isinstance(entry, dict)
                )
        except json.JSONDecodeError:
            invalidation = ()
    return Analysis(
        analysis_id=str(row[0]),
        cycle_id=str(row[1]),
        comparison_id=str(row[2]),
        class_id=str(row[3]),
        condition_id=str(row[4]),
        bankroll_config_id=str(row[5]),
        model_probability=float(row[6]),
        market_probability=(float(row[7]) if row[7] is not None else None),
        kelly_unclamped=float(row[8]),
        kelly_negative=bool(row[9]),
        kelly_clamped_by_max_cap=bool(row[10]),
        kelly_clamped_by_liquidity=bool(row[11]),
        suggested_fraction=float(row[12]),
        suggested_dollar_size=float(row[13]),
        ev_per_dollar=(float(row[14]) if row[14] is not None else None),
        bankroll_after_1_loss_pct=float(row[15]),
        bankroll_after_3_losses_pct=float(row[16]),
        bankroll_after_5_losses_pct=float(row[17]),
        suggested_pct_of_24h_volume=(float(row[18]) if row[18] is not None else None),
        days_to_resolution=(int(row[19]) if row[19] is not None else None),
        long_time_to_resolution=bool(row[20]),
        sub_threshold=bool(row[21]),
        sensitivity_analysis=sensitivity,
        invalidation_criteria=invalidation,
        low_signature_confidence=bool(row[24]),
        source_stale_warning=bool(row[25]),
        library_stale_warning=bool(row[26]),
        definition_drift_warning=bool(row[27]),
        low_mapping_confidence=bool(row[28]),
        low_liquidity=bool(row[29]),
        error=(str(row[30]) if row[30] is not None else None),
        computed_at=row[31],
        venue=row[32] if len(row) > 32 and row[32] is not None else "polymarket",
    )


__all__ = [
    "append_watch_state",
    "complete_cycle",
    "get_analysis",
    "get_analysis_trace",
    "latest_bankroll_config",
    "latest_watch_state",
    "list_by_state",
    "persist_analysis",
    "persist_analysis_trace",
    "query_analyses",
    "query_cycle",
    "write_bankroll_config",
    "write_cycle",
]


_RESERVED: tuple[Any, ...] = (Iterable,)
