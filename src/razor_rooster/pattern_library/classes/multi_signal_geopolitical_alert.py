"""Seed class: multi-signal geopolitical alert (T-PL-076).

Tests the multi-precursor combination logic (REQ-PL-SIG-004). The
event is an ACLED density spike in a country combined with a GDELT
tone shift downward, optionally co-occurring with a Federal Register
filing on the relevant agency. Three precursors → exercises
``combine_variables`` + the co-occurrence lookup.

v1 scaffold: occurrence list is the union of high-density ACLED
country-weeks. The three precursors query each source independently
so the signature engine learns their hit rates and the co-occurrence
table from real historical data.
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
    """Country-weeks where ACLED conflict-event density jumps."""
    rows = conn.execute(
        "WITH acled_density AS ("
        "  SELECT date_trunc('week', event_ts) AS week, country_iso3, "
        "         COUNT(*) AS event_count "
        "  FROM event_stream "
        "  WHERE source_id = 'acled' AND superseded_at IS NULL "
        "    AND country_iso3 IS NOT NULL "
        "  GROUP BY week, country_iso3"
        ") "
        "SELECT week AS occurrence_ts, country_iso3 "
        "FROM acled_density "
        "WHERE event_count >= 30 "
        "ORDER BY week"
    ).fetchall()
    if not rows:
        return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})
    return pd.DataFrame(
        {
            "occurrence_ts": pd.to_datetime([r[0] for r in rows], utc=True),
            "description": [f"country={r[1]}" for r in rows],
        }
    )


def _acled_event_density(
    conn: duckdb.DuckDBPyConnection,
    window_start: datetime,
    window_end: datetime,
) -> pd.Series:
    """Daily ACLED event count across all countries."""
    rows = conn.execute(
        "SELECT date_trunc('day', event_ts) AS day, COUNT(*) "
        "FROM event_stream "
        "WHERE source_id = 'acled' AND superseded_at IS NULL "
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


def _gdelt_tone_shift(
    conn: duckdb.DuckDBPyConnection,
    window_start: datetime,
    window_end: datetime,
) -> pd.Series:
    """Daily count of GDELT events (proxy for tone-shift volume).

    A proper signed-tone series is data_ingest TODO; v1 uses event
    count as a coarse proxy for "the news flow has changed."
    """
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


def _federal_register_state_dept_filings(
    conn: duckdb.DuckDBPyConnection,
    window_start: datetime,
    window_end: datetime,
) -> pd.Series:
    """Daily State/Defense/Treasury Federal Register filings."""
    rows = conn.execute(
        "SELECT date_trunc('day', published_date) AS day, COUNT(*) "
        "FROM document_docket "
        "WHERE source_id = 'federal_register' "
        "  AND superseded_at IS NULL "
        "  AND (UPPER(agency) LIKE '%STATE%' "
        "       OR UPPER(agency) LIKE '%DEFENSE%' "
        "       OR UPPER(agency) LIKE '%TREASURY%' "
        "       OR UPPER(agency) LIKE '%OFAC%') "
        "  AND published_date >= ? AND published_date < ? "
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
    class_id="multi_signal_geopolitical_alert",
    title="Multi-signal geopolitical alert (ACLED + GDELT + Federal Register)",
    description=(
        "Country-weeks with ACLED density >= 30 events. Combines three "
        "precursors (ACLED density, GDELT event volume, Federal Register "
        "diplomatic-agency filings) to exercise multi-variable combination "
        "logic and the co-occurrence lookup table."
    ),
    domain_sector=Sector.GEOPOLITICAL,
    occurrence_query=_occurrences,
    precursors=(
        PrecursorVariable(
            variable_id="acled_event_density",
            title="ACLED daily event density",
            query=_acled_event_density,
            direction="high_signals_event",
            lead_time_window=timedelta(days=14),
            threshold_method=ThresholdMethod.QUANTILE_95,
        ),
        PrecursorVariable(
            variable_id="gdelt_tone_shift",
            title="GDELT daily volume (tone-shift proxy)",
            query=_gdelt_tone_shift,
            direction="high_signals_event",
            lead_time_window=timedelta(days=21),
            threshold_method=ThresholdMethod.QUANTILE_95,
        ),
        PrecursorVariable(
            variable_id="federal_register_diplomatic_filings",
            title="Federal Register diplomatic-agency filings",
            query=_federal_register_state_dept_filings,
            direction="high_signals_event",
            lead_time_window=timedelta(days=28),
            threshold_method=ThresholdMethod.QUANTILE_95,
        ),
    ),
    base_rate_window_default=timedelta(days=365 * 5),
    refractory_months=2,
    baseline_sample_size=300,
)
