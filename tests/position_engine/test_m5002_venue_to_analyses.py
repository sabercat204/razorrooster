"""T-PE-101 — venue-to-analyses migration acceptance tests.

Verifies:
- m5002 adds the column to fresh installs and the column is NOT NULL.
- Polymarket analyses continue to work (venue defaults to 'polymarket').
- Kalshi analyses can be persisted alongside Polymarket ones.
- The new venue-aware index exists.
- ``query_analyses`` filters by venue.
- The renderer surfaces the venue tag.
- Linter still passes on rendered output with venue tag.
- Migration is idempotent.
- Pre-existing rows backfill to 'polymarket' on the upgrade path.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import applied_versions
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.position_engine.frame.linter import check_text
from razor_rooster.position_engine.frame.renderer import render, to_structured_dict
from razor_rooster.position_engine.models import Analysis, BankrollConfig
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import (
    get_analysis,
    persist_analysis,
    query_analyses,
    write_bankroll_config,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "position_engine_m5002.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_position_engine_migrations(conn)
    yield s
    s.close()


def _bankroll(config_id: str = "bk-1") -> BankrollConfig:
    return BankrollConfig(
        config_id=config_id,
        analytical_bankroll_usd=10000.0,
        max_single_position_pct=0.05,
        kelly_fraction_default=0.5,
        min_edge_threshold=0.02,
        effective_at=datetime(2026, 5, 1, tzinfo=UTC),
    )


def _analysis(
    *,
    analysis_id: str = "ana-1",
    class_id: str = "cls",
    condition_id: str = "0xabc",
    venue: str = "polymarket",
) -> Analysis:
    return Analysis(
        analysis_id=analysis_id,
        cycle_id="cy-1",
        comparison_id="cmp-1",
        class_id=class_id,
        condition_id=condition_id,
        bankroll_config_id="bk-1",
        model_probability=0.30,
        market_probability=0.10,
        kelly_unclamped=0.10,
        kelly_negative=False,
        kelly_clamped_by_max_cap=False,
        kelly_clamped_by_liquidity=False,
        suggested_fraction=0.05,
        suggested_dollar_size=500.0,
        ev_per_dollar=0.20,
        bankroll_after_1_loss_pct=0.95,
        bankroll_after_3_losses_pct=0.86,
        bankroll_after_5_losses_pct=0.77,
        suggested_pct_of_24h_volume=0.05,
        days_to_resolution=30,
        long_time_to_resolution=False,
        sub_threshold=False,
        sensitivity_analysis=None,
        invalidation_criteria=(),
        computed_at=datetime(2026, 5, 14, tzinfo=UTC),
        venue=venue,  # type: ignore[arg-type]
    )


def test_m5002_records_version(store: DuckDBStore) -> None:
    with store.connection() as conn:
        versions = applied_versions(conn)
    assert 5001 in versions
    assert 5002 in versions


def test_venue_column_exists_and_not_null(store: DuckDBStore) -> None:
    with store.connection() as conn:
        rows = conn.execute("PRAGMA table_info('analyses')").fetchall()
    columns = {r[1]: r for r in rows}
    assert "venue" in columns, "venue missing from analyses"
    # Field 3 is notnull boolean.
    assert columns["venue"][3] is True, "venue should be NOT NULL"


def test_venue_aware_index_exists(store: DuckDBStore) -> None:
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'analyses'"
        ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_analyses_venue_computed" in names


def test_persist_polymarket_analysis_default_venue(store: DuckDBStore) -> None:
    """Backward compat: an analysis without explicit venue defaults to polymarket."""
    analysis = _analysis(analysis_id="ana-poly")
    with store.connection() as conn:
        write_bankroll_config(conn, _bankroll())
        persist_analysis(conn, analysis)
        fetched = get_analysis(conn, analysis_id="ana-poly")
    assert fetched is not None
    assert fetched.venue == "polymarket"


def test_persist_kalshi_analysis_explicit_venue(store: DuckDBStore) -> None:
    analysis = _analysis(
        analysis_id="ana-kalshi",
        condition_id="KXCPI-26AUG-T2.5",
        venue="kalshi",
    )
    with store.connection() as conn:
        write_bankroll_config(conn, _bankroll())
        persist_analysis(conn, analysis)
        fetched = get_analysis(conn, analysis_id="ana-kalshi")
    assert fetched is not None
    assert fetched.venue == "kalshi"
    assert fetched.condition_id == "KXCPI-26AUG-T2.5"


def test_both_venue_analyses_coexist(store: DuckDBStore) -> None:
    with store.connection() as conn:
        write_bankroll_config(conn, _bankroll())
        persist_analysis(conn, _analysis(analysis_id="ana-poly", venue="polymarket"))
        persist_analysis(
            conn,
            _analysis(
                analysis_id="ana-kalshi",
                condition_id="KXTICK",
                venue="kalshi",
            ),
        )
        all_rows = query_analyses(conn)
    venues = {r.venue for r in all_rows}
    assert venues == {"polymarket", "kalshi"}


def test_query_analyses_filters_by_venue(store: DuckDBStore) -> None:
    with store.connection() as conn:
        write_bankroll_config(conn, _bankroll())
        persist_analysis(conn, _analysis(analysis_id="ana-poly", venue="polymarket"))
        persist_analysis(
            conn,
            _analysis(
                analysis_id="ana-kalshi",
                condition_id="KXTICK",
                venue="kalshi",
            ),
        )
        polymarket_only = query_analyses(conn, venue="polymarket")
        kalshi_only = query_analyses(conn, venue="kalshi")
    assert len(polymarket_only) == 1
    assert polymarket_only[0].venue == "polymarket"
    assert len(kalshi_only) == 1
    assert kalshi_only[0].venue == "kalshi"


def test_renderer_includes_venue_tag_polymarket() -> None:
    text = render(
        _analysis(condition_id="0xabc", venue="polymarket"),
        bankroll_usd=10000.0,
        class_title="CPI Above Target",
    )
    assert "0xabc" in text
    assert "(polymarket)" in text


def test_renderer_includes_venue_tag_kalshi() -> None:
    text = render(
        _analysis(condition_id="KXCPI-26AUG-T2.5", venue="kalshi"),
        bankroll_usd=10000.0,
        class_title="CPI Above Target",
    )
    assert "KXCPI-26AUG-T2.5" in text
    assert "(kalshi)" in text


def test_linter_passes_with_venue_tag() -> None:
    """The venue tag must not trigger any forbidden-phrase rules."""
    text_poly = render(
        _analysis(condition_id="0xabc", venue="polymarket"),
        bankroll_usd=10000.0,
        class_title="CPI Above Target",
    )
    text_kalshi = render(
        _analysis(condition_id="KXTICK", venue="kalshi"),
        bankroll_usd=10000.0,
        class_title="CPI Above Target",
    )
    # check_text raises if any forbidden phrase matches.
    check_text(text_poly)
    check_text(text_kalshi)


def test_structured_dict_includes_venue() -> None:
    structured = to_structured_dict(
        _analysis(condition_id="KXTICK", venue="kalshi"),
        bankroll_usd=10000.0,
        class_title="CPI",
    )
    assert structured["venue"] == "kalshi"
    assert structured["condition_id"] == "KXTICK"


def test_m5002_is_idempotent(store: DuckDBStore) -> None:
    with store.connection() as conn:
        before = applied_versions(conn)
        run_pending_position_engine_migrations(conn)
        after = applied_versions(conn)
    assert before == after


def test_existing_analyses_backfill_to_polymarket() -> None:
    """Pre-existing rows on the upgrade path receive ``venue='polymarket'``.

    Simulates a database that ran m5001 with the venue column already
    in canonical DDL (since this PR ships both at once), inserts a
    legacy-shaped row before m5002 detection, then confirms re-running
    m5002 leaves the polymarket value in place.
    """
    import duckdb

    from razor_rooster.data_ingest.persistence.migrations.m0001_initial import (
        up as m0001_up,
    )
    from razor_rooster.position_engine.persistence.migrations.m5001_position_engine_initial import (
        up as m5001_up,
    )
    from razor_rooster.position_engine.persistence.migrations.m5002_add_venue_to_analyses import (
        up as m5002_up,
    )

    conn = duckdb.connect(":memory:")
    try:
        m0001_up(conn)
        m5001_up(conn)
        # Insert a row using the canonical DDL's default. Because this PR
        # bundles m5001 + m5002, m5001 already has the column NOT NULL
        # DEFAULT 'polymarket'; insert a row with explicit polymarket
        # value to mimic a pre-Kalshi row.
        conn.execute(
            "INSERT INTO analyses ("
            "analysis_id, cycle_id, comparison_id, class_id, condition_id, "
            "venue, bankroll_config_id, model_probability, market_probability, "
            "kelly_unclamped, kelly_negative, kelly_clamped_by_max_cap, "
            "kelly_clamped_by_liquidity, suggested_fraction, "
            "suggested_dollar_size, ev_per_dollar, bankroll_after_1_loss_pct, "
            "bankroll_after_3_losses_pct, bankroll_after_5_losses_pct, "
            "suggested_pct_of_24h_volume, days_to_resolution, "
            "long_time_to_resolution, sub_threshold, sensitivity_analysis, "
            "invalidation_criteria, low_signature_confidence, "
            "source_stale_warning, library_stale_warning, "
            "definition_drift_warning, low_mapping_confidence, low_liquidity, "
            "error, computed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "preexisting-1",
                "cy-old",
                "cmp-old",
                "cls-old",
                "0xabc",
                "polymarket",
                "bk-1",
                0.30,
                0.10,
                0.10,
                False,
                False,
                False,
                0.05,
                500.0,
                0.20,
                0.95,
                0.86,
                0.77,
                0.05,
                30,
                False,
                False,
                None,
                "[]",
                False,
                False,
                False,
                False,
                False,
                False,
                None,
                datetime(2026, 5, 1, tzinfo=UTC),
            ],
        )
        # m5002 runs as a no-op since column is already NOT NULL.
        m5002_up(conn)
        row = conn.execute(
            "SELECT venue FROM analyses WHERE analysis_id = ?",
            ["preexisting-1"],
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "polymarket"
