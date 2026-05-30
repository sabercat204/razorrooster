"""T-MON-080 — monitor end-to-end acceptance test.

Verifies the acceptance criteria from MONITOR.md §8:

- Daily monitor cycle runs end-to-end.
- Multiple cycles produce queryable trajectory data per analysis.
- Resolution-on-cycle: a watched analysis transitions from
  ``'watching'`` to ``'expired'`` when the underlying market resolves.
- Failure isolation: a broken analysis does not stop the cycle.
- Alert ordering by tier priority is preserved.

This test composes a synthetic upstream chain (pattern library →
signal scanner → mispricing detector → position engine analyses with
watch states) directly via SQL fixtures instead of running each
upstream subsystem's full cycle. The position_engine E2E test
already covers the full-chain composition; this test focuses on
monitor-specific behavior layered on top.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.monitor.engines.comb import run_cycle
from razor_rooster.monitor.persistence.migrations import (
    run_pending_monitor_migrations,
)
from razor_rooster.monitor.persistence.operations import (
    query_alerts,
    query_cycle,
    query_follow_ups,
    query_trajectory,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.position_engine.models import Analysis
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import (
    append_watch_state,
    persist_analysis,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "monitor_e2e.duckdb"
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


def _seed_class(conn: duckdb.DuckDBPyConnection, class_id: str) -> None:
    conn.execute(
        "INSERT INTO pl_event_classes "
        "(class_id, title, description, domain_sector, definition_version, "
        "outcome_type, registered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            class_id,
            f"Class {class_id}",
            "synthetic E2E class",
            "geopolitics",
            1,
            "binary",
            datetime(2026, 5, 1, tzinfo=UTC),
        ],
    )


def _seed_market(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str,
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
            f"slug-{condition_id}",
            "Resolved synthetic market?",
            "binary",
            json.dumps([{"token_id": f"{condition_id}-yes", "label": "Yes"}]),
            datetime(2027, 1, 1, tzinfo=UTC),
            True,
            resolved,
            resolved,
        ],
    )


def _seed_resolution(
    conn: duckdb.DuckDBPyConnection, *, condition_id: str, label: str = "yes"
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
            f"{condition_id}-yes",
            label,
            datetime(2026, 5, 14, tzinfo=UTC),
            "polymarket-api",
        ],
    )


def _seed_scan(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str,
    posterior: float,
    when: datetime,
) -> str:
    scan_id = f"scan-{uuid.uuid4()}"
    conn.execute(
        "INSERT INTO scan_summaries "
        "(scan_id, scan_started_at, scan_completed_at, pattern_library_version, "
        "classes_total, classes_succeeded, classes_failed, classes_skipped, "
        "candidates_count, library_stale_warning) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [scan_id, when, when, 1, 1, 1, 0, 0, 1, False],
    )
    conn.execute(
        "INSERT INTO scan_records "
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
            when,
            when,
            when,
            0.10,
            0.05,
            0.18,
            posterior,
            max(0.0, posterior - 0.10),
            min(1.0, posterior + 0.10),
            0.5,
            posterior > 0.20,
        ],
    )
    return scan_id


def _seed_price(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str,
    outcome_token_id: str,
    mid_price: float,
    when: datetime,
) -> None:
    conn.execute(
        "INSERT INTO polymarket_price_snapshots "
        "(source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, source_payload_json, superseded_at, condition_id, "
        "outcome_token_id, snapshot_ts, snapshot_source, mid_price) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
        [
            "polymarket",
            f"snap-{uuid.uuid4()}",
            when,
            when,
            "1.0",
            json.dumps({}),
            condition_id,
            outcome_token_id,
            when,
            "rest",
            mid_price,
        ],
    )


def _seed_full_chain(
    conn: duckdb.DuckDBPyConnection,
    *,
    analysis_id: str,
    class_id: str,
    condition_id: str,
    state: str = "watching",
    market_resolved: bool = False,
    posterior_now: float = 0.30,
    market_p_now: float = 0.10,
    invalidation_criteria: tuple[dict[str, object], ...] = (),
    days_to_resolution: int | None = 60,
    when: datetime | None = None,
) -> Analysis:
    """Seed one full upstream chain ending in a watched analysis."""
    moment = when or datetime(2026, 5, 15, 12, tzinfo=UTC)
    outcome_token_id = f"{condition_id}-yes"
    _seed_class(conn, class_id)
    _seed_market(conn, condition_id=condition_id, resolved=market_resolved)
    if market_resolved:
        _seed_resolution(conn, condition_id=condition_id)
    # Bankroll + cycle (idempotent).
    conn.execute(
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
    conn.execute(
        "INSERT OR IGNORE INTO analysis_cycles "
        "(cycle_id, started_at, completed_at, bankroll_config_id, "
        "analyses_total, analyses_with_positive_kelly, "
        "analyses_clamped_by_cap, analyses_clamped_by_liquidity, "
        "duration_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "pe-cy-1",
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
    conn.execute(
        "INSERT OR IGNORE INTO comparison_cycles "
        "(cycle_id, started_at, completed_at, comparisons_total, "
        "surfaced_count, suppressed_breakdown, library_version_at_cycle, "
        "scan_id_consumed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "mp-cycle-1",
            datetime(2026, 5, 10, tzinfo=UTC),
            datetime(2026, 5, 10, tzinfo=UTC),
            1,
            1,
            json.dumps({}),
            1,
            "scan-original",
        ],
    )
    mapping_id = f"map-{analysis_id}"
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
            "aligned",
            "operator",
            datetime(2026, 5, 1, tzinfo=UTC),
            "synthetic",
        ],
    )
    comparison_id = f"comp-{analysis_id}"
    conn.execute(
        "INSERT INTO comparisons "
        "(comparison_id, cycle_id, mapping_id, class_id, condition_id, "
        "outcome_token_id, polarity, scan_id, model_probability, "
        "model_ci_lower, model_ci_upper, market_probability, "
        "market_best_bid, market_best_ask, market_last_trade_price, "
        "market_volume_24h, market_spread_bps, market_snapshot_ts, "
        "delta, log_odds_delta, ci_overlap, expected_value, "
        "confidence_weighted_score, surfaced, suppression_reasons, "
        "computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            "mp-cycle-1",
            mapping_id,
            class_id,
            condition_id,
            outcome_token_id,
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
            0.06,
            True,
            json.dumps([]),
            datetime(2026, 5, 10, tzinfo=UTC),
        ],
    )
    # Current scan + price snapshot (the "now" state monitor will read).
    _seed_scan(conn, class_id=class_id, posterior=posterior_now, when=moment)
    _seed_price(
        conn,
        condition_id=condition_id,
        outcome_token_id=outcome_token_id,
        mid_price=market_p_now,
        when=moment,
    )
    analysis = Analysis(
        analysis_id=analysis_id,
        cycle_id="pe-cy-1",
        comparison_id=comparison_id,
        class_id=class_id,
        condition_id=condition_id,
        bankroll_config_id="bk-1",
        model_probability=0.30,
        market_probability=0.10,
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
        computed_at=datetime(2026, 5, 10, tzinfo=UTC),
    )
    persist_analysis(conn, analysis)
    append_watch_state(
        conn,
        analysis_id=analysis_id,
        state=state,  # type: ignore[arg-type]
        when=datetime(2026, 5, 11, tzinfo=UTC),
    )
    return analysis


# -- E2E tests --------------------------------------------------------------


def test_monitor_cycle_end_to_end(store: DuckDBStore) -> None:
    """A daily monitor cycle runs over all watched + acted-on analyses."""
    with store.connection() as conn:
        # Mix: one quiet, one material shift, one time decay,
        # one acted_on with criterion triggered, one passed (excluded).
        _seed_full_chain(
            conn,
            analysis_id="an-quiet",
            class_id="cls-quiet",
            condition_id="cond-quiet",
            posterior_now=0.31,
        )
        _seed_full_chain(
            conn,
            analysis_id="an-shift",
            class_id="cls-shift",
            condition_id="cond-shift",
            posterior_now=0.45,  # +0.15 -> major shift
        )
        _seed_full_chain(
            conn,
            analysis_id="an-time",
            class_id="cls-time",
            condition_id="cond-time",
            posterior_now=0.30,
            days_to_resolution=2,
        )
        _seed_full_chain(
            conn,
            analysis_id="an-acted",
            class_id="cls-acted",
            condition_id="cond-acted",
            state="acted_on",
            posterior_now=0.30,
            market_p_now=0.40,
            invalidation_criteria=(
                {
                    "type": "market_move",
                    "direction": "market_p_rises_to",
                    "threshold": 0.30,
                    "description": "market_p rises to 0.30",
                },
            ),
        )
        _seed_full_chain(
            conn,
            analysis_id="an-passed",
            class_id="cls-passed",
            condition_id="cond-passed",
            state="passed",
        )

    report = run_cycle(store, now=datetime(2026, 5, 15, tzinfo=UTC))

    # Acceptance: cycle iterates watched + acted_on, skips passed.
    assert report.follow_ups_total == 4
    assert report.follow_ups_with_alerts >= 3

    with store.connection() as conn:
        all_follow_ups = query_follow_ups(conn, cycle_id=report.cycle_id)
    analysis_ids = {f.analysis_id for f in all_follow_ups}
    assert analysis_ids == {"an-quiet", "an-shift", "an-time", "an-acted"}

    # Tier ordering — verify alerts sorted correctly.
    with store.connection() as conn:
        ordered = query_alerts(conn)
    primary_order = [a.primary_alert_tier for a in ordered]
    # invalidation_triggered first (an-acted), then material_shift (an-shift),
    # then time_decay (an-time).
    assert primary_order[0] == "invalidation_triggered"
    assert "material_shift" in primary_order
    assert "time_decay" in primary_order


def test_monitor_multiple_cycles_produce_trajectory(store: DuckDBStore) -> None:
    """Trajectory queries over multiple cycles return chronological history."""
    with store.connection() as conn:
        _seed_full_chain(
            conn,
            analysis_id="an-watch",
            class_id="cls-watch",
            condition_id="cond-watch",
            posterior_now=0.35,
            when=datetime(2026, 5, 13, 12, tzinfo=UTC),
        )

    # Cycle 1.
    report1 = run_cycle(store, now=datetime(2026, 5, 13, 14, tzinfo=UTC))
    # Update synthetic 'current' to a higher posterior for cycle 2.
    with store.connection() as conn:
        _seed_scan(
            conn,
            class_id="cls-watch",
            posterior=0.42,
            when=datetime(2026, 5, 14, 12, tzinfo=UTC),
        )
        _seed_price(
            conn,
            condition_id="cond-watch",
            outcome_token_id="cond-watch-yes",
            mid_price=0.20,
            when=datetime(2026, 5, 14, 12, tzinfo=UTC),
        )
    report2 = run_cycle(store, now=datetime(2026, 5, 14, 14, tzinfo=UTC))

    # Both cycles should produce one follow-up apiece.
    assert report1.follow_ups_total == 1
    assert report2.follow_ups_total == 1

    with store.connection() as conn:
        history = query_trajectory(conn, analysis_id="an-watch")
    assert len(history) == 2
    # Chronological order.
    assert history[0].computed_at is not None
    assert history[1].computed_at is not None
    assert history[0].computed_at <= history[1].computed_at
    # Posterior tracked across cycles.
    assert history[0].current_model_p == pytest.approx(0.35)
    assert history[1].current_model_p == pytest.approx(0.42)


def test_monitor_resolution_triggers_expiration_interlock(
    store: DuckDBStore,
) -> None:
    """Resolution detection in monitor expires the matching watch state."""
    with store.connection() as conn:
        _seed_full_chain(
            conn,
            analysis_id="an-resolved",
            class_id="cls-resolved",
            condition_id="cond-resolved",
            market_resolved=True,
        )
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
                "cond-resolved",
                "yes",
                datetime(2026, 5, 14, tzinfo=UTC),
                0.30,
                0.10,
                "aligned",
                1,
                datetime(2026, 5, 14, tzinfo=UTC),
            ],
        )

    report = run_cycle(store, now=datetime(2026, 5, 15, tzinfo=UTC))
    assert report.resolutions_detected == 1
    assert report.expirations_written >= 1

    # Watch state transitioned to expired.
    with store.connection() as conn:
        latest = conn.execute(
            "SELECT state FROM watch_states WHERE analysis_id = ? ORDER BY set_at DESC LIMIT 1",
            ["an-resolved"],
        ).fetchone()
    assert latest is not None
    assert latest[0] == "expired"

    # Follow-up tagged as resolution.
    with store.connection() as conn:
        history = query_trajectory(conn, analysis_id="an-resolved")
    assert len(history) == 1
    assert history[0].resolution_status == "resolved_yes"
    assert history[0].primary_alert_tier == "resolution"


def test_monitor_failure_isolation_with_missing_analysis(
    store: DuckDBStore,
) -> None:
    """Monitor cycle continues when one analysis cannot be evaluated."""
    with store.connection() as conn:
        _seed_full_chain(
            conn,
            analysis_id="an-good",
            class_id="cls-good",
            condition_id="cond-good",
            posterior_now=0.32,
        )
        # Stray watch_state pointing at an analysis that was never persisted.
        append_watch_state(
            conn,
            analysis_id="an-ghost",
            state="watching",
            when=datetime(2026, 5, 11, tzinfo=UTC),
        )

    report = run_cycle(store, now=datetime(2026, 5, 15, tzinfo=UTC))
    # The good analysis still completes; the ghost one is silently skipped.
    assert report.follow_ups_total == 1

    with store.connection() as conn:
        cycle = query_cycle(conn, cycle_id=report.cycle_id)
    assert cycle is not None
    assert cycle.completed_at is not None
    # Aggregate counts on the cycle row match the report.
    assert cycle.follow_ups_total == 1
