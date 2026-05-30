"""System health assembler (T-RG-021; design §3.5).

Returns a content dict shaped like::

    {
      "type": "system_health",
      "stale_sources": [
        {"source_id": "noaa", "days_stale": 4, "last_successful_fetch": ...},
        ...
      ],
      "errored_subsystems": [
        {"subsystem": "polymarket_connector", "cycle_id": "...",
         "error_count": 1},
        ...
      ],
      "suppressed_breakdown": {"low_mapping_confidence": 8, ...},
      "library_age_days": int | None,
    }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import duckdb

from razor_rooster.data_ingest.persistence.provenance import query_freshness

logger = logging.getLogger(__name__)


_CYCLE_TABLES: tuple[tuple[str, str, str], ...] = (
    # (subsystem_name, table, cycle_id_column)
    ("data_ingest", "cycle_log", "cycle_id"),
    ("polymarket_connector", "cycle_log", "cycle_id"),
    ("signal_scanner", "scan_summaries", "scan_id"),
    ("mispricing_detector", "comparison_cycles", "cycle_id"),
    ("position_engine", "analysis_cycles", "cycle_id"),
    ("monitor", "monitor_cycles", "cycle_id"),
)


def assemble(
    conn: duckdb.DuckDBPyConnection,
    *,
    since_ts: datetime,
    until_ts: datetime,
    library_age_days: int | None = None,
) -> dict[str, Any]:
    stale_sources = _query_stale_sources(conn)
    errored_subsystems = _query_errored_subsystems(conn, since_ts=since_ts, until_ts=until_ts)
    suppressed = _query_suppressed_breakdown(conn, since_ts=since_ts, until_ts=until_ts)
    return {
        "type": "system_health",
        "stale_sources": stale_sources,
        "errored_subsystems": errored_subsystems,
        "suppressed_breakdown": suppressed,
        "library_age_days": library_age_days,
    }


# -- internals --------------------------------------------------------------


def _query_stale_sources(
    conn: duckdb.DuckDBPyConnection,
) -> list[dict[str, Any]]:
    rows = query_freshness(conn)
    out: list[dict[str, Any]] = []
    for r in rows:
        if not r.is_stale:
            continue
        days_stale: int | None = None
        if r.seconds_since_fetch is not None:
            days_stale = int(r.seconds_since_fetch // 86400)
        out.append(
            {
                "source_id": r.source_id,
                "days_stale": days_stale,
                "last_successful_fetch": r.last_successful_fetch,
            }
        )
    return out


def _query_errored_subsystems(
    conn: duckdb.DuckDBPyConnection,
    *,
    since_ts: datetime,
    until_ts: datetime,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for subsystem, table, cycle_col in _CYCLE_TABLES:
        try:
            errors = _count_errors_for_subsystem(
                conn,
                subsystem=subsystem,
                table=table,
                cycle_col=cycle_col,
                since_ts=since_ts,
                until_ts=until_ts,
            )
        except duckdb.CatalogException:
            # Table absent on this DB; skip silently.
            continue
        except Exception:
            logger.exception("system_health: error counting errors in %s.%s", subsystem, table)
            continue
        if errors:
            out.extend(errors)
    return out


def _count_errors_for_subsystem(
    conn: duckdb.DuckDBPyConnection,
    *,
    subsystem: str,
    table: str,
    cycle_col: str,
    since_ts: datetime,
    until_ts: datetime,
) -> list[dict[str, Any]]:
    # The schema across subsystems is heterogeneous: data_ingest
    # cycle_log has an ``errors`` JSON column; the others use
    # ``error_summary`` JSON. We probe both and skip silently when
    # neither exists.
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    error_col = (
        "error_summary"
        if "error_summary" in columns
        else ("errors" if "errors" in columns else None)
    )
    if error_col is None:
        return []
    started_col = (
        "started_at"
        if "started_at" in columns
        else ("scan_started_at" if "scan_started_at" in columns else None)
    )
    if started_col is None:
        return []
    rows = conn.execute(
        f"SELECT {cycle_col}, {error_col} FROM {table} "
        f"WHERE {started_col} >= ? AND {started_col} <= ? "
        f"AND {error_col} IS NOT NULL",
        [since_ts, until_ts],
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        cycle_id = str(row[0])
        raw = row[1]
        decoded: Any = None
        if isinstance(raw, str) and raw:
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                decoded = None
        if not decoded:
            continue
        # Either {'errors': [...]} or {'<source>': '<msg>'}
        if isinstance(decoded, dict) and decoded.get("errors"):
            errs = decoded["errors"]
            if isinstance(errs, list) and errs:
                out.append(
                    {
                        "subsystem": subsystem,
                        "cycle_id": cycle_id,
                        "error_count": len(errs),
                    }
                )
                continue
        if isinstance(decoded, dict):
            count = sum(1 for v in decoded.values() if v)
            if count:
                out.append(
                    {
                        "subsystem": subsystem,
                        "cycle_id": cycle_id,
                        "error_count": count,
                    }
                )
    return out


def _query_suppressed_breakdown(
    conn: duckdb.DuckDBPyConnection,
    *,
    since_ts: datetime,
    until_ts: datetime,
) -> dict[str, int]:
    """Aggregate suppression_reasons across comparisons in the window."""
    breakdown: dict[str, int] = {}
    try:
        rows = conn.execute(
            "SELECT suppression_reasons FROM comparisons "
            "WHERE surfaced = FALSE AND computed_at >= ? AND computed_at <= ? "
            "AND suppression_reasons IS NOT NULL",
            [since_ts, until_ts],
        ).fetchall()
    except duckdb.CatalogException:
        return breakdown
    for (raw,) in rows:
        if raw is None:
            continue
        if isinstance(raw, str) and raw:
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                continue
        else:
            decoded = raw
        if isinstance(decoded, list):
            for reason in decoded:
                key = str(reason)
                breakdown[key] = breakdown.get(key, 0) + 1
    return breakdown


__all__ = ["assemble"]
