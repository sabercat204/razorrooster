"""Single-venue-dominance warning tests (supplement §5).

When a class is mapped to multiple venues but one venue holds more
than 80% of the total 24h volume, the surfaced section appends a
``single_venue_dominance`` warning so the operator weighs the
cross-venue comparison appropriately.

Verifies:
- Single-venue classes get no warning.
- Two venues with balanced volume get no warning.
- Two venues with a 95/5 split get the warning.
- Threshold parameter is honored.
- The warning rides alongside other warnings (not replaced).
- Old comparisons for the same (class, venue) don't double-count
  toward the share calculation.
- Renderers still pass the imperative-language linter when the
  warning is present.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.position_engine.frame.linter import check_text
from razor_rooster.report_generator.engines.section_assemblers import (
    surfaced,
)
from razor_rooster.report_generator.models import SectionContent
from razor_rooster.report_generator.renderer import markdown, terminal
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "rg_dominance.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_polymarket_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        yield c
    store.close()


# -- seed helpers (same shape as test_cross_venue.py) --------------------


def _seed_class(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str,
    title: str = "Test Class",
    sector: str = "macro_us",
) -> None:
    conn.execute(
        "INSERT INTO pl_event_classes "
        "(class_id, title, description, domain_sector, secondary_sectors, "
        "definition_version, outcome_type, registered_at, "
        "last_evaluated_at, library_version_at_last_eval, removed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            class_id,
            title,
            "—",
            sector,
            None,
            1,
            "binary",
            datetime(2026, 5, 14, tzinfo=UTC),
            None,
            None,
            None,
        ],
    )


def _seed_cycle(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO comparison_cycles "
        "(cycle_id, started_at, completed_at, comparisons_total, "
        "surfaced_count, suppressed_breakdown, library_version_at_cycle, "
        "scan_id_consumed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "cy-1",
            datetime(2026, 5, 14, tzinfo=UTC),
            datetime(2026, 5, 14, tzinfo=UTC),
            0,
            0,
            "{}",
            1,
            "scan-1",
        ],
    )


def _seed_surfaced_comparison(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    class_id: str,
    venue: str,
    condition_id: str,
    market_volume_24h: float,
    market_probability: float = 0.30,
    computed_at: datetime | None = None,
    surfaced: bool = True,
) -> None:
    _seed_cycle(conn)
    when = computed_at or datetime(2026, 5, 14, 12, tzinfo=UTC)
    conn.execute(
        "INSERT INTO comparisons "
        "(comparison_id, cycle_id, mapping_id, class_id, condition_id, "
        "outcome_token_id, polarity, scan_id, model_probability, "
        "model_ci_lower, model_ci_upper, market_probability, "
        "market_best_bid, market_best_ask, market_last_trade_price, "
        "market_volume_24h, market_spread_bps, market_snapshot_ts, "
        "delta, log_odds_delta, ci_overlap, expected_value, "
        "confidence_weighted_score, surfaced, suppression_reasons, "
        "low_signature_confidence, source_stale_warning, "
        "library_stale_warning, definition_drift_warning, "
        "stale_market_price, no_market_price, degenerate_orderbook, "
        "low_liquidity, low_mapping_confidence, computed_at, venue) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            "cy-1",
            f"map-{comparison_id}",
            class_id,
            condition_id,
            f"{condition_id}-yes",
            "aligned",
            "scan-1",
            0.30,
            0.20,
            0.40,
            market_probability,
            market_probability - 0.005,
            market_probability + 0.005,
            market_probability,
            market_volume_24h,
            100,
            datetime(2026, 5, 10, tzinfo=UTC),
            0.30 - market_probability,
            0.5,
            False,
            0.05,
            1.5,
            surfaced,
            "[]",
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            when,
            venue,
        ],
    )


# -- assembler tests -----------------------------------------------------


def test_single_venue_class_no_dominance_warning(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A class with only one venue is not eligible for the warning."""
    _seed_class(conn, class_id="cls-solo")
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-solo",
        class_id="cls-solo",
        venue="polymarket",
        condition_id="0xSOLO",
        market_volume_24h=1_000_000.0,
    )
    out = surfaced.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    comparisons = out["comparisons"]
    assert len(comparisons) == 1
    assert "single_venue_dominance" not in comparisons[0]["warnings"]


def test_balanced_two_venue_no_warning(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """50/50 split between Polymarket and Kalshi → no warning."""
    _seed_class(conn, class_id="cls-balanced")
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-bal-p",
        class_id="cls-balanced",
        venue="polymarket",
        condition_id="0xBAL",
        market_volume_24h=10_000.0,
    )
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-bal-k",
        class_id="cls-balanced",
        venue="kalshi",
        condition_id="KX-BAL",
        market_volume_24h=10_000.0,
    )
    out = surfaced.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    for cmp_ in out["comparisons"]:
        assert "single_venue_dominance" not in cmp_["warnings"]


def test_dominant_venue_warning_appended(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """95/5 split → both surfaced rows for the class get the warning."""
    _seed_class(conn, class_id="cls-skewed")
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-skew-p",
        class_id="cls-skewed",
        venue="polymarket",
        condition_id="0xSKEW",
        market_volume_24h=95_000.0,
    )
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-skew-k",
        class_id="cls-skewed",
        venue="kalshi",
        condition_id="KX-SKEW",
        market_volume_24h=5_000.0,
    )
    out = surfaced.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    comparisons = out["comparisons"]
    assert len(comparisons) == 2
    for cmp_ in comparisons:
        assert "single_venue_dominance" in cmp_["warnings"]


def test_threshold_at_exactly_80_pct_no_warning(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """80/20 split is exactly at threshold → no warning (strict >).

    The threshold is "more than 80%". An 80/20 split passes the limit
    test but doesn't exceed it.
    """
    _seed_class(conn, class_id="cls-edge")
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-edge-p",
        class_id="cls-edge",
        venue="polymarket",
        condition_id="0xEDGE",
        market_volume_24h=80_000.0,
    )
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-edge-k",
        class_id="cls-edge",
        venue="kalshi",
        condition_id="KX-EDGE",
        market_volume_24h=20_000.0,
    )
    out = surfaced.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    for cmp_ in out["comparisons"]:
        assert "single_venue_dominance" not in cmp_["warnings"]


def test_threshold_just_above_80_pct_triggers_warning(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """81/19 split triggers the warning at the default threshold."""
    _seed_class(conn, class_id="cls-just-above")
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-ja-p",
        class_id="cls-just-above",
        venue="polymarket",
        condition_id="0xJA",
        market_volume_24h=81_000.0,
    )
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-ja-k",
        class_id="cls-just-above",
        venue="kalshi",
        condition_id="KX-JA",
        market_volume_24h=19_000.0,
    )
    out = surfaced.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    for cmp_ in out["comparisons"]:
        assert "single_venue_dominance" in cmp_["warnings"]


def test_custom_threshold(conn: duckdb.DuckDBPyConnection) -> None:
    """Tightening the threshold to 60% surfaces a 70/30 split."""
    _seed_class(conn, class_id="cls-tight")
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-tight-p",
        class_id="cls-tight",
        venue="polymarket",
        condition_id="0xT",
        market_volume_24h=70_000.0,
    )
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-tight-k",
        class_id="cls-tight",
        venue="kalshi",
        condition_id="KX-T",
        market_volume_24h=30_000.0,
    )
    out_default = surfaced.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    out_strict = surfaced.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
        single_venue_dominance_pct=0.60,
    )
    for cmp_ in out_default["comparisons"]:
        assert "single_venue_dominance" not in cmp_["warnings"]
    for cmp_ in out_strict["comparisons"]:
        assert "single_venue_dominance" in cmp_["warnings"]


def test_warning_coexists_with_other_warnings(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The dominance warning is appended; other flags are preserved."""
    _seed_class(conn, class_id="cls-multi")
    # Polymarket: dominant + low_liquidity flag set on the row.
    _seed_cycle(conn)
    when = datetime(2026, 5, 14, 12, tzinfo=UTC)
    conn.execute(
        "INSERT INTO comparisons "
        "(comparison_id, cycle_id, mapping_id, class_id, condition_id, "
        "outcome_token_id, polarity, scan_id, model_probability, "
        "model_ci_lower, model_ci_upper, market_probability, "
        "market_best_bid, market_best_ask, market_last_trade_price, "
        "market_volume_24h, market_spread_bps, market_snapshot_ts, "
        "delta, log_odds_delta, ci_overlap, expected_value, "
        "confidence_weighted_score, surfaced, suppression_reasons, "
        "low_signature_confidence, source_stale_warning, "
        "library_stale_warning, definition_drift_warning, "
        "stale_market_price, no_market_price, degenerate_orderbook, "
        "low_liquidity, low_mapping_confidence, computed_at, venue) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "c-multi-p",
            "cy-1",
            "map-multi-p",
            "cls-multi",
            "0xMULTI",
            "0xMULTI-yes",
            "aligned",
            "scan-1",
            0.30,
            0.20,
            0.40,
            0.30,
            0.295,
            0.305,
            0.30,
            95_000.0,
            100,
            datetime(2026, 5, 10, tzinfo=UTC),
            0.0,
            0.5,
            False,
            0.05,
            1.5,
            True,
            "[]",
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            True,  # low_liquidity flag set
            False,
            when,
            "polymarket",
        ],
    )
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-multi-k",
        class_id="cls-multi",
        venue="kalshi",
        condition_id="KX-MULTI",
        market_volume_24h=5_000.0,
    )
    out = surfaced.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    poly_cmp = next(c for c in out["comparisons"] if c["venue"] == "polymarket")
    assert "low_liquidity" in poly_cmp["warnings"]
    assert "single_venue_dominance" in poly_cmp["warnings"]


def test_old_comparison_does_not_inflate_share(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Stale comparisons for the same (class, venue) shouldn't double-count.

    An old Polymarket row with huge volume must not push the live
    Polymarket share over the threshold; only the latest row per
    (class, venue) participates in the share calculation.
    """
    _seed_class(conn, class_id="cls-stale-share")
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-stale-old",
        class_id="cls-stale-share",
        venue="polymarket",
        condition_id="0xSTALE",
        market_volume_24h=1_000_000.0,  # huge, stale
        computed_at=datetime(2026, 5, 14, 8, tzinfo=UTC),
    )
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-stale-new-p",
        class_id="cls-stale-share",
        venue="polymarket",
        condition_id="0xSTALE",
        market_volume_24h=10_000.0,
        computed_at=datetime(2026, 5, 14, 12, tzinfo=UTC),
    )
    _seed_surfaced_comparison(
        conn,
        comparison_id="c-stale-new-k",
        class_id="cls-stale-share",
        venue="kalshi",
        condition_id="KX-STALE",
        market_volume_24h=10_000.0,
        computed_at=datetime(2026, 5, 14, 12, tzinfo=UTC),
    )
    out = surfaced.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    # Latest snapshot is balanced 50/50, so no warning.
    for cmp_ in out["comparisons"]:
        if cmp_["class_id"] == "cls-stale-share":
            assert "single_venue_dominance" not in cmp_["warnings"]


# -- renderer linter compatibility ---------------------------------------


def _baseline_header() -> dict[str, Any]:
    return {
        "cycle_date": "2026-05-15",
        "since_ts": datetime(2026, 5, 14, tzinfo=UTC),
        "until_ts": datetime(2026, 5, 15, tzinfo=UTC),
        "library_version": 7,
        "library_age_days": 1,
        "stale_source_count": 0,
        "disabled_sections": (),
        "report_id": "rpt-dom",
    }


def _baseline_footer() -> dict[str, Any]:
    return {
        "disclaimer_text": "Decision-support only.",
        "completed_at": datetime(2026, 5, 15, 14, tzinfo=UTC),
        "system_version": "0.1.0",
        "report_id": "rpt-dom",
    }


def _surfaced_section_with_dominance() -> SectionContent:
    return SectionContent(
        name="surfaced",
        content={
            "type": "surfaced",
            "comparisons": [
                {
                    "comparison_id": "c-1",
                    "class_id": "cls",
                    "class_title": "Test",
                    "domain_sector": "macro_us",
                    "condition_id": "0xX",
                    "venue": "polymarket",
                    "model_p": 0.30,
                    "model_ci": (0.20, 0.40),
                    "market_p": 0.10,
                    "market_spread_bps": 100,
                    "delta": 0.20,
                    "log_odds_delta": 0.50,
                    "ev": 0.05,
                    "score": 1.5,
                    "case_for_model": ["m"],
                    "case_for_market": ["k"],
                    "ambiguity_factors": [],
                    "warnings": ["single_venue_dominance", "low_liquidity"],
                    "venue_shares": {"polymarket": 0.95, "kalshi": 0.05},
                    "scan_trace": None,
                    "comparison_trace": None,
                    "analysis": None,
                }
            ],
        },
    )


def test_terminal_renderer_passes_linter_with_dominance_warning() -> None:
    section = _surfaced_section_with_dominance()
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "single_venue_dominance" in text
    check_text(text)


def test_markdown_renderer_passes_linter_with_dominance_warning() -> None:
    section = _surfaced_section_with_dominance()
    md = markdown.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "single_venue_dominance" in md
    check_text(md)
