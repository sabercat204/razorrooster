"""Seed class: EIA grid reliability event (T-PL-075).

A NERC-reportable grid reliability event identified from EIA's
infrastructure data. v1 scaffold: occurrence query reads
``time_series`` rows tagged with EIA's grid-status indicator. Real
EIA grid-event tables aren't yet in the v1 data_ingest corpus, so
this class returns empty until the EIA connector adds the relevant
series in a future round (DEFER-PL-001).

The class still exercises the full refresh pipeline — empty
occurrence list → empty pl_outcomes → zero-occurrence base rate with
low_sample_warning, no signatures persisted, no analogues persisted,
calibration sentinel.
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
    """EIA grid-reliability events.

    Heuristic predicate: days where any EIA series tagged with
    'grid_disturbance' or 'reliability_event' has a non-zero value.
    Real EIA series for this don't exist in the v1 corpus; the class
    returns an empty frame until the EIA connector adds them.
    """
    rows = conn.execute(
        "SELECT observation_ts AS occurrence_ts, series_id "
        "FROM time_series "
        "WHERE source_id = 'eia' "
        "  AND superseded_at IS NULL "
        "  AND (series_id LIKE '%grid_disturbance%' "
        "       OR series_id LIKE '%reliability_event%') "
        "  AND value IS NOT NULL AND value > 0 "
        "ORDER BY observation_ts"
    ).fetchall()
    if not rows:
        return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})
    return pd.DataFrame(
        {
            "occurrence_ts": pd.to_datetime([r[0] for r in rows], utc=True),
            "description": [f"series={r[1]}" for r in rows],
        }
    )


def _grid_load_precursor(
    conn: duckdb.DuckDBPyConnection,
    window_start: datetime,
    window_end: datetime,
) -> pd.Series:
    """Daily EIA total electricity demand (proxy for grid stress)."""
    rows = conn.execute(
        "SELECT observation_ts, value "
        "FROM time_series "
        "WHERE source_id = 'eia' "
        "  AND series_id LIKE '%electricity_demand%' "
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
    class_id="eia_grid_reliability_event",
    title="EIA grid reliability event",
    description=(
        "EIA-reported grid reliability event (NERC categories). v1 scaffold "
        "returns empty until EIA connector adds the relevant series. The "
        "class still exercises the full refresh pipeline against zero "
        "occurrences — exposing low_sample_warning behavior."
    ),
    domain_sector=Sector.INFRASTRUCTURE_ENERGY,
    occurrence_query=_occurrences,
    precursors=(
        PrecursorVariable(
            variable_id="grid_load_demand",
            title="EIA daily electricity demand",
            query=_grid_load_precursor,
            direction="high_signals_event",
            lead_time_window=timedelta(days=14),
            threshold_method=ThresholdMethod.QUANTILE_95,
        ),
    ),
    base_rate_window_default=timedelta(days=365 * 10),
    refractory_months=3,
    baseline_sample_size=200,
)
