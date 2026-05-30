"""Per-sector Brier-score calibration tests (supplement §3).

Verifies:
- Empty case → no sector_brier_scores entries.
- A single resolved comparison contributes one entry for its sector.
- Brier math is correct for known inputs.
- Window filter excludes resolutions outside the rolling window.
- Invalidated resolutions are excluded from the score.
- The miscalibration flag fires above the threshold.
- Custom threshold parameter is honored.
- Renderers surface the table and pass the imperative-language linter.
- Compatibility: when the section has only Brier data and no fresh
  resolutions, the renderer still emits a non-empty body.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
    calibration,
)
from razor_rooster.report_generator.models import SectionContent
from razor_rooster.report_generator.renderer import markdown, terminal
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "rg_brier.duckdb"
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
    sector: str,
    title: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO pl_event_classes "
        "(class_id, title, description, domain_sector, secondary_sectors, "
        "definition_version, outcome_type, registered_at, "
        "last_evaluated_at, library_version_at_last_eval, removed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            class_id,
            title or class_id,
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


def _seed_resolved_comparison(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    class_id: str,
    model_p: float,
    outcome_observed: int,
    resolution_ts: datetime,
    resolution_outcome: str = "yes",
    venue: str = "polymarket",
    condition_id: str = "0xX",
) -> None:
    """Seed a comparison plus its corresponding comparison_resolutions row."""
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
            model_p,
            max(0.0, model_p - 0.10),
            min(1.0, model_p + 0.10),
            0.30,
            0.295,
            0.305,
            0.30,
            10_000.0,
            100,
            datetime(2026, 5, 10, tzinfo=UTC),
            model_p - 0.30,
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
            resolution_ts - timedelta(days=14),
            venue,
        ],
    )
    conn.execute(
        "INSERT INTO comparison_resolutions "
        "(comparison_id, condition_id, resolution_outcome, resolution_ts, "
        "model_probability_at_comparison, market_probability_at_comparison, "
        "polarity_at_comparison, outcome_observed, linked_at, venue) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            condition_id,
            resolution_outcome,
            resolution_ts,
            model_p,
            0.30,
            "aligned",
            outcome_observed,
            resolution_ts,
            venue,
        ],
    )


# -- assembler tests -----------------------------------------------------


def test_empty_when_no_resolutions(conn: duckdb.DuckDBPyConnection) -> None:
    out = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert out["sector_brier_scores"] == []


def test_single_resolution_well_calibrated(conn: duckdb.DuckDBPyConnection) -> None:
    """Model said 0.9, outcome was 1 → squared error 0.01 → very well-calibrated."""
    _seed_class(conn, class_id="cls-macro", sector="macroeconomic")
    _seed_resolved_comparison(
        conn,
        comparison_id="c-1",
        class_id="cls-macro",
        model_p=0.9,
        outcome_observed=1,
        resolution_ts=datetime(2026, 5, 10, tzinfo=UTC),
    )
    out = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    scores = out["sector_brier_scores"]
    assert len(scores) == 1
    entry = scores[0]
    assert entry["sector"] == "macroeconomic"
    assert entry["n_resolutions"] == 1
    assert entry["brier_score"] == pytest.approx(0.01, abs=1e-6)
    assert entry["miscalibrated"] is False


def test_single_resolution_poorly_calibrated(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Model said 0.1, outcome was 1 → squared error 0.81 → terrible."""
    _seed_class(conn, class_id="cls-bad", sector="climate")
    _seed_resolved_comparison(
        conn,
        comparison_id="c-bad",
        class_id="cls-bad",
        model_p=0.1,
        outcome_observed=1,
        resolution_ts=datetime(2026, 5, 10, tzinfo=UTC),
    )
    out = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    entry = out["sector_brier_scores"][0]
    assert entry["sector"] == "climate"
    assert entry["brier_score"] == pytest.approx(0.81)
    assert entry["miscalibrated"] is True


def test_brier_aggregates_across_resolutions(conn: duckdb.DuckDBPyConnection) -> None:
    """Multiple resolutions in the same sector average their squared errors."""
    _seed_class(conn, class_id="cls-multi", sector="regulatory")
    # Three resolutions: 0.9 / outcome=1 (err 0.01), 0.4 / outcome=0 (err 0.16),
    # 0.7 / outcome=1 (err 0.09). Mean = (0.01 + 0.16 + 0.09) / 3 = 0.0867.
    _seed_resolved_comparison(
        conn,
        comparison_id="c-r1",
        class_id="cls-multi",
        model_p=0.9,
        outcome_observed=1,
        resolution_ts=datetime(2026, 5, 1, tzinfo=UTC),
        condition_id="0xR1",
    )
    _seed_resolved_comparison(
        conn,
        comparison_id="c-r2",
        class_id="cls-multi",
        model_p=0.4,
        outcome_observed=0,
        resolution_ts=datetime(2026, 5, 2, tzinfo=UTC),
        condition_id="0xR2",
    )
    _seed_resolved_comparison(
        conn,
        comparison_id="c-r3",
        class_id="cls-multi",
        model_p=0.7,
        outcome_observed=1,
        resolution_ts=datetime(2026, 5, 3, tzinfo=UTC),
        condition_id="0xR3",
    )
    out = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    entry = next(e for e in out["sector_brier_scores"] if e["sector"] == "regulatory")
    expected_brier = round((0.01 + 0.16 + 0.09) / 3, 4)
    assert entry["n_resolutions"] == 3
    assert entry["brier_score"] == pytest.approx(expected_brier, abs=1e-4)


def test_window_filter_excludes_old_resolutions(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Resolutions outside the rolling window are excluded from the score."""
    _seed_class(conn, class_id="cls-old", sector="commodity")
    # Old: 200 days ago — outside default 90-day window.
    _seed_resolved_comparison(
        conn,
        comparison_id="c-old",
        class_id="cls-old",
        model_p=0.1,  # would have huge squared error 0.81
        outcome_observed=1,
        resolution_ts=datetime(2025, 11, 1, tzinfo=UTC),
        condition_id="0xOLD",
    )
    # Fresh: 5 days ago — inside window.
    _seed_resolved_comparison(
        conn,
        comparison_id="c-fresh",
        class_id="cls-old",
        model_p=0.9,  # squared error 0.01
        outcome_observed=1,
        resolution_ts=datetime(2026, 5, 10, tzinfo=UTC),
        condition_id="0xFRESH",
    )
    out = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    entry = next(e for e in out["sector_brier_scores"] if e["sector"] == "commodity")
    # Only the fresh resolution counts.
    assert entry["n_resolutions"] == 1
    assert entry["brier_score"] == pytest.approx(0.01, abs=1e-6)


def test_invalidated_resolutions_excluded(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Markets with resolution_outcome='invalid' don't contribute to Brier."""
    _seed_class(conn, class_id="cls-void", sector="public_health")
    _seed_resolved_comparison(
        conn,
        comparison_id="c-void",
        class_id="cls-void",
        model_p=0.3,
        outcome_observed=0,
        resolution_ts=datetime(2026, 5, 10, tzinfo=UTC),
        resolution_outcome="invalid",
        condition_id="0xVOID",
    )
    out = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    # The sector should have no entry because every resolution is invalid.
    assert all(e["sector"] != "public_health" for e in out["sector_brier_scores"])


def test_custom_window_days(conn: duckdb.DuckDBPyConnection) -> None:
    """Tightening the window excludes more resolutions."""
    _seed_class(conn, class_id="cls-tight-window", sector="geopolitical")
    # 10 days ago.
    _seed_resolved_comparison(
        conn,
        comparison_id="c-tw-old",
        class_id="cls-tight-window",
        model_p=0.1,
        outcome_observed=1,
        resolution_ts=datetime(2026, 5, 5, tzinfo=UTC),
        condition_id="0xTW1",
    )
    # 1 day ago.
    _seed_resolved_comparison(
        conn,
        comparison_id="c-tw-fresh",
        class_id="cls-tight-window",
        model_p=0.9,
        outcome_observed=1,
        resolution_ts=datetime(2026, 5, 14, tzinfo=UTC),
        condition_id="0xTW2",
    )
    until = datetime(2026, 5, 15, tzinfo=UTC)
    out_default = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=until,
    )
    out_tight = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=until,
        brier_window_days=5,  # excludes the 10-day-old one
    )
    default_entry = next(
        e for e in out_default["sector_brier_scores"] if e["sector"] == "geopolitical"
    )
    tight_entry = next(e for e in out_tight["sector_brier_scores"] if e["sector"] == "geopolitical")
    assert default_entry["n_resolutions"] == 2
    assert tight_entry["n_resolutions"] == 1


def test_custom_miscalibration_threshold(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Tightening the threshold flips a borderline sector to miscalibrated."""
    _seed_class(conn, class_id="cls-borderline", sector="climate")
    # Brier ≈ 0.16: 0.4 / 1 → squared err 0.36; 0.6 / 1 → 0.16; 0.7 / 1 → 0.09.
    # Wait — recompute. Use one resolution: 0.6 / 1 → squared 0.16.
    _seed_resolved_comparison(
        conn,
        comparison_id="c-bl",
        class_id="cls-borderline",
        model_p=0.6,
        outcome_observed=1,
        resolution_ts=datetime(2026, 5, 10, tzinfo=UTC),
        condition_id="0xBL",
    )
    out_default = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    out_strict = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
        miscalibration_threshold=0.10,
    )
    default_entry = next(e for e in out_default["sector_brier_scores"] if e["sector"] == "climate")
    strict_entry = next(e for e in out_strict["sector_brier_scores"] if e["sector"] == "climate")
    # 0.16 < default 0.25 but > 0.10.
    assert default_entry["miscalibrated"] is False
    assert strict_entry["miscalibrated"] is True


def test_multiple_sectors_sorted_alphabetically(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed_class(conn, class_id="cls-z", sector="zeta_sector")
    _seed_class(conn, class_id="cls-a", sector="alpha_sector")
    _seed_resolved_comparison(
        conn,
        comparison_id="c-z",
        class_id="cls-z",
        model_p=0.5,
        outcome_observed=1,
        resolution_ts=datetime(2026, 5, 10, tzinfo=UTC),
        condition_id="0xZ",
    )
    _seed_resolved_comparison(
        conn,
        comparison_id="c-a",
        class_id="cls-a",
        model_p=0.5,
        outcome_observed=1,
        resolution_ts=datetime(2026, 5, 10, tzinfo=UTC),
        condition_id="0xA",
    )
    out = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    sectors = [e["sector"] for e in out["sector_brier_scores"]]
    assert sectors == sorted(sectors)


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
        "report_id": "rpt-brier",
    }


def _baseline_footer() -> dict[str, Any]:
    return {
        "disclaimer_text": "Decision-support only.",
        "completed_at": datetime(2026, 5, 15, 14, tzinfo=UTC),
        "system_version": "0.1.0",
        "report_id": "rpt-brier",
    }


def _section_with_brier(
    *,
    resolutions: list[dict[str, Any]] | None = None,
    sector_brier_scores: list[dict[str, Any]] | None = None,
) -> SectionContent:
    return SectionContent(
        name="calibration",
        content={
            "type": "calibration",
            "resolutions": resolutions or [],
            "sector_brier_scores": sector_brier_scores or [],
        },
    )


def test_terminal_renderer_renders_brier_table_only() -> None:
    """When there are no fresh resolutions but sector Brier data exists,
    the renderer still emits a non-empty body."""
    section = _section_with_brier(
        sector_brier_scores=[
            {
                "sector": "macroeconomic",
                "n_resolutions": 12,
                "brier_score": 0.12,
                "miscalibrated": False,
                "window_days": 90,
            },
            {
                "sector": "climate",
                "n_resolutions": 4,
                "brier_score": 0.32,
                "miscalibrated": True,
                "window_days": 90,
            },
        ]
    )
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "Per-sector Brier scores" in text
    assert "macroeconomic" in text
    assert "0.1200" in text
    assert "climate" in text
    assert "miscalibrated" in text


def test_markdown_renderer_renders_brier_table() -> None:
    section = _section_with_brier(
        sector_brier_scores=[
            {
                "sector": "macroeconomic",
                "n_resolutions": 12,
                "brier_score": 0.12,
                "miscalibrated": False,
                "window_days": 90,
            }
        ]
    )
    md = markdown.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "Per-sector Brier scores" in md
    assert "| Sector | Brier | n | Window | Status |" in md
    assert "macroeconomic" in md
    assert "`0.1200`" in md


def test_renderers_pass_linter_with_brier_data() -> None:
    section = _section_with_brier(
        sector_brier_scores=[
            {
                "sector": "macroeconomic",
                "n_resolutions": 12,
                "brier_score": 0.12,
                "miscalibrated": False,
                "window_days": 90,
            },
            {
                "sector": "climate",
                "n_resolutions": 4,
                "brier_score": 0.32,
                "miscalibrated": True,
                "window_days": 90,
            },
        ]
    )
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    md = markdown.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    check_text(text)
    check_text(md)


def test_terminal_renderer_combines_resolutions_and_brier() -> None:
    """When both resolutions and sector_brier_scores are present, both render."""
    section = _section_with_brier(
        resolutions=[
            {
                "comparison_id": "c-old",
                "class_id": "cls-x",
                "class_title": "Test Class",
                "condition_id": "0xX",
                "venue": "polymarket",
                "resolution_outcome": "yes",
                "model_probability": 0.8,
                "market_probability": 0.5,
                "polarity": "aligned",
                "outcome_observed": 1,
                "days_to_resolution": 14,
                "predicted_band": "high",
                "verdict_text": "Model said 0.80 → resolved YES; well-calibrated.",
            }
        ],
        sector_brier_scores=[
            {
                "sector": "macroeconomic",
                "n_resolutions": 5,
                "brier_score": 0.15,
                "miscalibrated": False,
                "window_days": 90,
            }
        ],
    )
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[section],
        footer=_baseline_footer(),
    )
    assert "Test Class" in text
    assert "Per-sector Brier scores" in text
