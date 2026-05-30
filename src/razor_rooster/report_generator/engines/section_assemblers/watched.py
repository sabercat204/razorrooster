"""Active watched assembler (T-RG-023; design §3.5).

Returns a content dict shaped like::

    {
      "type": "watched",
      "follow_ups": [
        {
          "follow_up_id": "...",
          "analysis_id": "...",
          "class_id": "...",
          "class_title": "...",
          "primary_alert_tier": str | None,
          "alert_tiers": list[str],
          "analysis_model_p": float,
          "current_model_p": float | None,
          "analysis_market_p": float | None,
          "current_market_p": float | None,
          "model_shift_band": str | None,
          "market_shift_band": str | None,
          "days_since_analysis": int,
          "days_to_resolution": int | None,
          "resolution_status": str,
          "reasoning_text": str,
        },
        ...
      ],
    }

Ordered by alert tier priority (resolution > invalidation_triggered >
material_shift > precursor_shift > time_decay) then recency.
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
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT follow_up_id, analysis_id, primary_alert_tier, alert_tiers, "
        "analysis_model_p, current_model_p, analysis_market_p, "
        "current_market_p, model_shift_band, market_shift_band, "
        "days_since_analysis, days_to_resolution, resolution_status, "
        "reasoning_text, computed_at, venue "
        "FROM follow_ups "
        "WHERE recommended_review = TRUE "
        "AND computed_at >= ? AND computed_at <= ? "
        "ORDER BY CASE primary_alert_tier "
        "WHEN 'resolution' THEN 0 "
        "WHEN 'invalidation_triggered' THEN 1 "
        "WHEN 'material_shift' THEN 2 "
        "WHEN 'precursor_shift' THEN 3 "
        "WHEN 'time_decay' THEN 4 "
        "ELSE 5 END, computed_at DESC",
        [since_ts, until_ts],
    ).fetchall()
    follow_ups: list[dict[str, Any]] = []
    for row in rows:
        analysis = _query_analysis_meta(conn, analysis_id=str(row[1]))
        class_id = analysis.get("class_id", "?") if analysis else "?"
        class_title = analysis.get("class_title", class_id) if analysis else class_id
        condition_id = analysis.get("condition_id") if analysis else None
        alert_tiers = _decode_alert_tiers(row[3])
        follow_ups.append(
            {
                "follow_up_id": str(row[0]),
                "analysis_id": str(row[1]),
                "class_id": class_id,
                "class_title": class_title,
                "condition_id": condition_id,
                "venue": str(row[15]) if row[15] is not None else "polymarket",
                "primary_alert_tier": (str(row[2]) if row[2] is not None else None),
                "alert_tiers": alert_tiers,
                "analysis_model_p": float(row[4]),
                "current_model_p": (float(row[5]) if row[5] is not None else None),
                "analysis_market_p": (float(row[6]) if row[6] is not None else None),
                "current_market_p": (float(row[7]) if row[7] is not None else None),
                "model_shift_band": (str(row[8]) if row[8] is not None else None),
                "market_shift_band": (str(row[9]) if row[9] is not None else None),
                "days_since_analysis": int(row[10]),
                "days_to_resolution": (int(row[11]) if row[11] is not None else None),
                "resolution_status": str(row[12]),
                "reasoning_text": str(row[13]),
                "computed_at": row[14],
            }
        )
    return {"type": "watched", "follow_ups": follow_ups}


# -- internals --------------------------------------------------------------


def _query_analysis_meta(
    conn: duckdb.DuckDBPyConnection, *, analysis_id: str
) -> dict[str, Any] | None:
    """Pull class_id + class_title + condition_id for the analysis."""
    try:
        row = conn.execute(
            "SELECT a.analysis_id, a.class_id, c.title, a.condition_id "
            "FROM analyses a "
            "LEFT JOIN pl_event_classes c ON a.class_id = c.class_id "
            "WHERE a.analysis_id = ?",
            [analysis_id],
        ).fetchone()
    except duckdb.CatalogException:
        return None
    if row is None:
        return None
    return {
        "analysis_id": str(row[0]),
        "class_id": str(row[1]),
        "class_title": str(row[2]) if row[2] is not None else str(row[1]),
        "condition_id": str(row[3]) if row[3] is not None else None,
    }


def _decode_alert_tiers(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str) and raw:
        import json

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(decoded, list):
            return [str(t) for t in decoded]
    return []


__all__ = ["assemble"]
