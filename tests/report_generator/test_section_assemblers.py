"""T-RG-020..T-RG-026 — section assembler tests."""

from __future__ import annotations

import json
import uuid
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
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    calibration,
    footer,
    header,
    surfaced,
    system_health,
    watched,
    watchlist,
)
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "rg_assemblers.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_polymarket_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_position_engine_migrations(c)
        run_pending_monitor_migrations(c)
        run_pending_report_generator_migrations(c)
        yield c
    store.close()


# -- header tests -----------------------------------------------------------


def test_header_assemble_produces_expected_fields(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    out = header.assemble(
        conn,
        report_id="rep-abc",
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
        library_version=7,
        library_age_days=2,
        disabled_sections=("watchlist",),
    )
    assert out["type"] == "header"
    assert out["report_id"] == "rep-abc"
    assert out["cycle_date"] == "2026-05-15"
    assert out["library_version"] == 7
    assert out["library_age_days"] == 2
    assert out["stale_source_count"] == 0
    assert out["disabled_sections"] == ("watchlist",)


def test_header_counts_stale_sources(conn: duckdb.DuckDBPyConnection) -> None:
    register_source(
        conn,
        source_id="noaa",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=86400,
        license="public_domain",
    )
    # Last successful fetch a week ago — past 1-day threshold.
    update_last_successful_fetch(
        conn,
        source_id="noaa",
        when=datetime(2026, 5, 1, tzinfo=UTC),
    )
    out = header.assemble(
        conn,
        report_id="rep-abc",
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
        library_version=1,
        library_age_days=None,
    )
    assert out["stale_source_count"] == 1


# -- footer tests -----------------------------------------------------------


def test_footer_loads_disclaimer_text() -> None:
    text = footer.load_disclaimer_text()
    assert "decision-support analysis" in text
    assert "Polymarket" in text


def test_footer_assemble_includes_required_fields() -> None:
    out = footer.assemble(
        report_id="rep-abc",
        system_version="0.1.0",
        completed_at=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert out["type"] == "footer"
    assert "decision-support analysis" in out["disclaimer_text"]
    assert out["system_version"] == "0.1.0"


def test_footer_falls_back_to_default_text(tmp_path: Path) -> None:
    """A missing template path uses the verbatim REQ-RG-SEC-007 fallback."""
    text = footer.load_disclaimer_text(tmp_path / "missing.txt")
    assert "decision-support analysis" in text


# -- system_health tests ----------------------------------------------------


def test_system_health_no_stale_no_errors(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    out = system_health.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
        library_age_days=2,
    )
    assert out["type"] == "system_health"
    assert out["stale_sources"] == []
    assert out["errored_subsystems"] == []
    assert out["suppressed_breakdown"] == {}
    assert out["library_age_days"] == 2


def test_system_health_lists_stale_sources(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    register_source(
        conn,
        source_id="acled",
        source_type="event_stream",
        cadence="weekly",
        freshness_threshold_seconds=86400,
        license="cc_by_nc_4_0",
    )
    update_last_successful_fetch(conn, source_id="acled", when=datetime(2026, 5, 1, tzinfo=UTC))
    out = system_health.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert len(out["stale_sources"]) == 1
    assert out["stale_sources"][0]["source_id"] == "acled"


def test_system_health_aggregates_suppressed_breakdown(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """REQ-RG-SEC-002: suppressed comparisons grouped by reason."""
    _seed_minimal_market(conn)
    _seed_minimal_class(conn)
    _seed_unsurfaced_comparison(
        conn,
        comparison_id="comp-suppressed-1",
        suppression_reasons=["low_mapping_confidence"],
    )
    _seed_unsurfaced_comparison(
        conn,
        comparison_id="comp-suppressed-2",
        suppression_reasons=["low_mapping_confidence", "stale_market_price"],
    )
    out = system_health.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    breakdown = out["suppressed_breakdown"]
    assert breakdown.get("low_mapping_confidence") == 2
    assert breakdown.get("stale_market_price") == 1


# -- surfaced tests ---------------------------------------------------------


def test_surfaced_empty(conn: duckdb.DuckDBPyConnection) -> None:
    out = surfaced.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert out["type"] == "surfaced"
    assert out["comparisons"] == []


def test_surfaced_lists_comparisons_with_trace(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed_minimal_market(conn)
    _seed_minimal_class(conn, class_id="cls-sf", title="Surface Class")
    _seed_surfaced_comparison(
        conn,
        comparison_id="comp-sf",
        class_id="cls-sf",
        score=1.5,
    )
    _seed_comparison_trace(
        conn,
        comparison_id="comp-sf",
        case_for_model=["Model bullet 1"],
        case_for_market=["Market bullet 1"],
    )
    out = surfaced.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert len(out["comparisons"]) == 1
    item = out["comparisons"][0]
    assert item["comparison_id"] == "comp-sf"
    assert item["class_title"] == "Surface Class"
    assert item["case_for_model"] == ["Model bullet 1"]
    assert item["case_for_market"] == ["Market bullet 1"]


def test_surfaced_orders_by_score_descending(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed_minimal_market(conn)
    _seed_minimal_class(conn, class_id="cls-1", title="C1")
    _seed_minimal_class(conn, class_id="cls-2", title="C2")
    _seed_surfaced_comparison(conn, comparison_id="comp-low", class_id="cls-1", score=0.5)
    _seed_surfaced_comparison(conn, comparison_id="comp-high", class_id="cls-2", score=2.5)
    out = surfaced.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    ids = [c["comparison_id"] for c in out["comparisons"]]
    assert ids == ["comp-high", "comp-low"]


# -- watched tests ----------------------------------------------------------


def test_watched_empty(conn: duckdb.DuckDBPyConnection) -> None:
    out = watched.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert out["type"] == "watched"
    assert out["follow_ups"] == []


def test_watched_orders_by_alert_tier(conn: duckdb.DuckDBPyConnection) -> None:
    _seed_minimal_market(conn)
    _seed_minimal_class(conn, class_id="cls-w")
    _seed_minimal_analysis(conn, analysis_id="an-time", class_id="cls-w")
    _seed_minimal_analysis(conn, analysis_id="an-shift", class_id="cls-w")
    _seed_minimal_analysis(conn, analysis_id="an-resolution", class_id="cls-w")
    _seed_monitor_cycle(conn, cycle_id="mon-1")
    _seed_follow_up(
        conn,
        cycle_id="mon-1",
        follow_up_id="fu-time",
        analysis_id="an-time",
        primary_alert_tier="time_decay",
        recommended_review=True,
    )
    _seed_follow_up(
        conn,
        cycle_id="mon-1",
        follow_up_id="fu-shift",
        analysis_id="an-shift",
        primary_alert_tier="material_shift",
        recommended_review=True,
    )
    _seed_follow_up(
        conn,
        cycle_id="mon-1",
        follow_up_id="fu-resolution",
        analysis_id="an-resolution",
        primary_alert_tier="resolution",
        recommended_review=True,
        resolution_status="resolved_yes",
    )
    out = watched.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 16, tzinfo=UTC),
    )
    primary_order = [f["primary_alert_tier"] for f in out["follow_ups"]]
    assert primary_order == ["resolution", "material_shift", "time_decay"]


def test_watched_excludes_not_recommended(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed_minimal_market(conn)
    _seed_minimal_class(conn, class_id="cls-w")
    _seed_minimal_analysis(conn, analysis_id="an-quiet", class_id="cls-w")
    _seed_monitor_cycle(conn, cycle_id="mon-1")
    _seed_follow_up(
        conn,
        cycle_id="mon-1",
        follow_up_id="fu-quiet",
        analysis_id="an-quiet",
        primary_alert_tier=None,
        recommended_review=False,
    )
    out = watched.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 16, tzinfo=UTC),
    )
    assert out["follow_ups"] == []


# -- calibration tests ------------------------------------------------------


def test_predicted_band_high() -> None:
    assert calibration.predicted_band(0.85) == "high"
    assert calibration.predicted_band(0.7) == "high"


def test_predicted_band_mid() -> None:
    assert calibration.predicted_band(0.50) == "mid"
    assert calibration.predicted_band(0.3) == "mid"


def test_predicted_band_low() -> None:
    assert calibration.predicted_band(0.10) == "low"
    assert calibration.predicted_band(0.299) == "low"


def test_calibration_empty(conn: duckdb.DuckDBPyConnection) -> None:
    out = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert out["type"] == "calibration"
    assert out["resolutions"] == []


def test_calibration_high_yes_verdict(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed_minimal_market(conn)
    _seed_minimal_class(conn, class_id="cls-cal", title="Calibration Class")
    _seed_unsurfaced_comparison(conn, comparison_id="comp-cal", class_id="cls-cal")
    _seed_resolution(
        conn,
        comparison_id="comp-cal",
        outcome="yes",
        model_p=0.85,
    )
    out = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 16, tzinfo=UTC),
    )
    assert len(out["resolutions"]) == 1
    res = out["resolutions"][0]
    assert res["predicted_band"] == "high"
    assert "in line with predicted likelihood" in res["verdict_text"]


def test_calibration_low_yes_tail_outcome_verdict(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed_minimal_market(conn)
    _seed_minimal_class(conn, class_id="cls-cal2")
    _seed_unsurfaced_comparison(conn, comparison_id="comp-cal2", class_id="cls-cal2")
    _seed_resolution(
        conn,
        comparison_id="comp-cal2",
        outcome="yes",
        model_p=0.10,
    )
    out = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 16, tzinfo=UTC),
    )
    res = out["resolutions"][0]
    assert res["predicted_band"] == "low"
    assert "tail outcomes" in res["verdict_text"]


def test_calibration_invalid_outcome_undefined(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed_minimal_market(conn)
    _seed_minimal_class(conn, class_id="cls-cal3")
    _seed_unsurfaced_comparison(conn, comparison_id="comp-cal3", class_id="cls-cal3")
    _seed_resolution(
        conn,
        comparison_id="comp-cal3",
        outcome="invalid",
        model_p=0.50,
    )
    out = calibration.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 16, tzinfo=UTC),
    )
    res = out["resolutions"][0]
    assert "invalidated" in res["verdict_text"]


# -- watchlist tests --------------------------------------------------------


def test_watchlist_empty(conn: duckdb.DuckDBPyConnection) -> None:
    out = watchlist.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert out["type"] == "watchlist"
    assert out["candidates"] == []


def test_watchlist_no_active_mapping(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed_minimal_class(conn, class_id="cls-no-map")
    _seed_scan_record(conn, class_id="cls-no-map", posterior=0.40, is_candidate=True)
    out = watchlist.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 16, tzinfo=UTC),
    )
    assert len(out["candidates"]) == 1
    assert out["candidates"][0]["reason"] == "no_active_mapping"
    assert "mapping" in out["candidates"][0]["suggestion"]


def test_watchlist_excludes_already_surfaced(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed_minimal_market(conn)
    _seed_minimal_class(conn, class_id="cls-surfaced")
    _seed_scan_record(conn, class_id="cls-surfaced", posterior=0.40, is_candidate=True)
    _seed_surfaced_comparison(conn, comparison_id="comp-s", class_id="cls-surfaced")
    out = watchlist.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 16, tzinfo=UTC),
    )
    assert out["candidates"] == []


# -- shared seed helpers ----------------------------------------------------


def _seed_minimal_market(
    conn: duckdb.DuckDBPyConnection, *, condition_id: str = "0xMARKET"
) -> None:
    conn.execute(
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
            "Market?",
            "binary",
            json.dumps([{"token_id": f"{condition_id}-yes", "label": "Yes"}]),
            datetime(2027, 1, 1, tzinfo=UTC),
            True,
            False,
            False,
        ],
    )


def _seed_minimal_class(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str = "cls-1",
    title: str = "Test Class",
    domain_sector: str = "geopolitics",
) -> None:
    conn.execute(
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


def _seed_surfaced_comparison(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    class_id: str = "cls-1",
    score: float = 1.0,
    condition_id: str = "0xMARKET",
) -> None:
    _seed_comparison_cycle(conn)
    _seed_mapping(
        conn,
        mapping_id=f"map-{comparison_id}",
        class_id=class_id,
        condition_id=condition_id,
    )
    _seed_scan_record(conn, class_id=class_id)
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
        "low_liquidity, low_mapping_confidence, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            "mp-cycle-1",
            f"map-{comparison_id}",
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
            datetime(2026, 5, 10, tzinfo=UTC),
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
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    class_id: str = "cls-1",
    suppression_reasons: list[str] | None = None,
    condition_id: str = "0xMARKET",
) -> None:
    _seed_comparison_cycle(conn)
    _seed_mapping(
        conn,
        mapping_id=f"map-{comparison_id}",
        class_id=class_id,
        condition_id=condition_id,
    )
    _seed_scan_record(conn, class_id=class_id)
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
        "low_liquidity, low_mapping_confidence, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            "mp-cycle-1",
            f"map-{comparison_id}",
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
            datetime(2026, 5, 10, tzinfo=UTC),
            0.05,
            0.1,
            True,
            0.01,
            None,
            False,
            json.dumps(suppression_reasons or []),
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


def _seed_comparison_cycle(
    conn: duckdb.DuckDBPyConnection, *, cycle_id: str = "mp-cycle-1"
) -> None:
    conn.execute(
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
    conn: duckdb.DuckDBPyConnection,
    *,
    mapping_id: str,
    class_id: str,
    condition_id: str,
    polarity: str = "aligned",
) -> None:
    conn.execute(
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
            polarity,
            "operator",
            datetime(2026, 5, 1, tzinfo=UTC),
        ],
    )


def _seed_scan_record(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str = "cls-1",
    posterior: float = 0.30,
    scan_id: str = "scan-original",
    is_candidate: bool = True,
) -> None:
    # Allow re-seeding if scan_summaries already has the row.
    conn.execute(
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
            1 if is_candidate else 0,
            False,
        ],
    )
    # Use INSERT OR REPLACE since scan_records PK is (scan_id, class_id).
    conn.execute(
        "INSERT OR REPLACE INTO scan_records "
        "(scan_id, class_id, class_definition_version, pattern_library_version, "
        "data_as_of, scan_started_at, scan_completed_at, "
        "base_rate, base_rate_ci_lower, base_rate_ci_upper, "
        "posterior, posterior_ci_lower, posterior_ci_upper, "
        "log_odds_shift, is_candidate) "
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
            is_candidate,
        ],
    )


def _seed_comparison_trace(
    conn: duckdb.DuckDBPyConnection,
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
    conn.execute(
        "INSERT INTO comparison_traces (comparison_id, trace_json) VALUES (?, ?)",
        [comparison_id, json.dumps(payload)],
    )


def _seed_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    outcome: str = "yes",
    model_p: float = 0.50,
) -> None:
    conn.execute(
        "INSERT INTO comparison_resolutions "
        "(comparison_id, condition_id, resolution_outcome, resolution_ts, "
        "model_probability_at_comparison, market_probability_at_comparison, "
        "polarity_at_comparison, outcome_observed, linked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            "0xMARKET",
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
    conn: duckdb.DuckDBPyConnection,
    *,
    analysis_id: str,
    class_id: str,
    cycle_id: str = "pe-cy-1",
    bankroll_id: str = "bk-1",
    comparison_id: str | None = None,
    condition_id: str = "0xMARKET",
) -> None:
    cmp_id = comparison_id or f"comp-{analysis_id}"
    conn.execute(
        "INSERT OR IGNORE INTO bankroll_config "
        "(config_id, analytical_bankroll_usd, max_single_position_pct, "
        "kelly_fraction_default, min_edge_threshold, effective_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            bankroll_id,
            1000.0,
            0.10,
            0.5,
            0.02,
            datetime(2026, 5, 1, tzinfo=UTC),
            "operator",
        ],
    )
    conn.execute(
        "INSERT OR IGNORE INTO analysis_cycles "
        "(cycle_id, started_at, completed_at, bankroll_config_id, "
        "analyses_total, analyses_with_positive_kelly, "
        "analyses_clamped_by_cap, analyses_clamped_by_liquidity, "
        "duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            cycle_id,
            datetime(2026, 5, 10, tzinfo=UTC),
            datetime(2026, 5, 10, tzinfo=UTC),
            bankroll_id,
            1,
            1,
            0,
            0,
            5.0,
        ],
    )
    conn.execute(
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
            cycle_id,
            cmp_id,
            class_id,
            condition_id,
            bankroll_id,
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
            datetime(2026, 5, 10, tzinfo=UTC),
        ],
    )


def _seed_monitor_cycle(conn: duckdb.DuckDBPyConnection, *, cycle_id: str = "mon-1") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO monitor_cycles "
        "(cycle_id, started_at, completed_at, follow_ups_total, "
        "follow_ups_with_alerts, alerts_by_tier) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            cycle_id,
            datetime(2026, 5, 15, tzinfo=UTC),
            datetime(2026, 5, 15, tzinfo=UTC),
            0,
            0,
            json.dumps({}),
        ],
    )


def _seed_follow_up(
    conn: duckdb.DuckDBPyConnection,
    *,
    cycle_id: str,
    follow_up_id: str,
    analysis_id: str,
    primary_alert_tier: str | None,
    recommended_review: bool,
    resolution_status: str = "unresolved",
) -> None:
    alert_tiers = [primary_alert_tier] if primary_alert_tier else []
    conn.execute(
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
            0.32,
            json.dumps([0.20, 0.45]),
            0.12,
            datetime(2026, 5, 15, 12, tzinfo=UTC),
            0.02,
            "minor",
            0.02,
            "minor",
            json.dumps([]),
            5,
            60,
            False,
            json.dumps([]),
            0,
            resolution_status,
            recommended_review,
            primary_alert_tier,
            json.dumps(alert_tiers),
            "Synthetic reasoning text.",
            False,
            False,
            None,
            datetime(2026, 5, 15, 14, tzinfo=UTC),
        ],
    )


# Suppress unused-import warning for typing helpers kept for expansion.
_ = (Any, timedelta, uuid)
