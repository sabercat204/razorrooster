"""Liquidity-weighted cross-venue consensus tests (supplement §1).

Verifies:
- Consensus is the volume-weighted average when both venues report volume.
- Tie volumes produce the simple average.
- Heavily skewed volume pulls the consensus toward the bigger venue.
- All-NULL volumes fall back to the unweighted mean.
- Empty volumes (zero on both) fall back to the unweighted mean.
- Total 24h volume is reported alongside the consensus.
- Renderers display the consensus and total volume.
- Renderers fall back to the "unweighted" message when volume is missing.
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
    db_path = tmp_path / "rg_consensus.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_polymarket_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        yield c
    store.close()


# -- seed helpers (re-uses the cross_venue test pattern) -----------------


def _seed_class(conn: duckdb.DuckDBPyConnection, *, class_id: str) -> None:
    conn.execute(
        "INSERT INTO pl_event_classes "
        "(class_id, title, description, domain_sector, secondary_sectors, "
        "definition_version, outcome_type, registered_at, "
        "last_evaluated_at, library_version_at_last_eval, removed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            class_id,
            class_id,
            "—",
            "macro_us",
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


def _seed_comparison(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    class_id: str,
    venue: str,
    condition_id: str,
    market_probability: float,
    market_volume_24h: float | None,
) -> None:
    _seed_cycle(conn)
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
            datetime(2026, 5, 14, 12, tzinfo=UTC),
            venue,
        ],
    )


# -- assembler tests -----------------------------------------------------


def test_consensus_volume_weighted(conn: duckdb.DuckDBPyConnection) -> None:
    """50/50 volume → consensus is the simple average of the two prices."""
    _seed_class(conn, class_id="cls-eq")
    _seed_comparison(
        conn,
        comparison_id="c-eq-p",
        class_id="cls-eq",
        venue="polymarket",
        condition_id="0xEQ",
        market_probability=0.30,
        market_volume_24h=10_000.0,
    )
    _seed_comparison(
        conn,
        comparison_id="c-eq-k",
        class_id="cls-eq",
        venue="kalshi",
        condition_id="KX-EQ",
        market_probability=0.50,
        market_volume_24h=10_000.0,
    )
    out = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    item = out["items"][0]
    assert item["consensus_market_p"] == pytest.approx(0.40)
    assert item["total_volume_24h"] == pytest.approx(20_000.0)


def test_consensus_skewed_toward_bigger_venue(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """90/10 split pulls the consensus toward the high-volume side."""
    _seed_class(conn, class_id="cls-skew")
    _seed_comparison(
        conn,
        comparison_id="c-skew-p",
        class_id="cls-skew",
        venue="polymarket",
        condition_id="0xSK",
        market_probability=0.30,
        market_volume_24h=90_000.0,
    )
    _seed_comparison(
        conn,
        comparison_id="c-skew-k",
        class_id="cls-skew",
        venue="kalshi",
        condition_id="KX-SK",
        market_probability=0.60,
        market_volume_24h=10_000.0,
    )
    out = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    item = out["items"][0]
    # Weighted: (0.30 * 0.90) + (0.60 * 0.10) = 0.27 + 0.06 = 0.33.
    assert item["consensus_market_p"] == pytest.approx(0.33)
    assert item["total_volume_24h"] == pytest.approx(100_000.0)


def test_all_null_volumes_fallback_to_unweighted_mean(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """If neither venue reports volume, the mean is unweighted."""
    _seed_class(conn, class_id="cls-no-vol")
    _seed_comparison(
        conn,
        comparison_id="c-nv-p",
        class_id="cls-no-vol",
        venue="polymarket",
        condition_id="0xNV",
        market_probability=0.30,
        market_volume_24h=None,
    )
    _seed_comparison(
        conn,
        comparison_id="c-nv-k",
        class_id="cls-no-vol",
        venue="kalshi",
        condition_id="KX-NV",
        market_probability=0.50,
        market_volume_24h=None,
    )
    out = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    item = out["items"][0]
    assert item["consensus_market_p"] == pytest.approx(0.40)
    assert item["total_volume_24h"] is None


def test_zero_volumes_fallback_to_unweighted_mean(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """All-zero volumes use the unweighted-mean fallback."""
    _seed_class(conn, class_id="cls-zero-vol")
    _seed_comparison(
        conn,
        comparison_id="c-zv-p",
        class_id="cls-zero-vol",
        venue="polymarket",
        condition_id="0xZV",
        market_probability=0.30,
        market_volume_24h=0.0,
    )
    _seed_comparison(
        conn,
        comparison_id="c-zv-k",
        class_id="cls-zero-vol",
        venue="kalshi",
        condition_id="KX-ZV",
        market_probability=0.50,
        market_volume_24h=0.0,
    )
    out = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    item = out["items"][0]
    assert item["consensus_market_p"] == pytest.approx(0.40)
    assert item["total_volume_24h"] is None


def test_partial_volume_uses_present_weights(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """If only one venue has volume, only that venue's price counts toward
    the weighted consensus.
    """
    _seed_class(conn, class_id="cls-partial")
    _seed_comparison(
        conn,
        comparison_id="c-pp-p",
        class_id="cls-partial",
        venue="polymarket",
        condition_id="0xPP",
        market_probability=0.30,
        market_volume_24h=10_000.0,
    )
    _seed_comparison(
        conn,
        comparison_id="c-pp-k",
        class_id="cls-partial",
        venue="kalshi",
        condition_id="KX-PP",
        market_probability=0.80,
        market_volume_24h=None,
    )
    out = cross_venue.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    item = out["items"][0]
    # Only Polymarket has volume, so the weighted consensus equals
    # Polymarket's price exactly.
    assert item["consensus_market_p"] == pytest.approx(0.30)
    assert item["total_volume_24h"] == pytest.approx(10_000.0)


# -- renderer tests ------------------------------------------------------


def _baseline_header() -> dict[str, Any]:
    return {
        "cycle_date": "2026-05-15",
        "since_ts": datetime(2026, 5, 14, tzinfo=UTC),
        "until_ts": datetime(2026, 5, 15, tzinfo=UTC),
        "library_version": 7,
        "library_age_days": 1,
        "stale_source_count": 0,
        "disabled_sections": (),
        "report_id": "rpt-cs",
    }


def _baseline_footer() -> dict[str, Any]:
    return {
        "disclaimer_text": "Decision-support only.",
        "completed_at": datetime(2026, 5, 15, 14, tzinfo=UTC),
        "system_version": "0.1.0",
        "report_id": "rpt-cs",
    }


def _make_section(
    *,
    consensus_p: float | None,
    total_vol: float | None,
) -> SectionContent:
    venue_prices = [
        {
            "venue": "kalshi",
            "comparison_id": "c-k",
            "condition_id": "KX",
            "market_probability": 0.60,
            "market_volume_24h": 5_000.0,
            "market_spread_bps": 100,
            "market_snapshot_ts": datetime(2026, 5, 14, 12, tzinfo=UTC),
            "model_probability": 0.40,
            "model_ci": (0.30, 0.50),
        },
        {
            "venue": "polymarket",
            "comparison_id": "c-p",
            "condition_id": "0xP",
            "market_probability": 0.30,
            "market_volume_24h": 15_000.0,
            "market_spread_bps": 80,
            "market_snapshot_ts": datetime(2026, 5, 14, 12, tzinfo=UTC),
            "model_probability": 0.40,
            "model_ci": (0.30, 0.50),
        },
    ]
    return SectionContent(
        name="cross_venue",
        content={
            "type": "cross_venue",
            "spread_threshold_bps": 500,
            "items": [
                {
                    "class_id": "cls-rd",
                    "class_title": "Rendered Class",
                    "domain_sector": "macro_us",
                    "venue_prices": venue_prices,
                    "spread_bps": 3000,
                    "max_market_p": 0.60,
                    "min_market_p": 0.30,
                    "consensus_market_p": consensus_p,
                    "total_volume_24h": total_vol,
                }
            ],
        },
    )


def test_terminal_renderer_shows_weighted_consensus() -> None:
    section = _make_section(consensus_p=0.375, total_vol=20_000.0)
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "liquidity-weighted consensus" in text
    assert "0.375" in text
    assert "$20,000" in text


def test_terminal_renderer_falls_back_when_no_volume() -> None:
    section = _make_section(consensus_p=0.45, total_vol=None)
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "unweighted consensus" in text
    assert "0.450" in text
    assert "no per-venue volume available" in text


def test_markdown_renderer_consensus_table_column() -> None:
    section = _make_section(consensus_p=0.375, total_vol=20_000.0)
    md = markdown.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "| Class | Sector | Spread (bps) | Consensus | Venue prices |" in md
    assert "`0.375`" in md
    assert "liquidity-weighted consensus" in md


def test_markdown_renderer_consensus_unweighted_fallback() -> None:
    section = _make_section(consensus_p=0.45, total_vol=None)
    md = markdown.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "unweighted consensus" in md
    assert "no per-venue volume available" in md
