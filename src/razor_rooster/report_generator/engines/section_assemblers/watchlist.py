"""Watchlist assembler (T-RG-025; design §3.5).

Returns a content dict shaped like::

    {
      "type": "watchlist",
      "candidates": [
        {
          "scan_id": "...",
          "class_id": "...",
          "class_title": "...",
          "domain_sector": "...",
          "posterior": float,
          "base_rate": float,
          "log_odds_shift": float,
          "candidate_direction": str | None,
          "reason": "no_active_mapping" | "all_low_confidence" |
                    "all_stale_market_price",
          "suggestion": str,
        },
        ...
      ],
    }

Lists ``signal_scanner`` candidates that did not produce a
``mispricing_detector`` comparison this cycle for one of three
reasons documented in REQ-RG-SEC-006:

1. No active class_market_mapping for the class.
2. All mappings flagged ``low_mapping_confidence`` (no surfaced
   comparison shows confidence-weighted score).
3. All mapped markets had stale prices (the comparison row exists
   but with ``stale_market_price`` flag and unsurfaced).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import duckdb

logger = logging.getLogger(__name__)


def assemble(
    conn: duckdb.DuckDBPyConnection,
    *,
    since_ts: datetime,
    until_ts: datetime,
    verbosity: str = "full",
) -> dict[str, Any]:
    """Assemble the watchlist content.

    Verbosity affects whether the scan reasoning text is included:

    - "full" includes a scan summary string.
    - "compact" omits it.
    """
    candidate_rows = conn.execute(
        "SELECT scan_id, class_id, posterior, base_rate, log_odds_shift, "
        "candidate_direction "
        "FROM scan_records WHERE is_candidate = TRUE "
        "AND scan_started_at >= ? AND scan_started_at <= ?",
        [since_ts, until_ts],
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in candidate_rows:
        scan_id = str(row[0])
        class_id = str(row[1])
        reason = _classify_non_surfacing_reason(
            conn,
            class_id=class_id,
            since_ts=since_ts,
            until_ts=until_ts,
        )
        if reason is None:
            # The class produced a surfaced comparison; not watchlist.
            continue
        cls = _query_class(conn, class_id=class_id)
        candidates.append(
            {
                "scan_id": scan_id,
                "class_id": class_id,
                "class_title": cls.get("title", class_id) if cls else class_id,
                "domain_sector": cls.get("domain_sector", "unknown") if cls else "unknown",
                "posterior": float(row[2]),
                "base_rate": float(row[3]),
                "log_odds_shift": float(row[4]),
                "candidate_direction": (str(row[5]) if row[5] is not None else None),
                "reason": reason,
                "suggestion": _suggestion_for_reason(reason),
                "verbosity": verbosity,
            }
        )
    return {"type": "watchlist", "candidates": candidates}


# -- internals --------------------------------------------------------------


def _classify_non_surfacing_reason(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str,
    since_ts: datetime,
    until_ts: datetime,
) -> str | None:
    """Return why the class did not surface, or None if it did."""
    # 1. Look for surfaced comparison(s).
    surfaced = conn.execute(
        "SELECT 1 FROM comparisons WHERE class_id = ? AND surfaced = TRUE "
        "AND computed_at >= ? AND computed_at <= ? LIMIT 1",
        [class_id, since_ts, until_ts],
    ).fetchone()
    if surfaced is not None:
        return None
    # 2. Look for any (unsurfaced) comparison rows in the window to know
    #    if the class made it through the comparator at all.
    any_compare = conn.execute(
        "SELECT comparison_id, low_mapping_confidence, stale_market_price "
        "FROM comparisons WHERE class_id = ? "
        "AND computed_at >= ? AND computed_at <= ?",
        [class_id, since_ts, until_ts],
    ).fetchall()
    if not any_compare:
        # No comparison row at all means no active mapping.
        mapping = conn.execute(
            "SELECT 1 FROM class_market_mappings WHERE class_id = ? AND removed_at IS NULL LIMIT 1",
            [class_id],
        ).fetchone()
        if mapping is None:
            return "no_active_mapping"
        # Mapping exists but no comparison this window — treat as
        # mapping not yet exercised this cycle.
        return "no_active_mapping"
    # Inspect the unsurfaced comparison flags.
    if all(bool(r[1]) for r in any_compare):
        return "all_low_confidence"
    if all(bool(r[2]) for r in any_compare):
        return "all_stale_market_price"
    # Other suppression reasons exist (e.g. sub-edge); we treat those
    # as not in the watchlist scope per REQ-RG-SEC-006.
    return None


def _query_class(conn: duckdb.DuckDBPyConnection, *, class_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT title, domain_sector FROM pl_event_classes WHERE class_id = ?",
        [class_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "title": str(row[0]),
        "domain_sector": str(row[1]),
    }


def _suggestion_for_reason(reason: str) -> str:
    return {
        "no_active_mapping": (
            "Consider mapping this class to a Polymarket or Kalshi market "
            "if you find a corresponding contract."
        ),
        "all_low_confidence": (
            "Existing mappings carry low_mapping_confidence; consider "
            "reviewing the mapping or marking it inactive."
        ),
        "all_stale_market_price": (
            "Mapped markets have stale price snapshots; consider "
            "re-running the venue connector before the next cycle."
        ),
    }.get(reason, "")


__all__ = ["assemble"]
