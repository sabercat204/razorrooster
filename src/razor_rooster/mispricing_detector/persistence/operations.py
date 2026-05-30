"""Mispricing-detector persistence helpers (T-MD-011; design §3.3).

Typed write/read helpers against the six namespaced tables. Callers
pass in a DuckDB connection; the helpers do not acquire connections
from a store. Each operator-curated mapping has a stable
``mapping_id``; auto-derived mappings are computed fresh per cycle
and not persisted.

The state KV table is used by the linkage pass (OQ-MD-005 resolution)
to track ``last_linkage_ts`` across runs.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import duckdb

from razor_rooster.mispricing_detector.models import (
    ClassMarketMapping,
    Comparison,
    ComparisonCycle,
    ComparisonResolution,
    ComparisonTrace,
    MappingConfidence,
    MappingType,
    Polarity,
    Venue,
)

logger = logging.getLogger(__name__)


# -- mapping operations -----------------------------------------------------


class MappingExistsError(RuntimeError):
    """Raised when registering a duplicate active mapping."""


def register_mapping(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str,
    condition_id: str,
    mapping_type: MappingType,
    mapping_confidence: MappingConfidence = "exact",
    polarity: Polarity = "aligned",
    mapped_by: str = "operator",
    notes: str | None = None,
    when: datetime | None = None,
    venue: Venue = "polymarket",
) -> ClassMarketMapping:
    """Insert a new mapping. Raises :class:`MappingExistsError` on collision.

    The active-mapping uniqueness invariant post-T-MD-101 is
    ``(class_id, venue, condition_id, polarity)`` — two venues can host
    the same logical class against the same market identifier
    coincidentally without colliding. ``venue`` defaults to
    ``'polymarket'`` for backward compatibility with pre-Kalshi callers.
    """
    ts = when or datetime.now(tz=UTC)
    existing = conn.execute(
        "SELECT mapping_id FROM class_market_mappings "
        "WHERE class_id = ? AND venue = ? AND condition_id = ? AND polarity = ? "
        "  AND removed_at IS NULL",
        [class_id, venue, condition_id, polarity],
    ).fetchone()
    if existing is not None:
        raise MappingExistsError(
            f"active mapping already exists for class_id={class_id!r}, "
            f"venue={venue!r}, condition_id={condition_id!r}, "
            f"polarity={polarity!r}"
        )
    mapping_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO class_market_mappings ("
        "mapping_id, class_id, condition_id, mapping_type, mapping_confidence, "
        "polarity, mapped_by, mapped_at, removed_at, notes, venue"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
        [
            mapping_id,
            class_id,
            condition_id,
            mapping_type,
            mapping_confidence,
            polarity,
            mapped_by,
            ts,
            notes,
            venue,
        ],
    )
    return ClassMarketMapping(
        mapping_id=mapping_id,
        class_id=class_id,
        condition_id=condition_id,
        mapping_type=mapping_type,
        mapping_confidence=mapping_confidence,
        polarity=polarity,
        mapped_by=mapped_by,  # type: ignore[arg-type]
        mapped_at=ts,
        notes=notes,
        venue=venue,
    )


def remove_mapping(
    conn: duckdb.DuckDBPyConnection,
    *,
    mapping_id: str,
    when: datetime | None = None,
) -> bool:
    """Soft-delete a mapping. Returns True when a row was updated."""
    ts = when or datetime.now(tz=UTC)
    cur = conn.execute(
        "UPDATE class_market_mappings SET removed_at = ? "
        "WHERE mapping_id = ? AND removed_at IS NULL",
        [ts, mapping_id],
    )
    # DuckDB doesn't support ROW_COUNT() in all builds; verify via select.
    row = conn.execute(
        "SELECT removed_at FROM class_market_mappings WHERE mapping_id = ?",
        [mapping_id],
    ).fetchone()
    _ = cur
    return row is not None and row[0] is not None


def query_mappings(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str | None = None,
    condition_id: str | None = None,
    confidence: MappingConfidence | None = None,
    venue: Venue | None = None,
    include_removed: bool = False,
) -> tuple[ClassMarketMapping, ...]:
    """Return mappings matching the filter."""
    query = (
        "SELECT mapping_id, class_id, condition_id, mapping_type, mapping_confidence, "
        "polarity, mapped_by, mapped_at, removed_at, notes, venue "
        "FROM class_market_mappings"
    )
    conditions: list[str] = []
    params: list[Any] = []
    if not include_removed:
        conditions.append("removed_at IS NULL")
    if class_id is not None:
        conditions.append("class_id = ?")
        params.append(class_id)
    if condition_id is not None:
        conditions.append("condition_id = ?")
        params.append(condition_id)
    if confidence is not None:
        conditions.append("mapping_confidence = ?")
        params.append(confidence)
    if venue is not None:
        conditions.append("venue = ?")
        params.append(venue)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY class_id, mapped_at DESC"
    rows = conn.execute(query, params).fetchall()
    return tuple(_mapping_from_row(r) for r in rows)


def get_mapping(conn: duckdb.DuckDBPyConnection, *, mapping_id: str) -> ClassMarketMapping | None:
    row = conn.execute(
        "SELECT mapping_id, class_id, condition_id, mapping_type, mapping_confidence, "
        "polarity, mapped_by, mapped_at, removed_at, notes, venue "
        "FROM class_market_mappings WHERE mapping_id = ?",
        [mapping_id],
    ).fetchone()
    return _mapping_from_row(row) if row is not None else None


# -- comparison cycle / records ---------------------------------------------


def write_cycle(conn: duckdb.DuckDBPyConnection, cycle: ComparisonCycle) -> None:
    """Insert or update a ``comparison_cycles`` row."""
    breakdown_payload = json.dumps(dict(cycle.suppressed_breakdown))
    error_payload = json.dumps(dict(cycle.error_summary)) if cycle.error_summary else None
    existing = conn.execute(
        "SELECT 1 FROM comparison_cycles WHERE cycle_id = ?", [cycle.cycle_id]
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO comparison_cycles ("
            "cycle_id, started_at, completed_at, comparisons_total, surfaced_count, "
            "suppressed_breakdown, library_version_at_cycle, scan_id_consumed, "
            "error_summary"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                cycle.cycle_id,
                cycle.started_at,
                cycle.completed_at,
                cycle.comparisons_total,
                cycle.surfaced_count,
                breakdown_payload,
                cycle.library_version_at_cycle,
                cycle.scan_id_consumed,
                error_payload,
            ],
        )
    else:
        conn.execute(
            "UPDATE comparison_cycles SET "
            "started_at = ?, completed_at = ?, comparisons_total = ?, "
            "surfaced_count = ?, suppressed_breakdown = ?, "
            "library_version_at_cycle = ?, scan_id_consumed = ?, "
            "error_summary = ? "
            "WHERE cycle_id = ?",
            [
                cycle.started_at,
                cycle.completed_at,
                cycle.comparisons_total,
                cycle.surfaced_count,
                breakdown_payload,
                cycle.library_version_at_cycle,
                cycle.scan_id_consumed,
                error_payload,
                cycle.cycle_id,
            ],
        )


def complete_cycle(
    conn: duckdb.DuckDBPyConnection,
    *,
    cycle_id: str,
    completed_at: datetime,
    comparisons_total: int,
    surfaced_count: int,
    suppressed_breakdown: dict[str, int],
    error_summary: dict[str, Any] | None = None,
) -> None:
    """Stamp a cycle as complete with aggregate counts."""
    breakdown_payload = json.dumps(suppressed_breakdown)
    error_payload = json.dumps(error_summary) if error_summary else None
    conn.execute(
        "UPDATE comparison_cycles SET completed_at = ?, "
        "comparisons_total = ?, surfaced_count = ?, "
        "suppressed_breakdown = ?, "
        "error_summary = COALESCE(?, error_summary) "
        "WHERE cycle_id = ?",
        [
            completed_at,
            comparisons_total,
            surfaced_count,
            breakdown_payload,
            error_payload,
            cycle_id,
        ],
    )


def persist_comparison(conn: duckdb.DuckDBPyConnection, comparison: Comparison) -> None:
    """Idempotent upsert of one ``comparisons`` row."""
    suppression_payload = (
        json.dumps(list(comparison.suppression_reasons)) if comparison.suppression_reasons else None
    )
    params = [
        comparison.cycle_id,
        comparison.mapping_id,
        comparison.class_id,
        comparison.condition_id,
        comparison.outcome_token_id,
        comparison.polarity,
        comparison.scan_id,
        comparison.model_probability,
        comparison.model_ci_lower,
        comparison.model_ci_upper,
        comparison.market_probability,
        comparison.market_best_bid,
        comparison.market_best_ask,
        comparison.market_last_trade_price,
        comparison.market_volume_24h,
        comparison.market_spread_bps,
        comparison.market_snapshot_ts,
        comparison.delta,
        comparison.log_odds_delta,
        comparison.ci_overlap,
        comparison.expected_value,
        comparison.confidence_weighted_score,
        comparison.surfaced,
        suppression_payload,
        comparison.low_signature_confidence,
        comparison.source_stale_warning,
        comparison.library_stale_warning,
        comparison.definition_drift_warning,
        comparison.stale_market_price,
        comparison.no_market_price,
        comparison.degenerate_orderbook,
        comparison.low_liquidity,
        comparison.low_mapping_confidence,
        comparison.error,
        comparison.computed_at or datetime.now(tz=UTC),
        comparison.venue,
    ]
    existing = conn.execute(
        "SELECT 1 FROM comparisons WHERE comparison_id = ?", [comparison.comparison_id]
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO comparisons ("
            "comparison_id, cycle_id, mapping_id, class_id, condition_id, "
            "outcome_token_id, polarity, scan_id, "
            "model_probability, model_ci_lower, model_ci_upper, "
            "market_probability, market_best_bid, market_best_ask, "
            "market_last_trade_price, market_volume_24h, market_spread_bps, "
            "market_snapshot_ts, delta, log_odds_delta, ci_overlap, "
            "expected_value, confidence_weighted_score, surfaced, "
            "suppression_reasons, low_signature_confidence, "
            "source_stale_warning, library_stale_warning, "
            "definition_drift_warning, stale_market_price, no_market_price, "
            "degenerate_orderbook, low_liquidity, low_mapping_confidence, "
            "error, computed_at, venue"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [comparison.comparison_id, *params],
        )
    else:
        conn.execute(
            "UPDATE comparisons SET "
            "cycle_id = ?, mapping_id = ?, class_id = ?, condition_id = ?, "
            "outcome_token_id = ?, polarity = ?, scan_id = ?, "
            "model_probability = ?, model_ci_lower = ?, model_ci_upper = ?, "
            "market_probability = ?, market_best_bid = ?, market_best_ask = ?, "
            "market_last_trade_price = ?, market_volume_24h = ?, "
            "market_spread_bps = ?, market_snapshot_ts = ?, "
            "delta = ?, log_odds_delta = ?, ci_overlap = ?, "
            "expected_value = ?, confidence_weighted_score = ?, surfaced = ?, "
            "suppression_reasons = ?, low_signature_confidence = ?, "
            "source_stale_warning = ?, library_stale_warning = ?, "
            "definition_drift_warning = ?, stale_market_price = ?, "
            "no_market_price = ?, degenerate_orderbook = ?, low_liquidity = ?, "
            "low_mapping_confidence = ?, error = ?, computed_at = ?, "
            "venue = ? "
            "WHERE comparison_id = ?",
            [*params, comparison.comparison_id],
        )


def persist_trace(conn: duckdb.DuckDBPyConnection, trace: ComparisonTrace) -> None:
    """Idempotent upsert of one ``comparison_traces`` row."""
    payload = json.dumps(dict(trace.payload))
    existing = conn.execute(
        "SELECT 1 FROM comparison_traces WHERE comparison_id = ?", [trace.comparison_id]
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO comparison_traces (comparison_id, trace_json) VALUES (?, ?)",
            [trace.comparison_id, payload],
        )
    else:
        conn.execute(
            "UPDATE comparison_traces SET trace_json = ? WHERE comparison_id = ?",
            [payload, trace.comparison_id],
        )


def write_resolution_link(
    conn: duckdb.DuckDBPyConnection, resolution: ComparisonResolution
) -> None:
    """Idempotent upsert of one ``comparison_resolutions`` row."""
    existing = conn.execute(
        "SELECT 1 FROM comparison_resolutions WHERE comparison_id = ?",
        [resolution.comparison_id],
    ).fetchone()
    params = [
        resolution.condition_id,
        resolution.resolution_outcome,
        resolution.resolution_ts,
        resolution.model_probability_at_comparison,
        resolution.market_probability_at_comparison,
        resolution.polarity_at_comparison,
        resolution.outcome_observed,
        resolution.linked_at,
        resolution.venue,
    ]
    if existing is None:
        conn.execute(
            "INSERT INTO comparison_resolutions ("
            "comparison_id, condition_id, resolution_outcome, resolution_ts, "
            "model_probability_at_comparison, market_probability_at_comparison, "
            "polarity_at_comparison, outcome_observed, linked_at, venue"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [resolution.comparison_id, *params],
        )
    else:
        conn.execute(
            "UPDATE comparison_resolutions SET "
            "condition_id = ?, resolution_outcome = ?, resolution_ts = ?, "
            "model_probability_at_comparison = ?, market_probability_at_comparison = ?, "
            "polarity_at_comparison = ?, outcome_observed = ?, linked_at = ?, "
            "venue = ? "
            "WHERE comparison_id = ?",
            [*params, resolution.comparison_id],
        )


def query_comparisons(
    conn: duckdb.DuckDBPyConnection,
    *,
    cycle_id: str | None = None,
    surfaced_only: bool = False,
    since: datetime | None = None,
) -> tuple[Comparison, ...]:
    """Return comparisons matching the filter."""
    base_query = (
        "SELECT comparison_id, cycle_id, mapping_id, class_id, condition_id, "
        "outcome_token_id, polarity, scan_id, model_probability, model_ci_lower, "
        "model_ci_upper, market_probability, market_best_bid, market_best_ask, "
        "market_last_trade_price, market_volume_24h, market_spread_bps, "
        "market_snapshot_ts, delta, log_odds_delta, ci_overlap, expected_value, "
        "confidence_weighted_score, surfaced, suppression_reasons, "
        "low_signature_confidence, source_stale_warning, library_stale_warning, "
        "definition_drift_warning, stale_market_price, no_market_price, "
        "degenerate_orderbook, low_liquidity, low_mapping_confidence, "
        "error, computed_at, venue "
        "FROM comparisons"
    )
    conditions: list[str] = []
    params: list[Any] = []
    if cycle_id is not None:
        conditions.append("cycle_id = ?")
        params.append(cycle_id)
    if surfaced_only:
        conditions.append("surfaced = TRUE")
    if since is not None:
        conditions.append("computed_at >= ?")
        params.append(since)
    if conditions:
        base_query += " WHERE " + " AND ".join(conditions)
    base_query += " ORDER BY computed_at DESC, class_id"
    rows = conn.execute(base_query, params).fetchall()
    return tuple(_comparison_from_row(r) for r in rows)


def get_comparison(conn: duckdb.DuckDBPyConnection, *, comparison_id: str) -> Comparison | None:
    rows = conn.execute(
        "SELECT comparison_id, cycle_id, mapping_id, class_id, condition_id, "
        "outcome_token_id, polarity, scan_id, model_probability, model_ci_lower, "
        "model_ci_upper, market_probability, market_best_bid, market_best_ask, "
        "market_last_trade_price, market_volume_24h, market_spread_bps, "
        "market_snapshot_ts, delta, log_odds_delta, ci_overlap, expected_value, "
        "confidence_weighted_score, surfaced, suppression_reasons, "
        "low_signature_confidence, source_stale_warning, library_stale_warning, "
        "definition_drift_warning, stale_market_price, no_market_price, "
        "degenerate_orderbook, low_liquidity, low_mapping_confidence, "
        "error, computed_at, venue "
        "FROM comparisons WHERE comparison_id = ?",
        [comparison_id],
    ).fetchone()
    return _comparison_from_row(rows) if rows is not None else None


def query_trace(conn: duckdb.DuckDBPyConnection, *, comparison_id: str) -> ComparisonTrace | None:
    row = conn.execute(
        "SELECT trace_json FROM comparison_traces WHERE comparison_id = ?",
        [comparison_id],
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
    return ComparisonTrace(comparison_id=comparison_id, payload=payload)


def query_cycle(conn: duckdb.DuckDBPyConnection, *, cycle_id: str) -> ComparisonCycle | None:
    row = conn.execute(
        "SELECT cycle_id, started_at, completed_at, comparisons_total, "
        "surfaced_count, suppressed_breakdown, library_version_at_cycle, "
        "scan_id_consumed, error_summary "
        "FROM comparison_cycles WHERE cycle_id = ?",
        [cycle_id],
    ).fetchone()
    if row is None:
        return None
    breakdown: dict[str, int] = {}
    if isinstance(row[5], str) and row[5]:
        try:
            decoded = json.loads(row[5])
            if isinstance(decoded, dict):
                breakdown = {str(k): int(v) for k, v in decoded.items()}
        except (json.JSONDecodeError, ValueError, TypeError):
            breakdown = {}
    error_summary: dict[str, Any] | None = None
    if isinstance(row[8], str) and row[8]:
        try:
            decoded = json.loads(row[8])
            if isinstance(decoded, dict):
                error_summary = decoded
        except json.JSONDecodeError:
            error_summary = None
    return ComparisonCycle(
        cycle_id=str(row[0]),
        started_at=row[1],
        completed_at=row[2],
        comparisons_total=int(row[3]),
        surfaced_count=int(row[4]),
        suppressed_breakdown=breakdown,
        library_version_at_cycle=int(row[6]),
        scan_id_consumed=str(row[7]),
        error_summary=error_summary,
    )


def query_existing_resolution_links(
    conn: duckdb.DuckDBPyConnection, *, condition_id: str
) -> set[str]:
    """Return the set of comparison_ids already linked for a market."""
    rows = conn.execute(
        "SELECT comparison_id FROM comparison_resolutions WHERE condition_id = ?",
        [condition_id],
    ).fetchall()
    return {str(r[0]) for r in rows}


def query_comparisons_for_market(
    conn: duckdb.DuckDBPyConnection, *, condition_id: str
) -> tuple[Comparison, ...]:
    """Return all comparisons referencing a given market."""
    rows = conn.execute(
        "SELECT comparison_id, cycle_id, mapping_id, class_id, condition_id, "
        "outcome_token_id, polarity, scan_id, model_probability, model_ci_lower, "
        "model_ci_upper, market_probability, market_best_bid, market_best_ask, "
        "market_last_trade_price, market_volume_24h, market_spread_bps, "
        "market_snapshot_ts, delta, log_odds_delta, ci_overlap, expected_value, "
        "confidence_weighted_score, surfaced, suppression_reasons, "
        "low_signature_confidence, source_stale_warning, library_stale_warning, "
        "definition_drift_warning, stale_market_price, no_market_price, "
        "degenerate_orderbook, low_liquidity, low_mapping_confidence, "
        "error, computed_at, venue "
        "FROM comparisons WHERE condition_id = ? ORDER BY computed_at",
        [condition_id],
    ).fetchall()
    return tuple(_comparison_from_row(r) for r in rows)


# -- state KV ---------------------------------------------------------------


def state_get(conn: duckdb.DuckDBPyConnection, key: str) -> str | None:
    row = conn.execute(
        "SELECT state_value FROM mispricing_detector_state WHERE state_key = ?",
        [key],
    ).fetchone()
    return str(row[0]) if row is not None else None


def state_set(
    conn: duckdb.DuckDBPyConnection,
    key: str,
    value: str,
    *,
    when: datetime | None = None,
) -> None:
    ts = when or datetime.now(tz=UTC)
    existing = conn.execute(
        "SELECT 1 FROM mispricing_detector_state WHERE state_key = ?", [key]
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO mispricing_detector_state (state_key, state_value, updated_at) "
            "VALUES (?, ?, ?)",
            [key, value, ts],
        )
    else:
        conn.execute(
            "UPDATE mispricing_detector_state SET state_value = ?, updated_at = ? "
            "WHERE state_key = ?",
            [value, ts, key],
        )


# -- internals --------------------------------------------------------------


def _mapping_from_row(row: tuple[Any, ...]) -> ClassMarketMapping:
    venue_raw = row[10] if len(row) > 10 and row[10] is not None else "polymarket"
    venue: Venue = "kalshi" if venue_raw == "kalshi" else "polymarket"
    return ClassMarketMapping(
        mapping_id=str(row[0]),
        class_id=str(row[1]),
        condition_id=str(row[2]),
        mapping_type=row[3],
        mapping_confidence=row[4],
        polarity=row[5],
        mapped_by=row[6],
        mapped_at=row[7],
        removed_at=row[8],
        notes=str(row[9]) if row[9] is not None else None,
        venue=venue,
    )


def _comparison_from_row(row: tuple[Any, ...]) -> Comparison:
    suppression: tuple[str, ...] = ()
    if isinstance(row[24], str) and row[24]:
        try:
            decoded = json.loads(row[24])
            if isinstance(decoded, list):
                suppression = tuple(str(s) for s in decoded)
        except json.JSONDecodeError:
            suppression = ()
    venue_raw = row[36] if len(row) > 36 and row[36] is not None else "polymarket"
    venue: Venue = "kalshi" if venue_raw == "kalshi" else "polymarket"
    return Comparison(
        comparison_id=str(row[0]),
        cycle_id=str(row[1]),
        mapping_id=str(row[2]),
        class_id=str(row[3]),
        condition_id=str(row[4]),
        outcome_token_id=str(row[5]),
        polarity=row[6],
        scan_id=str(row[7]),
        model_probability=float(row[8]),
        model_ci_lower=float(row[9]),
        model_ci_upper=float(row[10]),
        market_probability=(float(row[11]) if row[11] is not None else None),
        market_best_bid=(float(row[12]) if row[12] is not None else None),
        market_best_ask=(float(row[13]) if row[13] is not None else None),
        market_last_trade_price=(float(row[14]) if row[14] is not None else None),
        market_volume_24h=(float(row[15]) if row[15] is not None else None),
        market_spread_bps=(int(row[16]) if row[16] is not None else None),
        market_snapshot_ts=row[17],
        delta=(float(row[18]) if row[18] is not None else None),
        log_odds_delta=(float(row[19]) if row[19] is not None else None),
        ci_overlap=bool(row[20]),
        expected_value=(float(row[21]) if row[21] is not None else None),
        confidence_weighted_score=(float(row[22]) if row[22] is not None else None),
        surfaced=bool(row[23]),
        suppression_reasons=suppression,
        low_signature_confidence=bool(row[25]),
        source_stale_warning=bool(row[26]),
        library_stale_warning=bool(row[27]),
        definition_drift_warning=bool(row[28]),
        stale_market_price=bool(row[29]),
        no_market_price=bool(row[30]),
        degenerate_orderbook=bool(row[31]),
        low_liquidity=bool(row[32]),
        low_mapping_confidence=bool(row[33]),
        error=(str(row[34]) if row[34] is not None else None),
        computed_at=row[35],
        venue=venue,
    )


__all__ = [
    "MappingExistsError",
    "complete_cycle",
    "get_comparison",
    "get_mapping",
    "persist_comparison",
    "persist_trace",
    "query_comparisons",
    "query_comparisons_for_market",
    "query_cycle",
    "query_existing_resolution_links",
    "query_mappings",
    "query_trace",
    "register_mapping",
    "remove_mapping",
    "state_get",
    "state_set",
    "write_cycle",
    "write_resolution_link",
]


# Reserved imports.
_RESERVED: tuple[Any, ...] = (Iterable,)
