"""Unit tests for the calibration_backtest polarity resolver (T-CB-015).

The resolver is the only path through which the replay loop learns
which polarity (``aligned`` vs ``inverted``) to apply when scoring a
historical Polymarket outcome against a model class. The tests exercise
the three-tier chain end-to-end against a real in-memory DuckDB
connection seeded with the canonical ``comparisons``,
``comparison_resolutions`` and ``class_market_mappings`` tables from
the mispricing-detector schema. Doing so ensures the SQL the resolver
issues actually parses on DuckDB and the table joins line up with the
production schema rather than a hand-rolled stub that might drift.

The ordering test on Tier 1 is correctness-critical: the design calls
for the **earliest** comparison resolution after ``prediction_ts`` to
win, because operator-curated mappings can flip after the prediction
was made and the backtest must score against the polarity that was in
effect at prediction time. Reversing the ``ORDER BY`` direction would
silently corrupt every backtest that crosses a polarity flip, so this
test is held as a regression guard.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from razor_rooster.calibration_backtest.engines import polarity
from razor_rooster.calibration_backtest.errors import NoPolarityError
from razor_rooster.mispricing_detector.persistence.schemas import (
    CLASS_MARKET_MAPPINGS_DDL,
    COMPARISON_CYCLES_DDL,
    COMPARISON_RESOLUTIONS_DDL,
    COMPARISONS_DDL,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield an in-memory DuckDB connection with the polarity tables created."""
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(CLASS_MARKET_MAPPINGS_DDL)
        connection.execute(COMPARISON_CYCLES_DDL)
        connection.execute(COMPARISONS_DDL)
        connection.execute(COMPARISON_RESOLUTIONS_DDL)
        yield connection
    finally:
        connection.close()


def _seed_cycle(conn: duckdb.DuckDBPyConnection, cycle_id: str = "cycle-1") -> str:
    """Insert a minimal ``comparison_cycles`` row required by FK semantics."""
    conn.execute(
        "INSERT INTO comparison_cycles ("
        "cycle_id, started_at, completed_at, comparisons_total, "
        "surfaced_count, suppressed_breakdown, library_version_at_cycle, "
        "scan_id_consumed, error_summary"
        ") VALUES (?, ?, NULL, 0, 0, '{}', 1, 'scan-1', NULL)",
        [cycle_id, datetime(2025, 1, 1, tzinfo=UTC)],
    )
    return cycle_id


def _insert_comparison(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    cycle_id: str,
    class_id: str,
    condition_id: str,
    venue: str = "polymarket",
    polarity_value: str = "aligned",
) -> None:
    """Insert a ``comparisons`` row with required NOT NULL columns populated."""
    conn.execute(
        "INSERT INTO comparisons ("
        "comparison_id, cycle_id, mapping_id, class_id, condition_id, "
        "outcome_token_id, polarity, scan_id, model_probability, "
        "model_ci_lower, model_ci_upper, computed_at, venue"
        ") VALUES (?, ?, 'mapping-x', ?, ?, 'tok', ?, 'scan-1', "
        "0.5, 0.4, 0.6, ?, ?)",
        [
            comparison_id,
            cycle_id,
            class_id,
            condition_id,
            polarity_value,
            datetime(2025, 1, 1, tzinfo=UTC),
            venue,
        ],
    )


def _insert_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    condition_id: str,
    resolution_ts: datetime,
    polarity_at_comparison: str,
    resolution_outcome: str = "yes",
    venue: str = "polymarket",
) -> None:
    """Insert a ``comparison_resolutions`` row keyed to a ``comparisons`` row."""
    conn.execute(
        "INSERT INTO comparison_resolutions ("
        "comparison_id, condition_id, resolution_outcome, resolution_ts, "
        "model_probability_at_comparison, market_probability_at_comparison, "
        "polarity_at_comparison, outcome_observed, linked_at, venue"
        ") VALUES (?, ?, ?, ?, 0.5, 0.5, ?, 1, ?, ?)",
        [
            comparison_id,
            condition_id,
            resolution_outcome,
            resolution_ts,
            polarity_at_comparison,
            resolution_ts,
            venue,
        ],
    )


def _insert_mapping(
    conn: duckdb.DuckDBPyConnection,
    *,
    mapping_id: str,
    class_id: str,
    condition_id: str,
    polarity_value: str,
    venue: str = "polymarket",
    removed_at: datetime | None = None,
) -> None:
    """Insert a ``class_market_mappings`` row with optional soft-delete."""
    conn.execute(
        "INSERT INTO class_market_mappings ("
        "mapping_id, class_id, condition_id, mapping_type, "
        "mapping_confidence, polarity, mapped_by, mapped_at, "
        "removed_at, notes, venue"
        ") VALUES (?, ?, ?, 'direct', 'high', ?, 'op', ?, ?, NULL, ?)",
        [
            mapping_id,
            class_id,
            condition_id,
            polarity_value,
            datetime(2025, 1, 1, tzinfo=UTC),
            removed_at,
            venue,
        ],
    )


# ---------------------------------------------------------------------------
# Tier 1 — comparison_resolutions hits
# ---------------------------------------------------------------------------


def test_tier_1_hit_returns_polarity_and_source(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A live ``comparison_resolutions`` row resolves via Tier 1."""
    cycle_id = _seed_cycle(conn)
    _insert_comparison(
        conn,
        comparison_id="cmp-1",
        cycle_id=cycle_id,
        class_id="cls-A",
        condition_id="cond-1",
    )
    _insert_resolution(
        conn,
        comparison_id="cmp-1",
        condition_id="cond-1",
        resolution_ts=datetime(2025, 6, 1, tzinfo=UTC),
        polarity_at_comparison="aligned",
    )

    polarity_value, source = polarity.resolve(
        conn,
        prediction_ts=datetime(2025, 5, 1, tzinfo=UTC),
        condition_id="cond-1",
        class_id="cls-A",
    )

    assert polarity_value == "aligned"
    assert source == polarity.SOURCE_COMPARISON_RESOLUTIONS


def test_tier_1_returns_earliest_resolution_after_prediction_ts(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Correctness-critical: ASC ordering selects the earliest eligible row.

    Three resolutions are seeded for the same ``(condition_id, class_id,
    venue)`` triple at staggered ``resolution_ts`` values around the
    prediction timestamp. The resolver must return the polarity carried
    by the **earliest** resolution whose ``resolution_ts`` is strictly
    greater than ``prediction_ts``; later resolutions may carry a
    flipped polarity if the operator re-curated the mapping after the
    prediction was made and the backtest must score against the
    polarity that was in effect at prediction time.
    """
    cycle_id = _seed_cycle(conn)
    prediction_ts = datetime(2025, 5, 15, tzinfo=UTC)
    earliest_after = prediction_ts + timedelta(days=1)
    middle_after = prediction_ts + timedelta(days=10)
    latest_after = prediction_ts + timedelta(days=30)
    before_prediction = prediction_ts - timedelta(days=5)

    # Seed four comparisons + resolutions, three after prediction_ts and
    # one before. The earliest-after row carries 'aligned'; later rows
    # carry 'inverted' to make the wrong sort direction observable.
    for idx, (ts, polarity_value) in enumerate(
        [
            (before_prediction, "aligned"),  # filtered: not > prediction_ts
            (latest_after, "inverted"),
            (middle_after, "inverted"),
            (earliest_after, "aligned"),
        ]
    ):
        cmp_id = f"cmp-{idx}"
        _insert_comparison(
            conn,
            comparison_id=cmp_id,
            cycle_id=cycle_id,
            class_id="cls-A",
            condition_id="cond-1",
        )
        _insert_resolution(
            conn,
            comparison_id=cmp_id,
            condition_id="cond-1",
            resolution_ts=ts,
            polarity_at_comparison=polarity_value,
        )

    polarity_value, source = polarity.resolve(
        conn,
        prediction_ts=prediction_ts,
        condition_id="cond-1",
        class_id="cls-A",
    )

    assert source == polarity.SOURCE_COMPARISON_RESOLUTIONS
    # Earliest row after prediction_ts carries 'aligned'; if ASC order
    # were ever flipped to DESC the test would surface 'inverted'.
    assert polarity_value == "aligned"


def test_tier_1_filters_resolution_outcome_invalid(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``resolution_outcome = 'invalid'`` rows must not feed Tier 1."""
    cycle_id = _seed_cycle(conn)
    prediction_ts = datetime(2025, 5, 1, tzinfo=UTC)
    invalid_ts = prediction_ts + timedelta(days=1)
    valid_ts = prediction_ts + timedelta(days=10)

    _insert_comparison(
        conn,
        comparison_id="cmp-invalid",
        cycle_id=cycle_id,
        class_id="cls-A",
        condition_id="cond-1",
    )
    _insert_resolution(
        conn,
        comparison_id="cmp-invalid",
        condition_id="cond-1",
        resolution_ts=invalid_ts,
        polarity_at_comparison="inverted",
        resolution_outcome="invalid",
    )

    _insert_comparison(
        conn,
        comparison_id="cmp-valid",
        cycle_id=cycle_id,
        class_id="cls-A",
        condition_id="cond-1",
    )
    _insert_resolution(
        conn,
        comparison_id="cmp-valid",
        condition_id="cond-1",
        resolution_ts=valid_ts,
        polarity_at_comparison="aligned",
        resolution_outcome="yes",
    )

    polarity_value, source = polarity.resolve(
        conn,
        prediction_ts=prediction_ts,
        condition_id="cond-1",
        class_id="cls-A",
    )

    # The 'invalid' row sits earlier than the valid one; if it were
    # accepted the resolver would return 'inverted'. The valid row
    # carries 'aligned', proving the invalid row was filtered out.
    assert polarity_value == "aligned"
    assert source == polarity.SOURCE_COMPARISON_RESOLUTIONS


def test_tier_1_filters_cross_venue_collision(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A Kalshi resolution must not pollute a Polymarket polarity query."""
    cycle_id = _seed_cycle(conn)
    prediction_ts = datetime(2025, 5, 1, tzinfo=UTC)

    # Kalshi row (earlier ts and would win without the venue filter)
    # vs Polymarket row (later ts, the row the caller wants).
    _insert_comparison(
        conn,
        comparison_id="cmp-kalshi",
        cycle_id=cycle_id,
        class_id="cls-A",
        condition_id="cond-1",
        venue="kalshi",
    )
    _insert_resolution(
        conn,
        comparison_id="cmp-kalshi",
        condition_id="cond-1",
        resolution_ts=prediction_ts + timedelta(days=1),
        polarity_at_comparison="inverted",
        venue="kalshi",
    )

    _insert_comparison(
        conn,
        comparison_id="cmp-poly",
        cycle_id=cycle_id,
        class_id="cls-A",
        condition_id="cond-1",
        venue="polymarket",
    )
    _insert_resolution(
        conn,
        comparison_id="cmp-poly",
        condition_id="cond-1",
        resolution_ts=prediction_ts + timedelta(days=10),
        polarity_at_comparison="aligned",
        venue="polymarket",
    )

    polarity_value, source = polarity.resolve(
        conn,
        prediction_ts=prediction_ts,
        condition_id="cond-1",
        class_id="cls-A",
        venue="polymarket",
    )

    assert polarity_value == "aligned"
    assert source == polarity.SOURCE_COMPARISON_RESOLUTIONS


def test_tier_1_filters_resolution_ts_at_or_before_prediction_ts(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Tier 1 only considers ``resolution_ts > prediction_ts`` (strict)."""
    cycle_id = _seed_cycle(conn)
    prediction_ts = datetime(2025, 5, 1, tzinfo=UTC)

    _insert_comparison(
        conn,
        comparison_id="cmp-equal",
        cycle_id=cycle_id,
        class_id="cls-A",
        condition_id="cond-1",
    )
    # Equal timestamp must NOT match (strict greater-than).
    _insert_resolution(
        conn,
        comparison_id="cmp-equal",
        condition_id="cond-1",
        resolution_ts=prediction_ts,
        polarity_at_comparison="inverted",
    )
    _insert_mapping(
        conn,
        mapping_id="map-fb",
        class_id="cls-A",
        condition_id="cond-1",
        polarity_value="aligned",
    )

    polarity_value, source = polarity.resolve(
        conn,
        prediction_ts=prediction_ts,
        condition_id="cond-1",
        class_id="cls-A",
    )

    # Tier 1 fails (strict inequality), Tier 2 wins.
    assert polarity_value == "aligned"
    assert source == polarity.SOURCE_CURRENT_MAPPING_FALLBACK


# ---------------------------------------------------------------------------
# Tier 2 — class_market_mappings fallback
# ---------------------------------------------------------------------------


def test_tier_2_hit_when_no_comparison_resolution(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """No ``comparison_resolutions`` row → Tier 2 returns the active mapping."""
    _insert_mapping(
        conn,
        mapping_id="map-1",
        class_id="cls-A",
        condition_id="cond-1",
        polarity_value="inverted",
    )

    polarity_value, source = polarity.resolve(
        conn,
        prediction_ts=datetime(2025, 5, 1, tzinfo=UTC),
        condition_id="cond-1",
        class_id="cls-A",
    )

    assert polarity_value == "inverted"
    assert source == polarity.SOURCE_CURRENT_MAPPING_FALLBACK


def test_tier_2_excludes_removed_at_not_null(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A soft-deleted mapping must not satisfy Tier 2."""
    _insert_mapping(
        conn,
        mapping_id="map-removed",
        class_id="cls-A",
        condition_id="cond-1",
        polarity_value="aligned",
        removed_at=datetime(2025, 4, 1, tzinfo=UTC),
    )

    with pytest.raises(NoPolarityError) as excinfo:
        polarity.resolve(
            conn,
            prediction_ts=datetime(2025, 5, 1, tzinfo=UTC),
            condition_id="cond-1",
            class_id="cls-A",
        )

    assert excinfo.value.condition_id == "cond-1"
    assert excinfo.value.class_id == "cls-A"
    assert excinfo.value.venue == "polymarket"


def test_tier_2_filters_cross_venue_collision(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Tier 2 venue filter blocks a Kalshi mapping from a Polymarket query."""
    _insert_mapping(
        conn,
        mapping_id="map-kalshi",
        class_id="cls-A",
        condition_id="cond-1",
        polarity_value="inverted",
        venue="kalshi",
    )
    _insert_mapping(
        conn,
        mapping_id="map-poly",
        class_id="cls-A",
        condition_id="cond-1",
        polarity_value="aligned",
        venue="polymarket",
    )

    polarity_value, source = polarity.resolve(
        conn,
        prediction_ts=datetime(2025, 5, 1, tzinfo=UTC),
        condition_id="cond-1",
        class_id="cls-A",
        venue="polymarket",
    )

    assert polarity_value == "aligned"
    assert source == polarity.SOURCE_CURRENT_MAPPING_FALLBACK


# ---------------------------------------------------------------------------
# Tier 3 — exhaustion
# ---------------------------------------------------------------------------


def test_tier_3_raises_no_polarity_error_when_neither_matches(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Empty Tier 1 and empty Tier 2 → :class:`NoPolarityError`."""
    prediction_ts = datetime(2025, 5, 1, tzinfo=UTC)
    with pytest.raises(NoPolarityError) as excinfo:
        polarity.resolve(
            conn,
            prediction_ts=prediction_ts,
            condition_id="cond-missing",
            class_id="cls-Z",
            venue="polymarket",
        )

    err = excinfo.value
    assert err.prediction_ts == prediction_ts
    assert err.condition_id == "cond-missing"
    assert err.class_id == "cls-Z"
    assert err.venue == "polymarket"
    # The default message must mention the offending identifiers so log
    # aggregators surface them without bespoke formatting.
    assert "cond-missing" in err.message
    assert "cls-Z" in err.message


def test_tier_1_preferred_over_tier_2_when_both_hit(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Tier 1 always wins when a valid comparison_resolution row exists."""
    cycle_id = _seed_cycle(conn)
    _insert_comparison(
        conn,
        comparison_id="cmp-1",
        cycle_id=cycle_id,
        class_id="cls-A",
        condition_id="cond-1",
    )
    _insert_resolution(
        conn,
        comparison_id="cmp-1",
        condition_id="cond-1",
        resolution_ts=datetime(2025, 6, 1, tzinfo=UTC),
        polarity_at_comparison="aligned",
    )
    # A contradictory active mapping; if Tier 2 ran first the
    # caller would receive 'inverted' / current_mapping_fallback.
    _insert_mapping(
        conn,
        mapping_id="map-1",
        class_id="cls-A",
        condition_id="cond-1",
        polarity_value="inverted",
    )

    polarity_value, source = polarity.resolve(
        conn,
        prediction_ts=datetime(2025, 5, 1, tzinfo=UTC),
        condition_id="cond-1",
        class_id="cls-A",
    )

    assert polarity_value == "aligned"
    assert source == polarity.SOURCE_COMPARISON_RESOLUTIONS
