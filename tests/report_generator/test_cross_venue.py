"""Cross-venue disagreement section tests (supplement §1).

Verifies:
- Empty case → empty items list, friendly empty message in renderers.
- Two venues with prices differing by less than the threshold →
  not surfaced.
- Two venues with prices differing by ≥ the threshold → surfaced
  with the right per-venue breakdown and spread_bps.
- Threshold parameter is honored.
- Items ordered by spread (largest first).
- Class with only one venue → not surfaced.
- Most-recent-comparison-per-(class, venue) wins so a class with
  multiple stale comparisons doesn't trigger spurious self-disagreement.
- Renderers produce non-empty body when items present.
- Renderers pass the imperative-language linter.
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
    cross_venue,
)
from razor_rooster.report_generator.models import SectionContent
from razor_rooster.report_generator.renderer import markdown, terminal
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "rg_cross_venue.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_polymarket_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        yield c
    store.close()


# -- seed helpers ---------------------------------------------------------


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
    """Insert a comparison_cycles parent row so comparisons can FK to it."""
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


def _seed_comparison(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    class_id: str,
    venue: str,
    condition_id: str,
    market_probability: float,
    model_probability: float = 0.30,
    market_volume_24h: float = 10_000.0,
    computed_at: datetime | None = None,
) -> None:
    """Insert a comparison row with a configurable venue + market_p.

    Uses the schema's defaults for unrelated columns so the test
    surface stays focused on cross-venue logic.
    """
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
            model_probability,
            model_probability - 0.10,
            model_probability + 0.10,
            market_probability,
            market_probability - 0.005,
            market_probability + 0.005,
            market_probability,
            market_volume_24h,
            100,
            datetime(2026, 5, 10, tzinfo=UTC),
            model_probability - market_probability,
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
            False,
            False,
            when,
            venue,
        ],
    )


# -- assembler tests ------------------------------------------------------


def test_empty_when_no_comparisons(conn: duckdb.DuckDBPyConnection) -> None:
    out = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert out["type"] == "cross_venue"
    assert out["items"] == []


def test_single_venue_class_not_surfaced(conn: duckdb.DuckDBPyConnection) -> None:
    """A class mapped to only one venue produces no cross-venue item."""
    _seed_class(conn, class_id="cls-solo")
    _seed_comparison(
        conn,
        comparison_id="c-solo-1",
        class_id="cls-solo",
        venue="polymarket",
        condition_id="0xSOLO",
        market_probability=0.30,
    )
    out = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert out["items"] == []


def test_below_threshold_not_surfaced(conn: duckdb.DuckDBPyConnection) -> None:
    """Two venues with a 3-pp spread (< 5pp default) → not surfaced."""
    _seed_class(conn, class_id="cls-narrow")
    _seed_comparison(
        conn,
        comparison_id="c-narrow-poly",
        class_id="cls-narrow",
        venue="polymarket",
        condition_id="0xNARROW",
        market_probability=0.30,
    )
    _seed_comparison(
        conn,
        comparison_id="c-narrow-kalshi",
        class_id="cls-narrow",
        venue="kalshi",
        condition_id="KX-NARROW",
        market_probability=0.33,  # 3pp delta → 300 bps; below 500 default
    )
    out = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert out["items"] == []


def test_above_threshold_surfaced(conn: duckdb.DuckDBPyConnection) -> None:
    """A 30-pp spread between Polymarket and Kalshi gets surfaced."""
    _seed_class(conn, class_id="cls-wide", title="Wide Disagreement")
    _seed_comparison(
        conn,
        comparison_id="c-wide-poly",
        class_id="cls-wide",
        venue="polymarket",
        condition_id="0xWIDE",
        market_probability=0.30,
    )
    _seed_comparison(
        conn,
        comparison_id="c-wide-kalshi",
        class_id="cls-wide",
        venue="kalshi",
        condition_id="KX-WIDE",
        market_probability=0.60,
    )
    out = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    items = out["items"]
    assert len(items) == 1
    item = items[0]
    assert item["class_id"] == "cls-wide"
    assert item["class_title"] == "Wide Disagreement"
    assert item["spread_bps"] == 3000  # 30 percentage points
    assert item["max_market_p"] == pytest.approx(0.60)
    assert item["min_market_p"] == pytest.approx(0.30)
    venue_prices = item["venue_prices"]
    assert {vp["venue"] for vp in venue_prices} == {"polymarket", "kalshi"}
    # Sorted alphabetically: kalshi before polymarket.
    assert [vp["venue"] for vp in venue_prices] == ["kalshi", "polymarket"]


def test_custom_threshold(conn: duckdb.DuckDBPyConnection) -> None:
    """Tighter threshold surfaces a previously-below-threshold class."""
    _seed_class(conn, class_id="cls-medium")
    _seed_comparison(
        conn,
        comparison_id="c-medium-poly",
        class_id="cls-medium",
        venue="polymarket",
        condition_id="0xMED",
        market_probability=0.30,
    )
    _seed_comparison(
        conn,
        comparison_id="c-medium-kalshi",
        class_id="cls-medium",
        venue="kalshi",
        condition_id="KX-MED",
        market_probability=0.33,
    )
    out_default = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    out_strict = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
        spread_threshold_bps=200,
    )
    assert out_default["items"] == []
    assert len(out_strict["items"]) == 1


def test_items_ordered_by_spread_descending(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Largest disagreement renders first."""
    _seed_class(conn, class_id="cls-mid")
    _seed_class(conn, class_id="cls-huge")
    _seed_comparison(
        conn,
        comparison_id="c-mid-p",
        class_id="cls-mid",
        venue="polymarket",
        condition_id="0xMID",
        market_probability=0.30,
    )
    _seed_comparison(
        conn,
        comparison_id="c-mid-k",
        class_id="cls-mid",
        venue="kalshi",
        condition_id="KX-MID",
        market_probability=0.40,  # 1000 bps
    )
    _seed_comparison(
        conn,
        comparison_id="c-huge-p",
        class_id="cls-huge",
        venue="polymarket",
        condition_id="0xHUGE",
        market_probability=0.20,
    )
    _seed_comparison(
        conn,
        comparison_id="c-huge-k",
        class_id="cls-huge",
        venue="kalshi",
        condition_id="KX-HUGE",
        market_probability=0.70,  # 5000 bps
    )
    out = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    spreads = [item["spread_bps"] for item in out["items"]]
    assert spreads == sorted(spreads, reverse=True)
    assert out["items"][0]["class_id"] == "cls-huge"


def test_most_recent_comparison_per_class_venue_wins(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Old comparisons for the same (class, venue) shouldn't double-count."""
    _seed_class(conn, class_id="cls-stale")
    # Earlier comparison on Polymarket — would create a "self-disagreement"
    # with the later Polymarket row if both were considered.
    _seed_comparison(
        conn,
        comparison_id="c-stale-poly-old",
        class_id="cls-stale",
        venue="polymarket",
        condition_id="0xSTALE",
        market_probability=0.10,
        computed_at=datetime(2026, 5, 14, 8, tzinfo=UTC),
    )
    _seed_comparison(
        conn,
        comparison_id="c-stale-poly-new",
        class_id="cls-stale",
        venue="polymarket",
        condition_id="0xSTALE",
        market_probability=0.30,
        computed_at=datetime(2026, 5, 14, 12, tzinfo=UTC),
    )
    _seed_comparison(
        conn,
        comparison_id="c-stale-kalshi",
        class_id="cls-stale",
        venue="kalshi",
        condition_id="KX-STALE",
        market_probability=0.32,
        computed_at=datetime(2026, 5, 14, 12, tzinfo=UTC),
    )
    out = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    # 0.30 vs 0.32 = 200 bps, below 500 default → should NOT surface.
    # If the old 0.10 row leaked through, we'd see ~2200 bps and it would.
    assert out["items"] == []


def test_three_venues_uses_max_spread(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """When >2 venues exist (forward-compat), the spread is max - min."""
    _seed_class(conn, class_id="cls-trio")
    for venue, condition_id, market_p in (
        ("polymarket", "0xT1", 0.20),
        ("kalshi", "KX-T1", 0.50),
        # A hypothetical third venue. Kept here for forward-compat;
        # nothing in v1.1 actually emits 'sx' rows.
        ("sx", "SX-T1", 0.80),
    ):
        _seed_comparison(
            conn,
            comparison_id=f"c-trio-{venue}",
            class_id="cls-trio",
            venue=venue,
            condition_id=condition_id,
            market_probability=market_p,
        )
    out = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert len(out["items"]) == 1
    assert out["items"][0]["spread_bps"] == 6000  # 80 - 20 = 60pp


# -- renderer tests -------------------------------------------------------


def _baseline_header() -> dict[str, Any]:
    return {
        "cycle_date": "2026-05-15",
        "since_ts": datetime(2026, 5, 14, tzinfo=UTC),
        "until_ts": datetime(2026, 5, 15, tzinfo=UTC),
        "library_version": 7,
        "library_age_days": 1,
        "stale_source_count": 0,
        "disabled_sections": (),
        "report_id": "rpt-cv",
    }


def _baseline_footer() -> dict[str, Any]:
    return {
        "disclaimer_text": "Decision-support only.",
        "completed_at": datetime(2026, 5, 15, 14, tzinfo=UTC),
        "system_version": "0.1.0",
        "report_id": "rpt-cv",
    }


def _baseline_cross_venue_section(*, items: list[dict[str, Any]]) -> SectionContent:
    return SectionContent(
        name="cross_venue",
        content={
            "type": "cross_venue",
            "spread_threshold_bps": 500,
            "items": items,
        },
    )


def _make_item(
    *,
    class_id: str = "cls-x",
    class_title: str = "Test Class",
    sector: str = "macro_us",
    poly_p: float = 0.30,
    kalshi_p: float = 0.60,
) -> dict[str, Any]:
    venue_prices = [
        {
            "venue": "kalshi",
            "comparison_id": f"c-{class_id}-k",
            "condition_id": "KX-X",
            "market_probability": kalshi_p,
            "market_volume_24h": 5_000.0,
            "market_spread_bps": 100,
            "market_snapshot_ts": datetime(2026, 5, 14, 12, tzinfo=UTC),
            "model_probability": 0.40,
            "model_ci": (0.30, 0.50),
        },
        {
            "venue": "polymarket",
            "comparison_id": f"c-{class_id}-p",
            "condition_id": "0xX",
            "market_probability": poly_p,
            "market_volume_24h": 12_000.0,
            "market_spread_bps": 80,
            "market_snapshot_ts": datetime(2026, 5, 14, 12, tzinfo=UTC),
            "model_probability": 0.40,
            "model_ci": (0.30, 0.50),
        },
    ]
    spread_bps = round(abs(poly_p - kalshi_p) * 10_000)
    return {
        "class_id": class_id,
        "class_title": class_title,
        "domain_sector": sector,
        "venue_prices": venue_prices,
        "spread_bps": spread_bps,
        "max_market_p": max(poly_p, kalshi_p),
        "min_market_p": min(poly_p, kalshi_p),
    }


def test_terminal_renderer_empty_message() -> None:
    section = _baseline_cross_venue_section(items=[])
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "CROSS-VENUE DISAGREEMENTS" in text
    assert "No cross-venue disagreements this cycle" in text


def test_terminal_renderer_renders_item() -> None:
    item = _make_item(class_title="CPI Above Target", poly_p=0.30, kalshi_p=0.60)
    section = _baseline_cross_venue_section(items=[item])
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "CROSS-VENUE DISAGREEMENTS" in text
    assert "CPI Above Target" in text
    assert "polymarket" in text
    assert "kalshi" in text
    assert "0.300" in text
    assert "0.600" in text
    assert "spread: 3000 bps" in text


def test_markdown_renderer_renders_item() -> None:
    item = _make_item(class_title="CPI Above Target", poly_p=0.30, kalshi_p=0.60)
    section = _baseline_cross_venue_section(items=[item])
    md = markdown.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "## Cross-Venue Disagreements" in md
    assert "CPI Above Target" in md
    assert "`polymarket`" in md
    assert "`kalshi`" in md
    # Table row asserts the GFM rendering is correct.
    assert "| Class | Sector | Spread (bps) | Consensus | Venue prices |" in md


def test_markdown_renderer_empty_message() -> None:
    section = _baseline_cross_venue_section(items=[])
    md = markdown.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "## Cross-Venue Disagreements" in md
    assert "No cross-venue disagreements this cycle" in md


def test_terminal_renderer_passes_linter() -> None:
    """The cross-venue section must not introduce imperative phrasing."""
    item = _make_item()
    section = _baseline_cross_venue_section(items=[item])
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    check_text(text)


def test_markdown_renderer_passes_linter() -> None:
    item = _make_item()
    section = _baseline_cross_venue_section(items=[item])
    md = markdown.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    check_text(md)
