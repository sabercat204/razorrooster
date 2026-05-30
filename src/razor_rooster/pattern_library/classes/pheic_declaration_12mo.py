"""Seed class: WHO PHEIC declaration in a 12-month window (T-PL-070).

A Public Health Emergency of International Concern (PHEIC) is the WHO's
formal escalation lever. Five have been declared since 2009 — H1N1,
polio, Ebola (West Africa), Zika, COVID-19, and the 2022 mpox outbreak.
This class evaluates the per-12-month rate of any new PHEIC declaration
plus precursor signals from WHO Disease Outbreak News (DON) frequency.

Predicate: WHO DON entries flagged as escalating to PHEIC. v1 reads
from ``event_stream`` rows where ``source_id = 'who_don'`` and the
description text contains "PHEIC" — a deliberately loose match suitable
for the refresh-pipeline scaffolding. Refinement after T-PL-081.

Limitations (v1):
- Match is lexical, not semantic. Future versions could parse a curated
  PHEIC-declaration list maintained as configuration.
- Sample size is tiny (5 PHEICs in WHO history) → low_sample_warning
  fires and base-rate credible interval is wide. That's the correct
  behavior; the operator sees the warning prominently.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb
import pandas as pd

from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    PrecursorVariable,
    Sector,
)


def _occurrences(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Pull PHEIC-flagged WHO DON entries from the canonical event_stream."""
    rows = conn.execute(
        "SELECT MIN(event_ts) AS occurrence_ts, description "
        "FROM event_stream "
        "WHERE source_id = 'who_don' "
        "  AND superseded_at IS NULL "
        "  AND (UPPER(description) LIKE '%PHEIC%' "
        "       OR UPPER(description) LIKE '%PUBLIC HEALTH EMERGENCY OF INTERNATIONAL CONCERN%') "
        "GROUP BY description "
        "ORDER BY occurrence_ts"
    ).fetchall()
    if not rows:
        return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})
    return pd.DataFrame(
        {
            "occurrence_ts": pd.to_datetime([r[0] for r in rows], utc=True),
            "description": [r[1] for r in rows],
        }
    )


def _don_frequency_precursor(
    conn: duckdb.DuckDBPyConnection,
    window_start: datetime,
    window_end: datetime,
) -> pd.Series:
    """WHO DON publication frequency over the lead window.

    Returns a daily series counting DON entries published per day. A
    surge in DON publication often precedes a PHEIC escalation by
    months.
    """
    rows = conn.execute(
        "SELECT event_ts FROM event_stream "
        "WHERE source_id = 'who_don' AND superseded_at IS NULL "
        "  AND event_ts >= ? AND event_ts < ? "
        "ORDER BY event_ts",
        [window_start, window_end],
    ).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    timestamps = pd.to_datetime([r[0] for r in rows], utc=True)
    daily_counts = timestamps.value_counts().sort_index()
    # Resample to a regular daily index across the window.
    full_index = pd.date_range(start=window_start, end=window_end, freq="D", tz="UTC")
    return daily_counts.reindex(full_index, fill_value=0).astype(float)


CLASS = EventClass(
    class_id="pheic_declaration_12mo",
    title="WHO PHEIC declaration in a 12-month window",
    description=(
        "A Public Health Emergency of International Concern declaration by the "
        "WHO. Tests low-sample base-rate handling — only 5-6 PHEICs since 2009. "
        "Refresh produces a wide credible interval and the low_sample warning."
    ),
    domain_sector=Sector.PUBLIC_HEALTH,
    occurrence_query=_occurrences,
    precursors=(
        PrecursorVariable(
            variable_id="who_don_publication_frequency",
            title="WHO DON publication frequency (daily)",
            query=_don_frequency_precursor,
            direction="high_signals_event",
            lead_time_window=timedelta(days=180),
        ),
    ),
    base_rate_window_default=timedelta(days=365 * 15),
    refractory_months=12,
    baseline_sample_size=200,
)
