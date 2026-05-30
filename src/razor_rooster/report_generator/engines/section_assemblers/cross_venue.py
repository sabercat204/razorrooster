"""Cross-venue disagreement section (T-RG-COMPAT §1; supplement
T-COMP-CV-001).

When the same event class is mapped to both Polymarket and Kalshi,
the two venues' market-implied probabilities sometimes disagree
materially. That disagreement is informative even within v1's
educational framing: if Polymarket says 30% and Kalshi says 60% on
the same question, the two markets are telling each other something,
and the operator should weigh both venues' prices before acting on
the model's view.

This section reads recently-computed comparisons grouped by
``class_id``, finds classes with comparisons spanning multiple venues
whose market-implied probabilities diverge by more than a configurable
threshold (default 5 percentage points), and surfaces them in the
report with a per-venue breakdown.

Returns a content dict shaped like::

    {
      "type": "cross_venue",
      "spread_threshold_bps": 500,
      "items": [
        {
          "class_id": "...",
          "class_title": "...",
          "domain_sector": "...",
          "venue_prices": [
            {
              "venue": "polymarket"|"kalshi",
              "comparison_id": "...",
              "condition_id": "...",
              "market_probability": float | None,
              "market_volume_24h": float | None,
              "market_spread_bps": int | None,
              "market_snapshot_ts": datetime | None,
              "model_probability": float,
              "model_ci": (float, float),
            },
            ...
          ],
          "spread_bps": int,
          "max_market_p": float,
          "min_market_p": float,
        },
        ...
      ],
    }

Items are ordered by ``spread_bps`` descending (largest disagreements
first). Empty list when no classes meet the threshold.

Framing rule (carried forward from REQ-RG-FRAME-002): the section
*describes* the disagreement; it does not direct the operator to act
on it. The renderer hands the operator a side-by-side comparison and
the disclaimer block does the rest.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

import duckdb

logger = logging.getLogger(__name__)


# Default disagreement threshold per supplement §1.3: 5 percentage
# points. Configurable via ``config/report.yaml`` (see ReportConfig
# in v1.2; for now this default is the only path).
DEFAULT_SPREAD_THRESHOLD_BPS: Final[int] = 500


def assemble(
    conn: duckdb.DuckDBPyConnection,
    *,
    since_ts: datetime,
    until_ts: datetime,
    spread_threshold_bps: int = DEFAULT_SPREAD_THRESHOLD_BPS,
    spread_threshold_bps_per_sector: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Assemble the cross-venue disagreement section content.

    Reads ``comparisons`` for the cycle window, groups by class_id,
    and emits one item per class whose venue prices spread by more
    than the per-sector threshold (or the global
    ``spread_threshold_bps`` when no sector override applies).

    No-op when no class meets its applicable threshold — the section
    renders the standard "no cross-venue disagreements this cycle"
    empty message.
    """
    per_sector = dict(spread_threshold_bps_per_sector or {})
    rows = conn.execute(
        "SELECT comparison_id, class_id, condition_id, venue, "
        "market_probability, market_volume_24h, market_spread_bps, "
        "market_snapshot_ts, model_probability, model_ci_lower, "
        "model_ci_upper, computed_at "
        "FROM comparisons "
        "WHERE computed_at >= ? AND computed_at <= ? "
        "AND market_probability IS NOT NULL "
        "ORDER BY computed_at DESC",
        [since_ts, until_ts],
    ).fetchall()

    # Group by class_id; keep the most recent comparison per (class_id,
    # venue) pair so a class that produced multiple comparisons across
    # the cycle window doesn't trigger spurious self-disagreement.
    by_class_venue: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        class_id = str(row[1])
        venue = str(row[3])
        key = (class_id, venue)
        if key in by_class_venue:
            # We sorted DESC by computed_at; first entry per key wins.
            continue
        by_class_venue[key] = {
            "comparison_id": str(row[0]),
            "condition_id": str(row[2]),
            "venue": venue,
            "market_probability": float(row[4]),
            "market_volume_24h": (float(row[5]) if row[5] is not None else None),
            "market_spread_bps": (int(row[6]) if row[6] is not None else None),
            "market_snapshot_ts": row[7],
            "model_probability": float(row[8]),
            "model_ci": (float(row[9]), float(row[10])),
        }

    # Group comparisons by class_id.
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (class_id, _venue), entry in by_class_venue.items():
        by_class[class_id].append(entry)

    items: list[dict[str, Any]] = []
    for class_id, venue_entries in by_class.items():
        if len(venue_entries) < 2:
            continue
        market_probs = [e["market_probability"] for e in venue_entries]
        max_p = max(market_probs)
        min_p = min(market_probs)
        spread_bps = round(abs(max_p - min_p) * 10_000)
        cls = _query_class(conn, class_id=class_id)
        sector = cls.get("domain_sector", "unknown") if cls else "unknown"
        # Per-sector threshold — fall through to the global value when
        # there's no override entry for this sector.
        applicable_threshold = per_sector.get(sector, spread_threshold_bps)
        if spread_bps < applicable_threshold:
            continue
        # Sort the per-venue breakdown alphabetically so the report's
        # output is deterministic across cycles.
        venue_entries_sorted = sorted(venue_entries, key=lambda e: str(e["venue"]))
        consensus_p, total_volume = _liquidity_weighted_consensus(venue_entries_sorted)
        items.append(
            {
                "class_id": class_id,
                "class_title": cls.get("title", class_id) if cls else class_id,
                "domain_sector": sector,
                "venue_prices": venue_entries_sorted,
                "spread_bps": spread_bps,
                "applicable_threshold_bps": applicable_threshold,
                "max_market_p": max_p,
                "min_market_p": min_p,
                "consensus_market_p": consensus_p,
                "total_volume_24h": total_volume,
            }
        )

    # Largest disagreements first.
    items.sort(key=lambda item: -int(item["spread_bps"]))

    return {
        "type": "cross_venue",
        "spread_threshold_bps": spread_threshold_bps,
        "spread_threshold_bps_per_sector": dict(per_sector),
        "items": items,
    }


# -- internals --------------------------------------------------------------


def _liquidity_weighted_consensus(
    venue_entries: list[dict[str, Any]],
) -> tuple[float | None, float | None]:
    """Compute the liquidity-weighted average market_probability.

    Weights each venue's market_probability by its 24h volume. When
    every venue has NULL volume, falls back to an unweighted mean so
    operators still see *some* consensus number rather than None;
    flag this in the renderer if it matters. When there's no
    market_probability data at all, returns (None, None).

    Returns ``(consensus_market_p, total_volume_24h)``.
    """
    weighted_sum = 0.0
    total_weight = 0.0
    for entry in venue_entries:
        market_p = entry.get("market_probability")
        if market_p is None:
            continue
        volume = entry.get("market_volume_24h")
        weight = float(volume) if volume is not None and volume > 0 else 0.0
        weighted_sum += float(market_p) * weight
        total_weight += weight
    if total_weight > 0:
        return weighted_sum / total_weight, total_weight
    # Fallback: unweighted mean across venues with a price.
    prices = [
        float(e["market_probability"])
        for e in venue_entries
        if e.get("market_probability") is not None
    ]
    if not prices:
        return None, None
    return sum(prices) / len(prices), None


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


__all__ = ["DEFAULT_SPREAD_THRESHOLD_BPS", "assemble"]
