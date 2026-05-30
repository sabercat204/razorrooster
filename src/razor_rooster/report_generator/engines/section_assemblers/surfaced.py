"""Surfaced comparisons assembler (T-RG-022; design §3.5).

Returns a content dict shaped like::

    {
      "type": "surfaced",
      "comparisons": [
        {
          "comparison_id": "...",
          "class_id": "...",
          "class_title": "...",
          "domain_sector": "...",
          "model_p": float,
          "model_ci": (float, float),
          "market_p": float | None,
          "market_spread_bps": int | None,
          "delta": float | None,
          "log_odds_delta": float | None,
          "ev": float | None,
          "score": float | None,
          "case_for_model": list[str],
          "case_for_market": list[str],
          "ambiguity_factors": list[str],
          "warnings": list[str],
          "scan_trace": dict | None,
          "comparison_trace": dict | None,
          "analysis": dict | None,
        },
        ...
      ],
    }
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import duckdb

logger = logging.getLogger(__name__)


def assemble(
    conn: duckdb.DuckDBPyConnection,
    *,
    since_ts: datetime,
    until_ts: datetime,
    single_venue_dominance_pct: float = 0.80,
    single_venue_dominance_pct_per_sector: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    per_sector_dominance = dict(single_venue_dominance_pct_per_sector or {})
    rows = conn.execute(
        "SELECT comparison_id, class_id, condition_id, scan_id, "
        "model_probability, model_ci_lower, model_ci_upper, "
        "market_probability, market_spread_bps, delta, log_odds_delta, "
        "expected_value, confidence_weighted_score, "
        "low_signature_confidence, source_stale_warning, "
        "library_stale_warning, low_mapping_confidence, low_liquidity, "
        "stale_market_price, no_market_price, computed_at, venue "
        "FROM comparisons "
        "WHERE surfaced = TRUE AND computed_at >= ? AND computed_at <= ? "
        "ORDER BY confidence_weighted_score DESC NULLS LAST, computed_at DESC",
        [since_ts, until_ts],
    ).fetchall()
    # Per-class venue-volume shares for the single-venue-dominance flag.
    # Computed once per assembler call, keyed by class_id.
    venue_shares = _compute_venue_volume_shares(conn, since_ts=since_ts, until_ts=until_ts)
    comparisons: list[dict[str, Any]] = []
    for row in rows:
        comparison_id = str(row[0])
        class_id = str(row[1])
        scan_id = str(row[3])
        cls = _query_class(conn, class_id=class_id)
        comparison_trace = _query_comparison_trace(conn, comparison_id=comparison_id)
        scan_trace = _query_scan_trace(conn, scan_id=scan_id, class_id=class_id)
        analysis = _query_analysis(
            conn, comparison_id=comparison_id, since_ts=since_ts, until_ts=until_ts
        )
        warnings = _flags_to_warnings(row)
        case_for_model: list[str] = []
        case_for_market: list[str] = []
        ambiguity_factors: list[str] = []
        if comparison_trace is not None:
            case_for_model = list(comparison_trace.get("case_for_model") or [])
            case_for_market = list(comparison_trace.get("case_for_market") or [])
            ambiguity_factors = list(comparison_trace.get("ambiguity_factors") or [])
            warnings = list(comparison_trace.get("warnings") or warnings)
        # Single-venue-dominance warning: when this class is mapped to
        # more than one venue but one venue holds > threshold share of
        # 24h volume, the operator should know the cross-venue
        # comparison may be uninformative. The threshold is
        # per-sector when an override is configured, otherwise
        # falls through to the global value.
        sector = cls.get("domain_sector", "unknown") if cls else "unknown"
        applicable_dominance = per_sector_dominance.get(sector, single_venue_dominance_pct)
        if (
            _has_single_venue_dominance(
                class_id=class_id,
                shares=venue_shares,
                threshold_pct=applicable_dominance,
            )
            and "single_venue_dominance" not in warnings
        ):
            warnings.append("single_venue_dominance")
        comparisons.append(
            {
                "comparison_id": comparison_id,
                "class_id": class_id,
                "class_title": cls.get("title", class_id) if cls else class_id,
                "domain_sector": cls.get("domain_sector", "unknown") if cls else "unknown",
                "condition_id": str(row[2]),
                "venue": str(row[21]) if row[21] is not None else "polymarket",
                "model_p": float(row[4]),
                "model_ci": (float(row[5]), float(row[6])),
                "market_p": (float(row[7]) if row[7] is not None else None),
                "market_spread_bps": (int(row[8]) if row[8] is not None else None),
                "delta": (float(row[9]) if row[9] is not None else None),
                "log_odds_delta": (float(row[10]) if row[10] is not None else None),
                "ev": (float(row[11]) if row[11] is not None else None),
                "score": (float(row[12]) if row[12] is not None else None),
                "case_for_model": case_for_model,
                "case_for_market": case_for_market,
                "ambiguity_factors": ambiguity_factors,
                "warnings": warnings,
                "venue_shares": venue_shares.get(class_id, {}),
                "scan_trace": scan_trace,
                "comparison_trace": comparison_trace,
                "analysis": analysis,
            }
        )
    return {"type": "surfaced", "comparisons": comparisons}


# -- internals --------------------------------------------------------------


def _query_class(conn: duckdb.DuckDBPyConnection, *, class_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT class_id, title, domain_sector FROM pl_event_classes WHERE class_id = ?",
        [class_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "class_id": str(row[0]),
        "title": str(row[1]),
        "domain_sector": str(row[2]),
    }


def _query_comparison_trace(
    conn: duckdb.DuckDBPyConnection, *, comparison_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT trace_json FROM comparison_traces WHERE comparison_id = ?",
        [comparison_id],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        decoded = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except json.JSONDecodeError:
        return None
    if isinstance(decoded, dict):
        return decoded
    return None


def _query_scan_trace(
    conn: duckdb.DuckDBPyConnection, *, scan_id: str, class_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT trace_json FROM scan_traces WHERE scan_id = ? AND class_id = ?",
        [scan_id, class_id],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        decoded = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except json.JSONDecodeError:
        return None
    if isinstance(decoded, dict):
        return decoded
    return None


def _query_analysis(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    since_ts: datetime,
    until_ts: datetime,
) -> dict[str, Any] | None:
    """Return the most recent position_engine analysis for a comparison.

    Returns None when no analysis exists yet for the surfaced
    comparison (e.g., the position_engine cycle has not run since the
    comparison surfaced).
    """
    try:
        row = conn.execute(
            "SELECT analysis_id, model_probability, market_probability, "
            "kelly_unclamped, suggested_fraction, suggested_dollar_size, "
            "ev_per_dollar, days_to_resolution, "
            "kelly_clamped_by_max_cap, kelly_clamped_by_liquidity, "
            "sub_threshold, computed_at "
            "FROM analyses WHERE comparison_id = ? "
            "AND computed_at >= ? AND computed_at <= ? "
            "ORDER BY computed_at DESC LIMIT 1",
            [comparison_id, since_ts, until_ts],
        ).fetchone()
    except duckdb.CatalogException:
        return None
    if row is None:
        return None
    rendered = _query_analysis_rendered_text(conn, analysis_id=str(row[0]))
    return {
        "analysis_id": str(row[0]),
        "model_probability": float(row[1]),
        "market_probability": (float(row[2]) if row[2] is not None else None),
        "kelly_unclamped": float(row[3]),
        "suggested_fraction": float(row[4]),
        "suggested_dollar_size": float(row[5]),
        "ev_per_dollar": (float(row[6]) if row[6] is not None else None),
        "days_to_resolution": (int(row[7]) if row[7] is not None else None),
        "kelly_clamped_by_max_cap": bool(row[8]),
        "kelly_clamped_by_liquidity": bool(row[9]),
        "sub_threshold": bool(row[10]),
        "computed_at": row[11],
        "rendered_text": rendered,
    }


def _query_analysis_rendered_text(
    conn: duckdb.DuckDBPyConnection, *, analysis_id: str
) -> str | None:
    try:
        row = conn.execute(
            "SELECT rendered_text FROM analysis_traces WHERE analysis_id = ?",
            [analysis_id],
        ).fetchone()
    except duckdb.CatalogException:
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _flags_to_warnings(row: tuple[Any, ...]) -> list[str]:
    """Translate boolean comparison flags into human-readable warnings."""
    warnings: list[str] = []
    if bool(row[13]):
        warnings.append("low_signature_confidence")
    if bool(row[14]):
        warnings.append("source_stale_warning")
    if bool(row[15]):
        warnings.append("library_stale_warning")
    if bool(row[16]):
        warnings.append("low_mapping_confidence")
    if bool(row[17]):
        warnings.append("low_liquidity")
    if bool(row[18]):
        warnings.append("stale_market_price")
    if bool(row[19]):
        warnings.append("no_market_price")
    return warnings


def _compute_venue_volume_shares(
    conn: duckdb.DuckDBPyConnection,
    *,
    since_ts: datetime,
    until_ts: datetime,
) -> dict[str, dict[str, float]]:
    """Per-class, per-venue share of total 24h volume.

    Returns ``{class_id: {venue: share_in_0_to_1, ...}, ...}``.

    For each class, reads the most recent comparison per (class, venue)
    pair within the cycle window — same dedup logic as the cross_venue
    section assembler — sums ``market_volume_24h`` across venues, and
    emits each venue's share. Classes with only one venue or with all-
    NULL volumes get a single-entry share dict (or empty), which the
    dominance check treats as not-dominant (no cross-venue surface to
    warn about).
    """
    rows = conn.execute(
        "WITH ranked AS ("
        " SELECT class_id, venue, market_volume_24h, "
        "        ROW_NUMBER() OVER ("
        "          PARTITION BY class_id, venue ORDER BY computed_at DESC"
        "        ) AS rn "
        " FROM comparisons "
        " WHERE computed_at >= ? AND computed_at <= ? "
        ") "
        "SELECT class_id, venue, market_volume_24h "
        "FROM ranked WHERE rn = 1",
        [since_ts, until_ts],
    ).fetchall()

    raw: dict[str, dict[str, float]] = {}
    for class_id, venue, volume in rows:
        if volume is None:
            continue
        cid = str(class_id)
        v = str(venue)
        raw.setdefault(cid, {})[v] = float(volume)

    shares: dict[str, dict[str, float]] = {}
    for cid, by_venue in raw.items():
        total = sum(by_venue.values())
        if total <= 0:
            continue
        shares[cid] = {v: vol / total for v, vol in by_venue.items()}
    return shares


def _has_single_venue_dominance(
    *,
    class_id: str,
    shares: dict[str, dict[str, float]],
    threshold_pct: float,
) -> bool:
    """Return True iff the class is mapped to >1 venue and one venue
    holds more than ``threshold_pct`` of the combined 24h volume.

    Single-venue classes can't be "dominant" — the warning is
    specifically about a cross-venue spread that's not informative
    because the smaller-venue side is too thin to trust.
    """
    by_venue = shares.get(class_id, {})
    if len(by_venue) < 2:
        return False
    return any(share > threshold_pct for share in by_venue.values())


__all__ = ["assemble"]
