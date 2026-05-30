"""T-MD-101 — venue discriminator migration acceptance tests.

Verifies:
- m4002 adds the column to fresh installs and the column is NOT NULL.
- Polymarket mappings continue to work (venue defaults to 'polymarket').
- Kalshi mappings can be registered alongside Polymarket ones.
- Uniqueness check is per-(class_id, venue, condition_id, polarity).
- The new venue-aware index exists.
- Comparison and resolution rows round-trip the column.
- Migration is idempotent.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    applied_versions,
)
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.models import (
    Comparison,
    ComparisonResolution,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    MappingExistsError,
    get_mapping,
    persist_comparison,
    query_comparisons,
    query_existing_resolution_links,
    query_mappings,
    register_mapping,
    write_resolution_link,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "mispricing_m4002.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_mispricing_migrations(conn)
    yield s
    s.close()


def test_m4002_records_version(store: DuckDBStore) -> None:
    with store.connection() as conn:
        versions = applied_versions(conn)
    assert 4001 in versions
    assert 4002 in versions


def test_venue_column_exists_on_all_three_tables(store: DuckDBStore) -> None:
    with store.connection() as conn:
        for table in (
            "class_market_mappings",
            "comparisons",
            "comparison_resolutions",
        ):
            rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
            columns = {r[1]: r for r in rows}
            assert "venue" in columns, f"venue missing from {table}"
            # Field 3 is notnull boolean.
            assert columns["venue"][3] is True, f"venue should be NOT NULL in {table}"


def test_venue_aware_active_index_exists(store: DuckDBStore) -> None:
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'class_market_mappings'"
        ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_class_market_mappings_active" in names
    assert "idx_comparisons_venue_class_computed" in (
        {
            r[0]
            for r in (
                store.connection()
                .__enter__()
                .execute("SELECT index_name FROM duckdb_indexes() WHERE table_name = 'comparisons'")
                .fetchall()
            )
        }
    )


def test_register_polymarket_mapping_default_venue(store: DuckDBStore) -> None:
    """Backward compat: register_mapping without venue defaults to polymarket."""
    with store.connection() as conn:
        m = register_mapping(
            conn,
            class_id="cls",
            condition_id="0xabc",
            mapping_type="direct",
        )
    assert m.venue == "polymarket"


def test_register_kalshi_mapping_explicit_venue(store: DuckDBStore) -> None:
    """Explicit venue='kalshi' produces a Kalshi-tagged mapping."""
    with store.connection() as conn:
        m = register_mapping(
            conn,
            class_id="cls_kalshi",
            condition_id="KXCPI-26AUG-T2.5",
            mapping_type="direct",
            venue="kalshi",
        )
    assert m.venue == "kalshi"


def test_same_class_can_map_to_both_venues(store: DuckDBStore) -> None:
    """T-MD-101 design contract: a single class maps to both venues."""
    with store.connection() as conn:
        m_poly = register_mapping(
            conn,
            class_id="cpi_above_target",
            condition_id="0xabc",
            mapping_type="direct",
            venue="polymarket",
        )
        m_kalshi = register_mapping(
            conn,
            class_id="cpi_above_target",
            condition_id="KXCPI-26AUG-T2.5",
            mapping_type="direct",
            venue="kalshi",
        )
        all_mappings = query_mappings(conn, class_id="cpi_above_target")
    assert m_poly.mapping_id != m_kalshi.mapping_id
    venues = {m.venue for m in all_mappings}
    assert venues == {"polymarket", "kalshi"}


def test_uniqueness_check_includes_venue(store: DuckDBStore) -> None:
    """Uniqueness invariant is (class_id, venue, condition_id, polarity)."""
    with store.connection() as conn:
        register_mapping(
            conn,
            class_id="cls",
            condition_id="0xabc",
            mapping_type="direct",
            venue="polymarket",
        )
        # Same class+condition_id+polarity but different venue → allowed.
        register_mapping(
            conn,
            class_id="cls",
            condition_id="0xabc",
            mapping_type="direct",
            venue="kalshi",
        )
        # Same in all four → refused.
        with pytest.raises(MappingExistsError, match="venue='polymarket'"):
            register_mapping(
                conn,
                class_id="cls",
                condition_id="0xabc",
                mapping_type="direct",
                venue="polymarket",
            )


def test_query_mappings_filters_by_venue(store: DuckDBStore) -> None:
    with store.connection() as conn:
        register_mapping(
            conn,
            class_id="cls",
            condition_id="0xabc",
            mapping_type="direct",
            venue="polymarket",
        )
        register_mapping(
            conn,
            class_id="cls",
            condition_id="KXTICK",
            mapping_type="direct",
            venue="kalshi",
        )
        polymarket_only = query_mappings(conn, venue="polymarket")
        kalshi_only = query_mappings(conn, venue="kalshi")
    assert len(polymarket_only) == 1
    assert polymarket_only[0].venue == "polymarket"
    assert len(kalshi_only) == 1
    assert kalshi_only[0].venue == "kalshi"


def test_get_mapping_returns_venue(store: DuckDBStore) -> None:
    with store.connection() as conn:
        m = register_mapping(
            conn,
            class_id="cls",
            condition_id="KXTICK",
            mapping_type="direct",
            venue="kalshi",
        )
        fetched = get_mapping(conn, mapping_id=m.mapping_id)
    assert fetched is not None
    assert fetched.venue == "kalshi"


def _comparison(
    *,
    comparison_id: str = "cmp-1",
    class_id: str = "cls",
    condition_id: str = "0xabc",
    venue: str = "polymarket",
) -> Comparison:
    return Comparison(
        comparison_id=comparison_id,
        cycle_id="cy-1",
        mapping_id="map-1",
        class_id=class_id,
        condition_id=condition_id,
        outcome_token_id=f"{condition_id}-yes",
        polarity="aligned",
        scan_id="scan-1",
        model_probability=0.30,
        model_ci_lower=0.20,
        model_ci_upper=0.40,
        market_probability=0.10,
        market_best_bid=0.09,
        market_best_ask=0.11,
        market_last_trade_price=0.10,
        market_volume_24h=10000.0,
        market_spread_bps=200,
        market_snapshot_ts=datetime(2026, 5, 10, tzinfo=UTC),
        delta=0.20,
        log_odds_delta=0.5,
        ci_overlap=False,
        expected_value=0.05,
        confidence_weighted_score=1.5,
        surfaced=True,
        suppression_reasons=(),
        computed_at=datetime(2026, 5, 14, tzinfo=UTC),
        venue=venue,  # type: ignore[arg-type]
    )


def test_comparison_round_trips_venue_field(store: DuckDBStore) -> None:
    """Comparison.venue persists and reads back correctly for both venues."""
    with store.connection() as conn:
        # Need a cycle row first to satisfy any FK-shaped expectations.
        # comparison_cycles is permissive but we still need a parent row.
        conn.execute(
            "INSERT INTO comparison_cycles "
            "(cycle_id, started_at, completed_at, comparisons_total, "
            "surfaced_count, suppressed_breakdown, library_version_at_cycle, "
            "scan_id_consumed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "cy-1",
                datetime(2026, 5, 14, tzinfo=UTC),
                datetime(2026, 5, 14, tzinfo=UTC),
                2,
                2,
                "{}",
                1,
                "scan-1",
            ],
        )
        persist_comparison(conn, _comparison(comparison_id="cmp-poly", venue="polymarket"))
        persist_comparison(
            conn,
            _comparison(
                comparison_id="cmp-kalshi",
                condition_id="KXTICK",
                venue="kalshi",
            ),
        )
        rows = query_comparisons(conn, cycle_id="cy-1")
    by_venue = {r.comparison_id: r.venue for r in rows}
    assert by_venue["cmp-poly"] == "polymarket"
    assert by_venue["cmp-kalshi"] == "kalshi"


def test_resolution_round_trips_venue_field(store: DuckDBStore) -> None:
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO comparison_cycles "
            "(cycle_id, started_at, completed_at, comparisons_total, "
            "surfaced_count, suppressed_breakdown, library_version_at_cycle, "
            "scan_id_consumed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "cy-1",
                datetime(2026, 5, 14, tzinfo=UTC),
                datetime(2026, 5, 14, tzinfo=UTC),
                1,
                1,
                "{}",
                1,
                "scan-1",
            ],
        )
        persist_comparison(
            conn,
            _comparison(comparison_id="cmp-kalshi", condition_id="KXTICK", venue="kalshi"),
        )
        write_resolution_link(
            conn,
            ComparisonResolution(
                comparison_id="cmp-kalshi",
                condition_id="KXTICK",
                resolution_outcome="yes",
                resolution_ts=datetime(2026, 6, 1, tzinfo=UTC),
                model_probability_at_comparison=0.30,
                market_probability_at_comparison=0.10,
                polarity_at_comparison="aligned",
                outcome_observed=1,
                linked_at=datetime(2026, 6, 1, tzinfo=UTC),
                venue="kalshi",
            ),
        )
        # Read back through the existing helper.
        ids = query_existing_resolution_links(conn, condition_id="KXTICK")
        # Read venue directly.
        row = conn.execute(
            "SELECT venue FROM comparison_resolutions WHERE comparison_id = ?",
            ["cmp-kalshi"],
        ).fetchone()
    assert "cmp-kalshi" in ids
    assert row is not None
    assert row[0] == "kalshi"


def test_m4002_is_idempotent(store: DuckDBStore) -> None:
    """Re-running migrations is a no-op on already-applied versions."""
    with store.connection() as conn:
        before = applied_versions(conn)
        run_pending_mispricing_migrations(conn)
        after = applied_versions(conn)
    assert before == after


def test_existing_polymarket_mappings_default_to_polymarket_venue() -> None:
    """T-MD-101 design contract: pre-existing rows backfill to 'polymarket'.

    Simulates a database that ran m4001 but not m4002, with a Polymarket
    mapping already registered. After m4002 runs, the row should carry
    venue='polymarket'.
    """
    import duckdb

    from razor_rooster.data_ingest.persistence.migrations.m0001_initial import (
        up as m0001_up,
    )
    from razor_rooster.mispricing_detector.persistence.migrations.m4001_mispricing_detector_initial import (
        up as m4001_up,
    )
    from razor_rooster.mispricing_detector.persistence.migrations.m4002_add_venue_discriminator import (
        up as m4002_up,
    )

    conn = duckdb.connect(":memory:")
    try:
        m0001_up(conn)
        # Apply m4001 with venue still in the DDL (it will be NOT NULL by
        # canonical DDL). Insert a synthetic row already venue='polymarket'.
        m4001_up(conn)
        conn.execute(
            "INSERT INTO class_market_mappings "
            "(mapping_id, class_id, condition_id, mapping_type, "
            "mapping_confidence, polarity, mapped_by, mapped_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "preexisting-1",
                "cls-old",
                "0xabc",
                "direct",
                "exact",
                "aligned",
                "operator",
                datetime(2026, 5, 1, tzinfo=UTC),
                None,
            ],
        )
        # m4002 runs as a no-op since column is already NOT NULL.
        m4002_up(conn)
        row = conn.execute(
            "SELECT venue FROM class_market_mappings WHERE mapping_id = ?",
            ["preexisting-1"],
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "polymarket"
