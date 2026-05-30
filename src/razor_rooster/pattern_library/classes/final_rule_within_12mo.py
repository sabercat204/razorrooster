"""Seed class: Federal Register final rule within 12 months of proposed (T-PL-072).

For each docket where a proposed rule was published, did the agency
issue a final rule within 12 months? Tests the document_docket schema
plus the well-specified-predicate corner — the docket_id ties proposed
and final rule rows together.

Predicate: paired (proposed_date, final_date) where the gap is ≤ 365
days. Occurrences are dated at the final-rule publication.
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
    """Pairs of (proposed_date, final_date) on the same docket within 12 months.

    The query joins document_docket rows on docket_id where one carries
    document_type='proposed_rule' and the other 'rule' (final). The
    occurrence_ts is the final-rule date.
    """
    rows = conn.execute(
        "SELECT final.published_date AS occurrence_ts, "
        "       final.docket_id AS docket_id "
        "FROM document_docket AS final "
        "INNER JOIN document_docket AS proposed "
        "  ON final.docket_id = proposed.docket_id "
        " AND final.source_id = 'federal_register' "
        " AND proposed.source_id = 'federal_register' "
        " AND final.document_type = 'rule' "
        " AND proposed.document_type = 'proposed_rule' "
        " AND final.superseded_at IS NULL "
        " AND proposed.superseded_at IS NULL "
        "WHERE date_diff('day', proposed.published_date, final.published_date) "
        "      BETWEEN 0 AND 365 "
        "ORDER BY final.published_date"
    ).fetchall()
    if not rows:
        return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})
    return pd.DataFrame(
        {
            "occurrence_ts": pd.to_datetime([r[0] for r in rows], utc=True),
            "description": [f"docket={r[1]}" for r in rows],
        }
    )


def _proposed_rule_count(
    conn: duckdb.DuckDBPyConnection,
    window_start: datetime,
    window_end: datetime,
) -> pd.Series:
    """Daily count of proposed-rule publications in the Federal Register."""
    rows = conn.execute(
        "SELECT date_trunc('day', published_date) AS day, COUNT(*) "
        "FROM document_docket "
        "WHERE source_id = 'federal_register' "
        "  AND document_type = 'proposed_rule' "
        "  AND superseded_at IS NULL "
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
    class_id="final_rule_within_12mo",
    title="Federal Register final rule within 12 months of proposed",
    description=(
        "For dockets with a proposed-rule publication, the rate at which a "
        "final rule appears within 12 months. Tests the document_docket "
        "schema and well-specified joined predicates."
    ),
    domain_sector=Sector.REGULATORY,
    occurrence_query=_occurrences,
    precursors=(
        PrecursorVariable(
            variable_id="federal_register_proposed_rule_count",
            title="Federal Register proposed-rule daily count",
            query=_proposed_rule_count,
            direction="high_signals_event",
            lead_time_window=timedelta(days=180),
        ),
    ),
    base_rate_window_default=timedelta(days=365 * 30),
    refractory_months=6,
    baseline_sample_size=300,
)
