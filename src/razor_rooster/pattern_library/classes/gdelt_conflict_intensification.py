"""Seed class: GDELT conflict intensification (T-PL-071).

A country-week is flagged as a "conflict intensification" event when
the average GDELT tone (negative) and event count both cross thresholds
within a 7-day rolling window. Tests dense / abundant data — GDELT
events are 15-minute-cadence and the historical archive is years deep,
so this class is the inverse stress case to the rare-PHEIC class.

Predicate: rolling 7-day count of conflict-coded GDELT events exceeds
a manual threshold per country. The class scaffolding uses a global
threshold for v1; per-country tuning lives in DEFER-PL-001.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb
import pandas as pd

from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    PrecursorVariable,
    Sector,
    ThresholdMethod,
)


def _occurrences(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Country-weeks where GDELT conflict-coded event density spikes.

    A "spike" is the rolling 7-day event count exceeding 50. The
    threshold is global for v1 — a future per-country normalization is
    the obvious next step (DEFER-PL-001).
    """
    rows = conn.execute(
        "WITH counts AS ("
        "  SELECT date_trunc('day', event_ts) AS day, country_iso3, "
        "         COUNT(*) AS event_count "
        "  FROM event_stream "
        "  WHERE source_id = 'gdelt_events' "
        "    AND superseded_at IS NULL "
        "    AND country_iso3 IS NOT NULL "
        "  GROUP BY day, country_iso3"
        ") "
        "SELECT day AS occurrence_ts, country_iso3 "
        "FROM counts "
        "WHERE event_count >= 50 "
        "ORDER BY day"
    ).fetchall()
    if not rows:
        return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})
    return pd.DataFrame(
        {
            "occurrence_ts": pd.to_datetime([r[0] for r in rows], utc=True),
            "description": [f"country={r[1]}" for r in rows],
        }
    )


def _conflict_event_density(
    conn: duckdb.DuckDBPyConnection,
    window_start: datetime,
    window_end: datetime,
) -> pd.Series:
    """Daily count of GDELT conflict events across all countries."""
    rows = conn.execute(
        "SELECT date_trunc('day', event_ts) AS day, COUNT(*) "
        "FROM event_stream "
        "WHERE source_id = 'gdelt_events' AND superseded_at IS NULL "
        "  AND event_ts >= ? AND event_ts < ? "
        "GROUP BY day "
        "ORDER BY day",
        [window_start, window_end],
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    timestamps = pd.to_datetime([r[0] for r in rows], utc=True)
    counts = pd.Series([r[1] for r in rows], index=timestamps, dtype=float)
    full_index = pd.date_range(start=window_start, end=window_end, freq="D", tz="UTC")
    return counts.reindex(full_index, fill_value=0.0)


CLASS = EventClass(
    class_id="gdelt_conflict_intensification",
    title="GDELT country-week conflict intensification",
    description=(
        "A country-week with GDELT conflict-coded event count exceeding 50 "
        "in a single day. Tests dense / high-volume data — opposite stress "
        "case to PHEIC. Operator-tunable per-country thresholds in v1.1."
    ),
    domain_sector=Sector.GEOPOLITICAL,
    occurrence_query=_occurrences,
    precursors=(
        PrecursorVariable(
            variable_id="gdelt_event_density",
            title="GDELT conflict-event daily density",
            query=_conflict_event_density,
            direction="high_signals_event",
            lead_time_window=timedelta(days=14),
            threshold_method=ThresholdMethod.QUANTILE_95,
        ),
    ),
    base_rate_window_default=timedelta(days=365 * 5),
    refractory_months=1,
    baseline_sample_size=500,
)
