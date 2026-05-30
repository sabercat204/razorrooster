"""Seed class: OPEC unscheduled production cut (T-PL-073).

OPEC announces production-cut decisions on scheduled meeting dates and
occasionally outside that schedule. The unscheduled-cut event is the
analytically interesting one — it's typically a response to either
price collapse or geopolitical disruption.

The class is a sparse-event scaffolding for v1: occurrences are
identified from FRED oil-price data via a heuristic dip-then-jump
pattern. Refinement after T-PL-081 — the proper occurrence list comes
from a curated OPEC announcement table, which doesn't exist in the v1
data_ingest corpus.
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
    """Heuristic occurrence list: weeks where Brent price jumped > 10% in 5 days.

    Designed to scaffold the refresh pipeline against real FRED data
    when the backfill is populated. The heuristic is intentionally
    coarse — production tuning in v1.1 with a curated OPEC announcements
    list.
    """
    rows = conn.execute(
        "WITH brent AS ("
        "  SELECT observation_ts, value "
        "  FROM time_series "
        "  WHERE source_id = 'fred' "
        "    AND series_id LIKE '%DCOILBRENTEU%' "
        "    AND superseded_at IS NULL "
        "    AND value IS NOT NULL "
        "  ORDER BY observation_ts"
        ") "
        "SELECT a.observation_ts AS occurrence_ts "
        "FROM brent a "
        "INNER JOIN brent b "
        "  ON b.observation_ts = a.observation_ts - INTERVAL 5 DAY "
        "WHERE a.value / NULLIF(b.value, 0) >= 1.10 "
        "ORDER BY occurrence_ts"
    ).fetchall()
    if not rows:
        return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})
    return pd.DataFrame(
        {
            "occurrence_ts": pd.to_datetime([r[0] for r in rows], utc=True),
            "description": ["heuristic: 5-day brent jump >= 10%"] * len(rows),
        }
    )


def _brent_price_precursor(
    conn: duckdb.DuckDBPyConnection,
    window_start: datetime,
    window_end: datetime,
) -> pd.Series:
    """Daily Brent crude price (FRED DCOILBRENTEU)."""
    rows = conn.execute(
        "SELECT observation_ts, value "
        "FROM time_series "
        "WHERE source_id = 'fred' "
        "  AND series_id LIKE '%DCOILBRENTEU%' "
        "  AND superseded_at IS NULL "
        "  AND observation_ts >= ? AND observation_ts < ? "
        "  AND value IS NOT NULL "
        "ORDER BY observation_ts",
        [window_start, window_end],
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    timestamps = pd.to_datetime([r[0] for r in rows], utc=True)
    return pd.Series([r[1] for r in rows], index=timestamps, dtype=float)


CLASS = EventClass(
    class_id="opec_unscheduled_cut",
    title="OPEC unscheduled production cut (heuristic via FRED Brent jumps)",
    description=(
        "Weeks where the FRED Brent crude price jumps by 10% over 5 days, used "
        "as a heuristic for OPEC unscheduled cuts. Tests sparse-event signature "
        "scaffolding with FRED + (in v1.1) EIA precursors. Refinement post-T-PL-081."
    ),
    domain_sector=Sector.COMMODITY,
    occurrence_query=_occurrences,
    precursors=(
        PrecursorVariable(
            variable_id="brent_price_level",
            title="FRED Brent crude price",
            query=_brent_price_precursor,
            direction="low_signals_event",  # low prices precede cuts
            lead_time_window=timedelta(days=60),
            threshold_method=ThresholdMethod.QUANTILE_95,
        ),
    ),
    base_rate_window_default=timedelta(days=365 * 20),
    refractory_months=3,
    baseline_sample_size=500,
)
