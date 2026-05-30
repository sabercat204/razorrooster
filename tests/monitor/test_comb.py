"""T-MON-030 / T-MON-031 — cycle orchestrator tests."""

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
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.monitor.config.loader import MonitorConfig, ShiftBandConfig
from razor_rooster.monitor.engines.comb import (
    evaluate_analysis,
    run_cycle,
)
from razor_rooster.monitor.persistence.migrations import (
    run_pending_monitor_migrations,
)
from razor_rooster.monitor.persistence.operations import (
    query_cycle,
    query_follow_ups,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.position_engine.models import Analysis, AnalysisTrace
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import (
    append_watch_state,
    persist_analysis,
    persist_analysis_trace,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)

# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "monitor_comb.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        run_pending_mispricing_migrations(conn)
        run_pending_position_engine_migrations(conn)
        run_pending_monitor_migrations(conn)
    yield s
    s.close()


def _config() -> MonitorConfig:
    return MonitorConfig(
        default_bands=ShiftBandConfig(
            minor_threshold=0.01,
            material_threshold=0.05,
            major_threshold=0.15,
        ),
        time_decay_alert_days=7,
    )


def _seed_class(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str = "cls-1",
    domain_sector: str = "geopolitics",
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO pl_event_classes "
        "(class_id, title, description, domain_sector, definition_version, "
        "outcome_type, registered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            class_id,
            "Test Class",
            "synthetic test class",
            domain_sector,
            1,
            "binary",
            datetime(2026, 5, 1, tzinfo=UTC),
        ],
    )


def _seed_market(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str = "0xMARKET",
    resolved: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO polymarket_markets "
        "(source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, source_payload_json, superseded_at, condition_id, "
        "slug, question, market_type, outcome_tokens, end_date, "
        "active, closed, resolved) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "polymarket",
            condition_id,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
            "1.0",
            json.dumps({}),
            condition_id,
            "test-market",
            "Test market?",
            "binary",
            json.dumps(
                [{"token_id": "tok-yes", "label": "Yes"}, {"token_id": "tok-no", "label": "No"}]
            ),
            datetime(2027, 1, 1, tzinfo=UTC),
            True,
            False,
            resolved,
        ],
    )


def _seed_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str = "0xMARKET",
    winning_label: str = "yes",
) -> None:
    conn.execute(
        "INSERT INTO polymarket_resolutions "
        "(source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, source_payload_json, superseded_at, condition_id, "
        "winning_outcome_token_id, winning_outcome_label, resolution_ts, "
        "resolution_source, invalidated) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, FALSE)",
        [
            "polymarket",
            f"res-{condition_id}",
            datetime(2026, 5, 14, tzinfo=UTC),
            datetime(2026, 5, 14, tzinfo=UTC),
            "1.0",
            json.dumps({}),
            condition_id,
            "tok-yes",
            winning_label,
            datetime(2026, 5, 14, tzinfo=UTC),
            "polymarket-api",
        ],
    )


def _seed_price_snapshot(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str = "0xMARKET",
    outcome_token_id: str = "tok-yes",
    mid_price: float = 0.10,
    when: datetime | None = None,
) -> None:
    ts = when or datetime(2026, 5, 15, 12, tzinfo=UTC)
    conn.execute(
        "INSERT INTO polymarket_price_snapshots "
        "(source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, source_payload_json, superseded_at, condition_id, "
        "outcome_token_id, snapshot_ts, snapshot_source, mid_price) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
        [
            "polymarket",
            f"snap-{uuid.uuid4()}",
            ts,
            ts,
            "1.0",
            json.dumps({}),
            condition_id,
            outcome_token_id,
            ts,
            "rest",
            mid_price,
        ],
    )


def _seed_scan_record(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str = "cls-1",
    posterior: float = 0.30,
    scan_id: str | None = None,
    when: datetime | None = None,
    precursors: list[dict[str, Any]] | None = None,
) -> str:
    sid = scan_id or f"scan-{uuid.uuid4()}"
    ts = when or datetime(2026, 5, 15, 12, tzinfo=UTC)
    conn.execute(
        "INSERT INTO scan_summaries "
        "(scan_id, scan_started_at, scan_completed_at, pattern_library_version, "
        "classes_total, classes_succeeded, classes_failed, classes_skipped, "
        "candidates_count, library_stale_warning) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [sid, ts, ts, 1, 1, 1, 0, 0, 0, False],
    )
    conn.execute(
        "INSERT INTO scan_records "
        "(scan_id, class_id, class_definition_version, pattern_library_version, "
        "data_as_of, scan_started_at, scan_completed_at, "
        "base_rate, base_rate_ci_lower, base_rate_ci_upper, "
        "posterior, posterior_ci_lower, posterior_ci_upper, "
        "log_odds_shift, is_candidate, "
        "library_stale_warning, source_stale_warning, definition_drift_warning, "
        "low_signature_confidence, no_update_applied) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            sid,
            class_id,
            1,
            1,
            ts,
            ts,
            ts,
            0.10,
            0.05,
            0.18,
            posterior,
            max(0.0, posterior - 0.10),
            min(1.0, posterior + 0.10),
            0.5,
            posterior > 0.20,
            False,
            False,
            False,
            False,
            False,
        ],
    )
    if precursors is not None:
        conn.execute(
            "INSERT INTO scan_traces (scan_id, class_id, trace_json) VALUES (?, ?, ?)",
            [sid, class_id, json.dumps({"precursors": precursors})],
        )
    return sid


def _seed_comparison(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str = "comp-1",
    class_id: str = "cls-1",
    condition_id: str = "0xMARKET",
    outcome_token_id: str = "tok-yes",
    polarity: str = "aligned",
    cycle_id: str = "mp-cycle-1",
    mapping_id: str = "map-1",
    scan_id: str = "scan-1",
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO comparison_cycles "
        "(cycle_id, started_at, completed_at, comparisons_total, "
        "surfaced_count, suppressed_breakdown, library_version_at_cycle, "
        "scan_id_consumed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            cycle_id,
            datetime(2026, 5, 10, tzinfo=UTC),
            datetime(2026, 5, 10, tzinfo=UTC),
            1,
            1,
            json.dumps({}),
            1,
            scan_id,
        ],
    )
    conn.execute(
        "INSERT OR IGNORE INTO class_market_mappings "
        "(mapping_id, class_id, condition_id, mapping_type, mapping_confidence, "
        "polarity, mapped_by, mapped_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            mapping_id,
            class_id,
            condition_id,
            "direct",
            "exact",
            polarity,
            "operator",
            datetime(2026, 5, 1, tzinfo=UTC),
            "test",
        ],
    )
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
            cycle_id,
            mapping_id,
            class_id,
            condition_id,
            outcome_token_id,
            polarity,
            scan_id,
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
            0.06,
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
            datetime(2026, 5, 10, tzinfo=UTC),
        ],
    )


def _make_analysis(
    *,
    analysis_id: str = "an-1",
    cycle_id: str = "pe-cy-1",
    comparison_id: str = "comp-1",
    class_id: str = "cls-1",
    condition_id: str = "0xMARKET",
    bankroll_id: str = "bk-1",
    model_probability: float = 0.30,
    market_probability: float | None = 0.10,
    invalidation_criteria: tuple[dict[str, Any], ...] = (),
    days_to_resolution: int | None = 60,
    when: datetime | None = None,
) -> Analysis:
    return Analysis(
        analysis_id=analysis_id,
        cycle_id=cycle_id,
        comparison_id=comparison_id,
        class_id=class_id,
        condition_id=condition_id,
        bankroll_config_id=bankroll_id,
        model_probability=model_probability,
        market_probability=market_probability,
        kelly_unclamped=0.05,
        kelly_negative=False,
        kelly_clamped_by_max_cap=False,
        kelly_clamped_by_liquidity=False,
        suggested_fraction=0.05,
        suggested_dollar_size=50.0,
        ev_per_dollar=0.20,
        bankroll_after_1_loss_pct=95.0,
        bankroll_after_3_losses_pct=85.74,
        bankroll_after_5_losses_pct=77.38,
        suggested_pct_of_24h_volume=0.05,
        days_to_resolution=days_to_resolution,
        long_time_to_resolution=False,
        sub_threshold=False,
        sensitivity_analysis=None,
        invalidation_criteria=invalidation_criteria,
        low_signature_confidence=False,
        source_stale_warning=False,
        library_stale_warning=False,
        definition_drift_warning=False,
        low_mapping_confidence=False,
        low_liquidity=False,
        error=None,
        computed_at=when or datetime(2026, 5, 10, tzinfo=UTC),
    )


def _seed_bankroll(conn: duckdb.DuckDBPyConnection, *, config_id: str = "bk-1") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO bankroll_config "
        "(config_id, analytical_bankroll_usd, max_single_position_pct, "
        "kelly_fraction_default, min_edge_threshold, effective_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            config_id,
            1000.0,
            0.10,
            0.5,
            0.02,
            datetime(2026, 5, 1, tzinfo=UTC),
            "operator",
        ],
    )


def _seed_pe_cycle(conn: duckdb.DuckDBPyConnection, *, cycle_id: str = "pe-cy-1") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO analysis_cycles "
        "(cycle_id, started_at, completed_at, bankroll_config_id, "
        "analyses_total, analyses_with_positive_kelly, "
        "analyses_clamped_by_cap, analyses_clamped_by_liquidity, "
        "duration_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            cycle_id,
            datetime(2026, 5, 10, tzinfo=UTC),
            datetime(2026, 5, 10, tzinfo=UTC),
            "bk-1",
            1,
            1,
            0,
            0,
            5.0,
        ],
    )


# -- evaluate_analysis tests ------------------------------------------------


def test_evaluate_analysis_unresolved_minor_shift(store: DuckDBStore) -> None:
    """Baseline: unresolved market with minor shifts produces no alert."""
    with store.connection() as conn:
        _seed_class(conn)
        _seed_market(conn)
        _seed_bankroll(conn)
        _seed_pe_cycle(conn)
        _seed_comparison(conn)
        _seed_scan_record(conn, posterior=0.31, precursors=[])
        _seed_price_snapshot(conn, mid_price=0.105)
        analysis = _make_analysis()
        follow_up = evaluate_analysis(
            conn,
            cycle_id="cy-monitor-1",
            analysis=analysis,
            config=_config(),
            now=datetime(2026, 5, 15, tzinfo=UTC),
        )
    assert follow_up.resolution_status == "unresolved"
    assert follow_up.primary_alert_tier is None
    assert follow_up.recommended_review is False
    assert follow_up.current_model_p == pytest.approx(0.31)
    assert follow_up.model_shift_band == "minor"


def test_evaluate_analysis_material_model_shift_flags_review(store: DuckDBStore) -> None:
    with store.connection() as conn:
        _seed_class(conn)
        _seed_market(conn)
        _seed_bankroll(conn)
        _seed_pe_cycle(conn)
        _seed_comparison(conn)
        _seed_scan_record(conn, posterior=0.40)  # 0.10 above analysis -> material
        _seed_price_snapshot(conn, mid_price=0.10)
        analysis = _make_analysis()
        follow_up = evaluate_analysis(
            conn,
            cycle_id="cy-monitor-1",
            analysis=analysis,
            config=_config(),
            now=datetime(2026, 5, 15, tzinfo=UTC),
        )
    assert follow_up.recommended_review is True
    assert follow_up.primary_alert_tier == "material_shift"
    assert "material_shift" in follow_up.alert_tiers


def test_evaluate_analysis_resolution_short_circuits(store: DuckDBStore) -> None:
    """When market is resolved, follow-up bypasses change detection."""
    with store.connection() as conn:
        _seed_class(conn)
        _seed_market(conn, resolved=True)
        _seed_resolution(conn, winning_label="yes")
        _seed_bankroll(conn)
        _seed_pe_cycle(conn)
        _seed_comparison(conn)
        _seed_scan_record(conn, posterior=0.40)
        _seed_price_snapshot(conn, mid_price=0.99)
        analysis = _make_analysis()
        follow_up = evaluate_analysis(
            conn,
            cycle_id="cy-monitor-1",
            analysis=analysis,
            config=_config(),
            now=datetime(2026, 5, 15, tzinfo=UTC),
        )
    assert follow_up.resolution_status == "resolved_yes"
    assert follow_up.primary_alert_tier == "resolution"
    assert follow_up.recommended_review is True
    # Detection fields are NULL on a resolution short-circuit.
    assert follow_up.current_model_p is None
    assert follow_up.model_shift_band is None
    assert follow_up.precursor_snapshot == ()


def test_evaluate_analysis_resolution_inverts_with_polarity(store: DuckDBStore) -> None:
    """Inverted polarity flips yes/no in resolution_status."""
    with store.connection() as conn:
        _seed_class(conn)
        _seed_market(conn, resolved=True)
        _seed_resolution(conn, winning_label="yes")
        _seed_bankroll(conn)
        _seed_pe_cycle(conn)
        _seed_comparison(conn, polarity="inverted")
        analysis = _make_analysis()
        follow_up = evaluate_analysis(
            conn,
            cycle_id="cy-monitor-1",
            analysis=analysis,
            config=_config(),
            now=datetime(2026, 5, 15, tzinfo=UTC),
        )
    assert follow_up.resolution_status == "resolved_no"


def test_evaluate_analysis_invalidation_market_move_triggered(store: DuckDBStore) -> None:
    criterion = {
        "type": "market_move",
        "direction": "market_p_rises_to",
        "threshold": 0.30,
        "description": "Market price rises to 0.30",
    }
    with store.connection() as conn:
        _seed_class(conn)
        _seed_market(conn)
        _seed_bankroll(conn)
        _seed_pe_cycle(conn)
        _seed_comparison(conn)
        _seed_scan_record(conn, posterior=0.30)
        _seed_price_snapshot(conn, mid_price=0.35)
        analysis = _make_analysis(invalidation_criteria=(criterion,))
        follow_up = evaluate_analysis(
            conn,
            cycle_id="cy-monitor-1",
            analysis=analysis,
            config=_config(),
            now=datetime(2026, 5, 15, tzinfo=UTC),
        )
    assert follow_up.invalidation_triggered_count == 1
    assert follow_up.primary_alert_tier == "invalidation_triggered"
    assert follow_up.recommended_review is True


def test_evaluate_analysis_time_decay_alert(store: DuckDBStore) -> None:
    with store.connection() as conn:
        _seed_class(conn)
        _seed_market(conn)
        _seed_bankroll(conn)
        _seed_pe_cycle(conn)
        _seed_comparison(conn)
        _seed_scan_record(conn, posterior=0.30)
        _seed_price_snapshot(conn, mid_price=0.10)
        analysis = _make_analysis(days_to_resolution=3)
        follow_up = evaluate_analysis(
            conn,
            cycle_id="cy-monitor-1",
            analysis=analysis,
            config=_config(),
            now=datetime(2026, 5, 15, tzinfo=UTC),
        )
    assert follow_up.time_decay_alert is True
    assert follow_up.recommended_review is True
    assert follow_up.primary_alert_tier == "time_decay"


def test_evaluate_analysis_uses_analysis_trace_precursors(store: DuckDBStore) -> None:
    """When the analysis trace contains a precursors list, snapshots are paired."""
    with store.connection() as conn:
        _seed_class(conn)
        _seed_market(conn)
        _seed_bankroll(conn)
        _seed_pe_cycle(conn)
        _seed_comparison(conn)
        # Current scan precursor changed state since analysis time.
        _seed_scan_record(
            conn,
            posterior=0.30,
            precursors=[
                {
                    "variable_id": "v1",
                    "title": "V1",
                    "threshold": 5.0,
                    "direction": "high_signals_event",
                    "current_value": 6.5,
                    "fired": True,
                }
            ],
        )
        _seed_price_snapshot(conn, mid_price=0.10)
        analysis = _make_analysis()
        # Persist analysis + analysis trace with snapshot of precursor.
        persist_analysis(conn, analysis)
        persist_analysis_trace(
            conn,
            AnalysisTrace(
                analysis_id=analysis.analysis_id,
                rendered_text="synthetic",
                structured_dict={
                    "precursors": [
                        {
                            "variable_id": "v1",
                            "title": "V1",
                            "threshold": 5.0,
                            "direction": "high_signals_event",
                            "current_value": 4.0,
                            "fired": False,
                        }
                    ]
                },
            ),
        )
        follow_up = evaluate_analysis(
            conn,
            cycle_id="cy-monitor-1",
            analysis=analysis,
            config=_config(),
            now=datetime(2026, 5, 15, tzinfo=UTC),
        )
    assert len(follow_up.precursor_snapshot) == 1
    snap = follow_up.precursor_snapshot[0]
    assert snap["variable_id"] == "v1"
    assert snap["threshold_crossed"] is True
    # Precursor crossing alone with no material shift triggers
    # precursor_shift tier and review.
    assert follow_up.primary_alert_tier == "precursor_shift"
    assert follow_up.recommended_review is False  # precursor alone doesn't force review
    assert "precursor_shift" in follow_up.alert_tiers


# -- run_cycle tests --------------------------------------------------------


def _seed_full_analysis(
    conn: duckdb.DuckDBPyConnection,
    *,
    analysis_id: str = "an-1",
    state: str = "watching",
    market_resolved: bool = False,
    posterior_now: float = 0.31,
    invalidation_criteria: tuple[dict[str, Any], ...] = (),
) -> Analysis:
    """Persist one full upstream chain culminating in a watched analysis."""
    class_id = f"cls-{analysis_id}"
    condition_id = f"cond-{analysis_id}"
    comparison_id = f"comp-{analysis_id}"
    mapping_id = f"map-{analysis_id}"
    _seed_class(conn, class_id=class_id)
    _seed_market(conn, condition_id=condition_id, resolved=market_resolved)
    if market_resolved:
        _seed_resolution(conn, condition_id=condition_id)
    _seed_bankroll(conn)
    _seed_pe_cycle(conn)
    _seed_comparison(
        conn,
        comparison_id=comparison_id,
        class_id=class_id,
        condition_id=condition_id,
        mapping_id=mapping_id,
    )
    _seed_scan_record(conn, class_id=class_id, posterior=posterior_now)
    _seed_price_snapshot(conn, condition_id=condition_id, mid_price=0.10)
    analysis = _make_analysis(
        analysis_id=analysis_id,
        comparison_id=comparison_id,
        class_id=class_id,
        condition_id=condition_id,
        invalidation_criteria=invalidation_criteria,
    )
    persist_analysis(conn, analysis)
    append_watch_state(
        conn,
        analysis_id=analysis_id,
        state=state,  # type: ignore[arg-type]
        when=datetime(2026, 5, 11, tzinfo=UTC),
    )
    return analysis


def test_run_cycle_iterates_all_watched_and_acted_on(store: DuckDBStore) -> None:
    with store.connection() as conn:
        _seed_full_analysis(conn, analysis_id="an-watching", state="watching")
        _seed_full_analysis(conn, analysis_id="an-acted", state="acted_on")
        _seed_full_analysis(conn, analysis_id="an-passed", state="passed")
    report = run_cycle(store, config=_config(), now=datetime(2026, 5, 15, tzinfo=UTC))
    assert report.follow_ups_total == 2
    assert len(report.errors) == 0
    with store.connection() as conn:
        cycle = query_cycle(conn, cycle_id=report.cycle_id)
        follow_ups = query_follow_ups(conn, cycle_id=report.cycle_id)
    assert cycle is not None
    assert cycle.follow_ups_total == 2
    assert {f.analysis_id for f in follow_ups} == {"an-watching", "an-acted"}


def test_run_cycle_failure_isolation(store: DuckDBStore) -> None:
    """When one analysis evaluation fails, others still complete."""
    with store.connection() as conn:
        good = _seed_full_analysis(conn, analysis_id="an-good", state="watching")
        # Create a watch_states entry for an analysis that never got
        # persisted to analyses — get_analysis returns None and the
        # evaluator skips it without erroring out.
        append_watch_state(
            conn,
            analysis_id="an-missing",
            state="watching",
            when=datetime(2026, 5, 11, tzinfo=UTC),
        )
    report = run_cycle(store, config=_config(), now=datetime(2026, 5, 15, tzinfo=UTC))
    # The missing analysis is skipped silently (warned), good completes.
    assert report.follow_ups_total == 1
    with store.connection() as conn:
        follow_ups = query_follow_ups(conn, cycle_id=report.cycle_id)
    assert len(follow_ups) == 1
    assert follow_ups[0].analysis_id == good.analysis_id


def test_run_cycle_resolution_triggers_expiration(store: DuckDBStore) -> None:
    """Resolution detection triggers position_engine expiration pass.

    The expiration pass uses comparison_resolutions, so we seed that
    table as well to mirror what mispricing_detector would do.
    """
    with store.connection() as conn:
        _seed_full_analysis(conn, analysis_id="an-resolved", state="watching", market_resolved=True)
        # Seed comparison_resolutions so position_engine.run_expiration_pass
        # finds the resolved comparison and expires the watch state.
        conn.execute(
            "INSERT INTO comparison_resolutions "
            "(comparison_id, condition_id, resolution_outcome, resolution_ts, "
            "model_probability_at_comparison, market_probability_at_comparison, "
            "polarity_at_comparison, outcome_observed, linked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "comp-an-resolved",
                "cond-an-resolved",
                "yes",
                datetime(2026, 5, 14, tzinfo=UTC),
                0.30,
                0.10,
                "aligned",
                1,
                datetime(2026, 5, 14, tzinfo=UTC),
            ],
        )
    report = run_cycle(store, config=_config(), now=datetime(2026, 5, 15, tzinfo=UTC))
    assert report.resolutions_detected == 1
    assert report.expirations_written >= 1
    # Watch state should now be 'expired'.
    with store.connection() as conn:
        latest = conn.execute(
            "SELECT state FROM watch_states WHERE analysis_id = ? ORDER BY set_at DESC LIMIT 1",
            ["an-resolved"],
        ).fetchone()
    assert latest is not None
    assert latest[0] == "expired"


def test_run_cycle_writes_alerts_by_tier(store: DuckDBStore) -> None:
    """Cycle aggregates correctly count alerts by primary tier."""
    with store.connection() as conn:
        # 1 material_shift, 1 quiet, 1 time_decay
        _seed_class(conn, class_id="cls-shift")
        _seed_market(conn, condition_id="cond-shift")
        _seed_bankroll(conn)
        _seed_pe_cycle(conn)
        _seed_comparison(
            conn,
            comparison_id="comp-shift",
            class_id="cls-shift",
            condition_id="cond-shift",
            mapping_id="map-shift",
        )
        _seed_scan_record(conn, class_id="cls-shift", posterior=0.40)
        _seed_price_snapshot(conn, condition_id="cond-shift")
        a1 = _make_analysis(
            analysis_id="an-shift",
            comparison_id="comp-shift",
            class_id="cls-shift",
            condition_id="cond-shift",
        )
        persist_analysis(conn, a1)
        append_watch_state(
            conn,
            analysis_id="an-shift",
            state="watching",
            when=datetime(2026, 5, 11, tzinfo=UTC),
        )

        _seed_full_analysis(conn, analysis_id="an-quiet", state="watching")

        _seed_class(conn, class_id="cls-time")
        _seed_market(conn, condition_id="cond-time")
        _seed_comparison(
            conn,
            comparison_id="comp-time",
            class_id="cls-time",
            condition_id="cond-time",
            mapping_id="map-time",
        )
        _seed_scan_record(conn, class_id="cls-time", posterior=0.30)
        _seed_price_snapshot(conn, condition_id="cond-time")
        a3 = _make_analysis(
            analysis_id="an-time",
            comparison_id="comp-time",
            class_id="cls-time",
            condition_id="cond-time",
            days_to_resolution=2,
        )
        persist_analysis(conn, a3)
        append_watch_state(
            conn,
            analysis_id="an-time",
            state="watching",
            when=datetime(2026, 5, 11, tzinfo=UTC),
        )
    report = run_cycle(store, config=_config(), now=datetime(2026, 5, 15, tzinfo=UTC))
    assert report.follow_ups_total == 3
    # Two of the three flagged: material_shift, time_decay.
    assert report.follow_ups_with_alerts == 2
    assert report.alerts_by_tier.get("material_shift") == 1
    assert report.alerts_by_tier.get("time_decay") == 1


def test_run_cycle_completes_with_no_watched_analyses(store: DuckDBStore) -> None:
    report = run_cycle(store, config=_config(), now=datetime(2026, 5, 15, tzinfo=UTC))
    assert report.follow_ups_total == 0
    assert report.completed_at is not None
    assert report.duration_seconds is not None and report.duration_seconds >= 0
    with store.connection() as conn:
        cycle = query_cycle(conn, cycle_id=report.cycle_id)
    assert cycle is not None
    assert cycle.follow_ups_total == 0


# -- regression: long-elapsed analysis (days_since_analysis) -----------------


def test_evaluate_analysis_days_since_uses_analysis_computed_at(
    store: DuckDBStore,
) -> None:
    with store.connection() as conn:
        _seed_class(conn)
        _seed_market(conn)
        _seed_bankroll(conn)
        _seed_pe_cycle(conn)
        _seed_comparison(conn)
        _seed_scan_record(conn, posterior=0.30)
        _seed_price_snapshot(conn, mid_price=0.10)
        analysis = _make_analysis(when=datetime(2026, 5, 5, tzinfo=UTC))  # 10 days before now
        follow_up = evaluate_analysis(
            conn,
            cycle_id="cy",
            analysis=analysis,
            config=_config(),
            now=datetime(2026, 5, 15, tzinfo=UTC),
        )
    assert follow_up.days_since_analysis == 10


# Suppress unused-import warning for timedelta (kept for future use).
_ = timedelta
