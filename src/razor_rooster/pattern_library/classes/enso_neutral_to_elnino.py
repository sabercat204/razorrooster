"""Seed class: ENSO neutral → El Niño transition (T-PL-074).

A calendar quarter where the ENSO state shifts from neutral to El
Niño. Tests time-series threshold predicates against NOAA-derived
data — the El Niño threshold is +0.5°C ENSO 3.4 anomaly sustained for
five consecutive months per NOAA convention.

For v1, the ENSO 3.4 series is identified by a series_id pattern
match on data_ingest's ``time_series`` table. The actual NOAA series
mapping is finalized at backfill time; the predicate gracefully
returns an empty list when no matching series is present.
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
    """Quarters where the rolling-3-month ENSO 3.4 anomaly crosses +0.5°C upward.

    Reads the ENSO series (anomaly values) from time_series. An
    occurrence is the first day of a quarter where the rolling
    3-month mean is >= 0.5 AND the prior quarter's mean was < 0.5.
    """
    rows = conn.execute(
        "WITH enso AS ("
        "  SELECT observation_ts, value "
        "  FROM time_series "
        "  WHERE source_id = 'noaa' "
        "    AND (series_id LIKE '%ENSO%' OR series_id LIKE '%nino34%' "
        "         OR series_id LIKE '%ONI%') "
        "    AND superseded_at IS NULL "
        "    AND value IS NOT NULL "
        "), "
        "rolling AS ("
        "  SELECT observation_ts, "
        "         AVG(value) OVER ("
        "           ORDER BY observation_ts ROWS BETWEEN 89 PRECEDING AND CURRENT ROW"
        "         ) AS rolling_3mo "
        "  FROM enso"
        "), "
        "transitions AS ("
        "  SELECT observation_ts, "
        "         date_trunc('quarter', observation_ts) AS quarter_start, "
        "         rolling_3mo, "
        "         LAG(rolling_3mo, 90) OVER (ORDER BY observation_ts) AS prior_rolling "
        "  FROM rolling"
        ") "
        "SELECT MIN(quarter_start) AS occurrence_ts "
        "FROM transitions "
        "WHERE rolling_3mo >= 0.5 AND prior_rolling < 0.5 "
        "GROUP BY quarter_start "
        "ORDER BY occurrence_ts"
    ).fetchall()
    if not rows:
        return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})
    return pd.DataFrame(
        {
            "occurrence_ts": pd.to_datetime([r[0] for r in rows if r[0] is not None], utc=True),
            "description": ["ENSO neutral->elnino"] * len([r for r in rows if r[0] is not None]),
        }
    )


def _enso_anomaly_precursor(
    conn: duckdb.DuckDBPyConnection,
    window_start: datetime,
    window_end: datetime,
) -> pd.Series:
    """Daily ENSO 3.4 anomaly value over the lead window."""
    rows = conn.execute(
        "SELECT observation_ts, value "
        "FROM time_series "
        "WHERE source_id = 'noaa' "
        "  AND (series_id LIKE '%ENSO%' OR series_id LIKE '%nino34%' "
        "       OR series_id LIKE '%ONI%') "
        "  AND superseded_at IS NULL "
        "  AND value IS NOT NULL "
        "  AND observation_ts >= ? AND observation_ts < ? "
        "ORDER BY observation_ts",
        [window_start, window_end],
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    timestamps = pd.to_datetime([r[0] for r in rows], utc=True)
    return pd.Series([r[1] for r in rows], index=timestamps, dtype=float)


CLASS = EventClass(
    class_id="enso_neutral_to_elnino",
    title="ENSO neutral->elnino transition by quarter",
    description=(
        "Quarters where the rolling 3-month ENSO 3.4 anomaly crosses +0.5°C "
        "from below. Tests time-series threshold predicates against NOAA data."
    ),
    domain_sector=Sector.CLIMATE,
    occurrence_query=_occurrences,
    precursors=(
        PrecursorVariable(
            variable_id="enso_anomaly_value",
            title="ENSO 3.4 anomaly daily value",
            query=_enso_anomaly_precursor,
            direction="high_signals_event",
            lead_time_window=timedelta(days=180),
            threshold_method=ThresholdMethod.MANUAL,
            manual_threshold=0.3,
        ),
    ),
    base_rate_window_default=timedelta(days=365 * 30),
    refractory_months=18,
    baseline_sample_size=200,
)
