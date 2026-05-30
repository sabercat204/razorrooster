"""T-RG-080 — report_generator end-to-end acceptance test.

Verifies the acceptance criteria from REPORT_GENERATOR.md §8:

- A daily generation runs end-to-end.
- Every section renders or shows a "section unavailable" placeholder.
- Disclaimer block, "case for market" prominence, sizing disclaimer
  block, and equal prominence rendering all appear correctly.
- Imperative-language linter rejects adversarial output.
- Markdown export produces well-formed markdown.
- ``report_log`` retains historical reports.
- No network calls observable during generation (T-RG-081).

The test composes a synthetic upstream chain via SQL fixtures and
runs the generator on top. The position_engine and monitor E2E
tests already cover full-chain composition with real engines; this
test focuses on report-generator-specific behavior layered on top.
"""

from __future__ import annotations

import json
import socket
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.data_ingest.persistence.provenance import (
    register_source,
    update_last_successful_fetch,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.monitor.persistence.migrations import (
    run_pending_monitor_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.position_engine.frame.linter import (
    ImperativeLanguageDetected,
)
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.report_generator.engines.generator import generate
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import (
    list_reports,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "rg_e2e.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_polymarket_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_position_engine_migrations(c)
        run_pending_monitor_migrations(c)
        run_pending_report_generator_migrations(c)
    yield s
    s.close()


@pytest.fixture
def populated_store(store: DuckDBStore) -> DuckDBStore:
    """Seed a synthetic upstream chain with mixed-section content."""
    with store.connection() as conn:
        # Stale source for system_health.
        register_source(
            conn,
            source_id="noaa",
            source_type="time_series",
            cadence="daily",
            freshness_threshold_seconds=86400,
            license="public_domain",
        )
        update_last_successful_fetch(
            conn,
            source_id="noaa",
            when=datetime(2026, 5, 1, tzinfo=UTC),
        )
        # One class + one market.
        _seed_class(conn, class_id="cls-pheic", title="PHEIC declaration")
        _seed_class(conn, class_id="cls-unmapped", title="Unmapped class for watchlist")
        _seed_market(conn, condition_id="0xPHEIC")
        # Mispricing cycle setup.
        _seed_comparison_cycle(conn)
        _seed_mapping(conn, mapping_id="m1", class_id="cls-pheic", condition_id="0xPHEIC")
        _seed_scan_record(conn, class_id="cls-pheic", posterior=0.40)
        _seed_scan_record(conn, class_id="cls-unmapped", posterior=0.45)
        # One surfaced comparison + trace.
        _seed_surfaced_comparison(
            conn,
            comparison_id="comp-pheic",
            class_id="cls-pheic",
            mapping_id="m1",
            condition_id="0xPHEIC",
        )
        _seed_comparison_trace(
            conn,
            comparison_id="comp-pheic",
            case_for_model=["Precursor signature elevated."],
            case_for_market=["Market participants may have private info."],
        )
        # One unsurfaced suppressed comparison for system_health breakdown.
        _seed_unsurfaced_comparison(
            conn,
            comparison_id="comp-suppressed",
            class_id="cls-pheic",
            mapping_id="m1",
            condition_id="0xPHEIC",
            suppression_reasons=["sub_edge_threshold"],
        )
        # One position_engine analysis for the surfaced comparison.
        _seed_minimal_analysis(
            conn,
            analysis_id="an-pheic",
            class_id="cls-pheic",
            comparison_id="comp-pheic",
            condition_id="0xPHEIC",
        )
        # One resolved comparison for calibration.
        _seed_minimal_class(conn, class_id="cls-resolved")
        _seed_unsurfaced_comparison(
            conn,
            comparison_id="comp-resolved",
            class_id="cls-resolved",
            mapping_id="m-resolved",
            condition_id="0xRESOLVED",
            suppression_reasons=[],
        )
        _seed_resolution(
            conn,
            comparison_id="comp-resolved",
            condition_id="0xRESOLVED",
            outcome="yes",
            model_p=0.80,
        )
        # One follow_up under recommended_review for watched section.
        _seed_minimal_class(conn, class_id="cls-watched")
        _seed_market(conn, condition_id="0xWATCH")
        _seed_minimal_analysis(
            conn,
            analysis_id="an-watched",
            class_id="cls-watched",
            comparison_id="comp-watched",
            condition_id="0xWATCH",
        )
        _seed_monitor_cycle(conn, cycle_id="mon-1")
        _seed_follow_up(
            conn,
            cycle_id="mon-1",
            follow_up_id="fu-watched",
            analysis_id="an-watched",
            primary_alert_tier="material_shift",
            recommended_review=True,
        )
    return store


# -- E2E tests --------------------------------------------------------------


def test_full_report_renders_all_sections(populated_store: DuckDBStore) -> None:
    result = generate(
        populated_store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 15, 14, tzinfo=UTC),
        library_age_days=2,
    )
    text = result.rendered_terminal_text
    # Every section header.
    assert "RAZOR-ROOSTER REPORT" in text
    assert "SYSTEM HEALTH" in text
    assert "SURFACED COMPARISONS" in text
    assert "ACTIVE WATCHED SITUATIONS" in text
    assert "CALIBRATION LOG" in text
    assert "WATCHLIST (DEVELOPING)" in text
    # System health surfaces stale source + suppressed breakdown.
    assert "noaa" in text
    assert "sub_edge_threshold" in text
    # Surfaced section has the case-for-market section.
    assert "Possible reasons the model may be right" in text
    assert "Possible reasons the market may be right" in text
    assert "Market participants may have private info" in text
    # Watched section ranks the follow-up.
    assert "fu-watched" in text
    assert "material_shift" in text
    # Calibration includes the high_yes verdict.
    assert "in line with predicted likelihood" in text
    # Watchlist surfaces the unmapped class.
    assert "Unmapped class for watchlist" in text
    # Footer disclaimer.
    assert "DISCLAIMER:" in text
    assert "decision-support analysis" in text
    # Sections rendered exhaustively.
    assert set(result.sections_rendered) == {
        "system_health",
        "surfaced",
        "cross_venue",
        "watched",
        "calibration",
        "watchlist",
    }


def test_markdown_export_round_trip(populated_store: DuckDBStore, tmp_path: Path) -> None:
    md_path = tmp_path / "out" / "report.md"
    result = generate(
        populated_store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        markdown_path=md_path,
        quiet=True,
        now=datetime(2026, 5, 15, 14, tzinfo=UTC),
    )
    on_disk = md_path.read_text(encoding="utf-8")
    assert on_disk == result.rendered_markdown_text
    # Markdown structure checks.
    assert on_disk.startswith("# Razor-Rooster Report")
    assert "## System Health" in on_disk
    assert "## Surfaced Comparisons" in on_disk
    assert "## Calibration Log" in on_disk
    assert "## Disclaimer" in on_disk
    # GFM table for calibration.
    assert "| Class | Venue | Outcome | Predicted p | Days to Resolution | Verdict |" in on_disk


def test_section_failure_isolation_full_report(
    populated_store: DuckDBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One section fails; the rest still render."""
    from razor_rooster.report_generator.engines.section_assemblers import (
        watched as watched_assembler,
    )

    def boom(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("watched failure")

    monkeypatch.setattr(watched_assembler, "assemble", boom)
    result = generate(
        populated_store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 15, 14, tzinfo=UTC),
    )
    text = result.rendered_terminal_text
    # Failed section shows placeholder; others still rendered.
    assert "section error: RuntimeError: watched failure" in text
    assert "SURFACED COMPARISONS" in text
    assert "CALIBRATION LOG" in text
    assert any(f["section"] == "watched" for f in result.sections_failed)


def test_empty_cycle_renders_nothing_to_report(store: DuckDBStore) -> None:
    """Empty upstream → 'nothing to report' notes per section."""
    result = generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 15, tzinfo=UTC),
    )
    text = result.rendered_terminal_text
    assert "No comparisons surfaced" in text
    assert "No watched analyses required review" in text
    assert "No new resolutions" in text
    assert "No unmapped candidates" in text


def test_linter_rejects_adversarial_output(
    populated_store: DuckDBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversarial render text triggers ImperativeLanguageDetected."""
    from razor_rooster.report_generator.engines.section_assemblers import (
        footer as footer_assembler,
    )

    monkeypatch.setattr(
        footer_assembler,
        "load_disclaimer_text",
        lambda *args, **kwargs: "I recommend you take this position now.",
    )
    with pytest.raises(ImperativeLanguageDetected):
        generate(
            populated_store,
            since=datetime(2026, 5, 14, tzinfo=UTC),
            quiet=True,
            now=datetime(2026, 5, 15, tzinfo=UTC),
        )
    # No row written.
    with populated_store.connection() as conn:
        rows = list_reports(conn)
    assert rows == ()


def test_multiple_reports_persist(populated_store: DuckDBStore) -> None:
    """Repeated reports accumulate in report_log for retrospective access."""
    for offset in range(3):
        generate(
            populated_store,
            since=datetime(2026, 5, 13 + offset, tzinfo=UTC),
            quiet=True,
            now=datetime(2026, 5, 14 + offset, 14, tzinfo=UTC),
        )
    with populated_store.connection() as conn:
        all_reports = list_reports(conn)
    assert len(all_reports) == 3


# -- T-RG-081: network-disabled smoke test ---------------------------------


def test_no_network_calls_during_generate(
    populated_store: DuckDBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-NFR-RG-LOCAL-001: no network activity during a generate call.

    Patches ``socket.socket`` to raise on instantiation. If any code
    path opens a network socket the test fails.
    """
    real_socket = socket.socket

    class BlockedSocket(real_socket):
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("network access attempted during report generation")

    monkeypatch.setattr(socket, "socket", BlockedSocket)
    result = generate(
        populated_store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert result.report_id


# -- shared seed helpers ----------------------------------------------------


def _seed_class(
    conn: object,  # duckdb.DuckDBPyConnection at runtime
    *,
    class_id: str,
    title: str = "Test class",
    domain_sector: str = "geopolitics",
) -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT OR IGNORE INTO pl_event_classes "
        "(class_id, title, description, domain_sector, definition_version, "
        "outcome_type, registered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            class_id,
            title,
            "synthetic test class",
            domain_sector,
            1,
            "binary",
            datetime(2026, 5, 1, tzinfo=UTC),
        ],
    )


def _seed_minimal_class(conn: object, *, class_id: str) -> None:
    _seed_class(conn, class_id=class_id, title=class_id)


def _seed_market(conn: object, *, condition_id: str, resolved: bool = False) -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO polymarket_markets "
        "(source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, source_payload_json, superseded_at, "
        "condition_id, slug, question, market_type, outcome_tokens, "
        "end_date, active, closed, resolved) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "polymarket",
            condition_id,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
            "1.0",
            json.dumps({}),
            condition_id,
            f"slug-{condition_id}",
            "Test market?",
            "binary",
            json.dumps([{"token_id": f"{condition_id}-yes", "label": "Yes"}]),
            datetime(2027, 1, 1, tzinfo=UTC),
            True,
            False,
            resolved,
        ],
    )


def _seed_comparison_cycle(conn: object, *, cycle_id: str = "mp-cycle-1") -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT OR IGNORE INTO comparison_cycles "
        "(cycle_id, started_at, completed_at, comparisons_total, "
        "surfaced_count, suppressed_breakdown, library_version_at_cycle, "
        "scan_id_consumed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            cycle_id,
            datetime(2026, 5, 14, tzinfo=UTC),
            datetime(2026, 5, 14, tzinfo=UTC),
            1,
            1,
            json.dumps({}),
            1,
            "scan-original",
        ],
    )


def _seed_mapping(
    conn: object,
    *,
    mapping_id: str,
    class_id: str,
    condition_id: str,
) -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT OR IGNORE INTO class_market_mappings "
        "(mapping_id, class_id, condition_id, mapping_type, "
        "mapping_confidence, polarity, mapped_by, mapped_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            mapping_id,
            class_id,
            condition_id,
            "direct",
            "exact",
            "aligned",
            "operator",
            datetime(2026, 5, 1, tzinfo=UTC),
        ],
    )


def _seed_scan_record(
    conn: object,
    *,
    class_id: str,
    posterior: float,
    scan_id: str = "scan-original",
) -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT OR IGNORE INTO scan_summaries "
        "(scan_id, scan_started_at, scan_completed_at, "
        "pattern_library_version, classes_total, classes_succeeded, "
        "classes_failed, classes_skipped, candidates_count, "
        "library_stale_warning) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            scan_id,
            datetime(2026, 5, 14, tzinfo=UTC),
            datetime(2026, 5, 14, tzinfo=UTC),
            1,
            1,
            1,
            0,
            0,
            1,
            False,
        ],
    )
    conn.execute(  # type: ignore[attr-defined]
        "INSERT OR REPLACE INTO scan_records "
        "(scan_id, class_id, class_definition_version, "
        "pattern_library_version, data_as_of, scan_started_at, "
        "scan_completed_at, base_rate, base_rate_ci_lower, "
        "base_rate_ci_upper, posterior, posterior_ci_lower, "
        "posterior_ci_upper, log_odds_shift, is_candidate) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            scan_id,
            class_id,
            1,
            1,
            datetime(2026, 5, 14, tzinfo=UTC),
            datetime(2026, 5, 14, tzinfo=UTC),
            datetime(2026, 5, 14, tzinfo=UTC),
            0.10,
            0.05,
            0.18,
            posterior,
            max(0.0, posterior - 0.10),
            min(1.0, posterior + 0.10),
            0.5,
            True,
        ],
    )


def _seed_surfaced_comparison(
    conn: object,
    *,
    comparison_id: str,
    class_id: str,
    mapping_id: str,
    condition_id: str,
    score: float = 1.5,
) -> None:
    conn.execute(  # type: ignore[attr-defined]
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
        "low_liquidity, low_mapping_confidence, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            "mp-cycle-1",
            mapping_id,
            class_id,
            condition_id,
            f"{condition_id}-yes",
            "aligned",
            "scan-original",
            0.30,
            0.20,
            0.40,
            0.10,
            0.09,
            0.11,
            0.10,
            10000.0,
            200,
            datetime(2026, 5, 14, tzinfo=UTC),
            0.20,
            0.5,
            False,
            0.05,
            score,
            True,
            json.dumps([]),
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
        ],
    )


def _seed_unsurfaced_comparison(
    conn: object,
    *,
    comparison_id: str,
    class_id: str,
    mapping_id: str,
    condition_id: str,
    suppression_reasons: list[str],
) -> None:
    _seed_mapping(
        conn,
        mapping_id=mapping_id,
        class_id=class_id,
        condition_id=condition_id,
    )
    conn.execute(  # type: ignore[attr-defined]
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
        "low_liquidity, low_mapping_confidence, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            "mp-cycle-1",
            mapping_id,
            class_id,
            condition_id,
            f"{condition_id}-yes",
            "aligned",
            "scan-original",
            0.30,
            0.20,
            0.40,
            0.10,
            0.09,
            0.11,
            0.10,
            10000.0,
            200,
            datetime(2026, 5, 14, tzinfo=UTC),
            0.05,
            0.1,
            True,
            0.01,
            None,
            False,
            json.dumps(suppression_reasons),
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
        ],
    )


def _seed_comparison_trace(
    conn: object,
    *,
    comparison_id: str,
    case_for_model: list[str],
    case_for_market: list[str],
) -> None:
    payload = {
        "case_for_model": case_for_model,
        "case_for_market": case_for_market,
        "ambiguity_factors": [],
        "warnings": [],
    }
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO comparison_traces (comparison_id, trace_json) VALUES (?, ?)",
        [comparison_id, json.dumps(payload)],
    )


def _seed_resolution(
    conn: object,
    *,
    comparison_id: str,
    condition_id: str,
    outcome: str,
    model_p: float,
) -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO comparison_resolutions "
        "(comparison_id, condition_id, resolution_outcome, resolution_ts, "
        "model_probability_at_comparison, market_probability_at_comparison, "
        "polarity_at_comparison, outcome_observed, linked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            condition_id,
            outcome,
            datetime(2026, 5, 14, tzinfo=UTC),
            model_p,
            0.10,
            "aligned",
            1 if outcome == "yes" else 0,
            datetime(2026, 5, 14, 14, tzinfo=UTC),
        ],
    )


def _seed_minimal_analysis(
    conn: object,
    *,
    analysis_id: str,
    class_id: str,
    comparison_id: str,
    condition_id: str,
) -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT OR IGNORE INTO bankroll_config "
        "(config_id, analytical_bankroll_usd, max_single_position_pct, "
        "kelly_fraction_default, min_edge_threshold, effective_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            "bk-1",
            1000.0,
            0.10,
            0.5,
            0.02,
            datetime(2026, 5, 1, tzinfo=UTC),
            "operator",
        ],
    )
    conn.execute(  # type: ignore[attr-defined]
        "INSERT OR IGNORE INTO analysis_cycles "
        "(cycle_id, started_at, completed_at, bankroll_config_id, "
        "analyses_total, analyses_with_positive_kelly, "
        "analyses_clamped_by_cap, analyses_clamped_by_liquidity, "
        "duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "pe-cy-1",
            datetime(2026, 5, 14, tzinfo=UTC),
            datetime(2026, 5, 14, tzinfo=UTC),
            "bk-1",
            1,
            1,
            0,
            0,
            5.0,
        ],
    )
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO analyses "
        "(analysis_id, cycle_id, comparison_id, class_id, condition_id, "
        "bankroll_config_id, model_probability, market_probability, "
        "kelly_unclamped, kelly_negative, kelly_clamped_by_max_cap, "
        "kelly_clamped_by_liquidity, suggested_fraction, "
        "suggested_dollar_size, ev_per_dollar, "
        "bankroll_after_1_loss_pct, bankroll_after_3_losses_pct, "
        "bankroll_after_5_losses_pct, suggested_pct_of_24h_volume, "
        "days_to_resolution, long_time_to_resolution, sub_threshold, "
        "sensitivity_analysis, invalidation_criteria, "
        "low_signature_confidence, source_stale_warning, "
        "library_stale_warning, definition_drift_warning, "
        "low_mapping_confidence, low_liquidity, error, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            analysis_id,
            "pe-cy-1",
            comparison_id,
            class_id,
            condition_id,
            "bk-1",
            0.30,
            0.10,
            0.05,
            False,
            False,
            False,
            0.05,
            50.0,
            0.20,
            95.0,
            85.74,
            77.38,
            0.05,
            60,
            False,
            False,
            None,
            json.dumps([]),
            False,
            False,
            False,
            False,
            False,
            False,
            None,
            datetime(2026, 5, 14, 12, tzinfo=UTC),
        ],
    )


def _seed_monitor_cycle(conn: object, *, cycle_id: str = "mon-1") -> None:
    conn.execute(  # type: ignore[attr-defined]
        "INSERT OR IGNORE INTO monitor_cycles "
        "(cycle_id, started_at, completed_at, follow_ups_total, "
        "follow_ups_with_alerts, alerts_by_tier) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            cycle_id,
            datetime(2026, 5, 15, tzinfo=UTC),
            datetime(2026, 5, 15, tzinfo=UTC),
            1,
            1,
            json.dumps({"material_shift": 1}),
        ],
    )


def _seed_follow_up(
    conn: object,
    *,
    cycle_id: str,
    follow_up_id: str,
    analysis_id: str,
    primary_alert_tier: str | None,
    recommended_review: bool,
) -> None:
    alert_tiers = [primary_alert_tier] if primary_alert_tier else []
    conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO follow_ups "
        "(follow_up_id, cycle_id, analysis_id, analysis_model_p, "
        "analysis_market_p, analysis_computed_at, current_scan_id, "
        "current_model_p, current_model_ci, current_market_p, "
        "current_market_snapshot_ts, model_probability_shift, "
        "model_shift_band, market_probability_shift, market_shift_band, "
        "precursor_snapshot, days_since_analysis, days_to_resolution, "
        "time_decay_alert, invalidation_evaluations, "
        "invalidation_triggered_count, resolution_status, "
        "recommended_review, primary_alert_tier, alert_tiers, "
        "reasoning_text, source_stale_warning, library_stale_warning, "
        "error, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            follow_up_id,
            cycle_id,
            analysis_id,
            0.30,
            0.10,
            datetime(2026, 5, 10, tzinfo=UTC),
            "scan-current",
            0.42,
            json.dumps([0.30, 0.55]),
            0.12,
            datetime(2026, 5, 15, 12, tzinfo=UTC),
            0.12,
            "material",
            0.02,
            "minor",
            json.dumps([]),
            5,
            60,
            False,
            json.dumps([]),
            0,
            "unresolved",
            recommended_review,
            primary_alert_tier,
            json.dumps(alert_tiers),
            "Watched analysis with material upward shift.",
            False,
            False,
            None,
            datetime(2026, 5, 15, 14, tzinfo=UTC),
        ],
    )


# Suppress unused-import warning for typing helpers kept for expansion.
_ = (timedelta, uuid)
