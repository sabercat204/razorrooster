"""T-PE-061 — auto-expiration of watch states."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.models import (
    Comparison,
    ComparisonCycle,
    ComparisonResolution,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    persist_comparison,
    write_resolution_link,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    write_cycle as write_comparison_cycle,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.position_engine.models import (
    Analysis,
    AnalysisCycle,
    BankrollConfig,
)
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import (
    append_watch_state,
    latest_watch_state,
    persist_analysis,
    write_bankroll_config,
    write_cycle,
)
from razor_rooster.position_engine.watch.expiration import run_expiration_pass
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "pe_expiration.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_polymarket_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_position_engine_migrations(c)
    try:
        yield s
    finally:
        s.close()


def _seed_analysis_with_resolution(
    store: DuckDBStore,
    *,
    analysis_id: str = "an-1",
    comparison_id: str = "cmp-1",
    condition_id: str = "0xabc",
    initial_state: str = "watching",
    resolution_outcome: str = "yes",
) -> None:
    now = datetime(2026, 5, 15, 12, tzinfo=UTC)
    cfg = BankrollConfig(
        config_id=str(uuid.uuid4()),
        analytical_bankroll_usd=1000.0,
        max_single_position_pct=0.05,
        kelly_fraction_default=0.5,
        min_edge_threshold=0.03,
        effective_at=now,
    )
    with store.connection() as conn:
        write_bankroll_config(conn, cfg)
        write_cycle(
            conn,
            AnalysisCycle(
                cycle_id="cy-1",
                started_at=now,
                completed_at=now,
                bankroll_config_id=cfg.config_id,
                analyses_total=1,
                analyses_with_positive_kelly=1,
                analyses_clamped_by_cap=0,
                analyses_clamped_by_liquidity=0,
            ),
        )
        write_comparison_cycle(
            conn,
            ComparisonCycle(
                cycle_id="cmp-cy-1",
                started_at=now,
                completed_at=now,
                comparisons_total=1,
                surfaced_count=1,
                suppressed_breakdown={},
                library_version_at_cycle=1,
                scan_id_consumed="scan-1",
            ),
        )
        persist_comparison(
            conn,
            Comparison(
                comparison_id=comparison_id,
                cycle_id="cmp-cy-1",
                mapping_id="m-1",
                class_id="cls",
                condition_id=condition_id,
                outcome_token_id="tok-yes",
                polarity="aligned",
                scan_id="scan-1",
                model_probability=0.30,
                model_ci_lower=0.20,
                model_ci_upper=0.40,
                market_probability=0.10,
                market_best_bid=0.09,
                market_best_ask=0.11,
                market_last_trade_price=0.10,
                market_volume_24h=20000.0,
                market_spread_bps=200,
                market_snapshot_ts=now,
                delta=0.20,
                log_odds_delta=1.4,
                ci_overlap=False,
                expected_value=0.20,
                confidence_weighted_score=1.18,
                surfaced=True,
                computed_at=now,
            ),
        )
        persist_analysis(
            conn,
            Analysis(
                analysis_id=analysis_id,
                cycle_id="cy-1",
                comparison_id=comparison_id,
                class_id="cls",
                condition_id=condition_id,
                bankroll_config_id=cfg.config_id,
                model_probability=0.30,
                market_probability=0.10,
                kelly_unclamped=0.20,
                kelly_negative=False,
                kelly_clamped_by_max_cap=False,
                kelly_clamped_by_liquidity=False,
                suggested_fraction=0.025,
                suggested_dollar_size=25.0,
                ev_per_dollar=0.20,
                bankroll_after_1_loss_pct=0.975,
                bankroll_after_3_losses_pct=0.928,
                bankroll_after_5_losses_pct=0.881,
                suggested_pct_of_24h_volume=0.00125,
                days_to_resolution=180,
                long_time_to_resolution=False,
                sub_threshold=False,
                sensitivity_analysis=None,
                invalidation_criteria=(),
                computed_at=now,
            ),
        )
        append_watch_state(
            conn,
            analysis_id=analysis_id,
            state=initial_state,  # type: ignore[arg-type]
        )
        write_resolution_link(
            conn,
            ComparisonResolution(
                comparison_id=comparison_id,
                condition_id=condition_id,
                resolution_outcome=resolution_outcome,  # type: ignore[arg-type]
                resolution_ts=datetime(2026, 6, 1, tzinfo=UTC),
                model_probability_at_comparison=0.30,
                market_probability_at_comparison=0.10,
                polarity_at_comparison="aligned",
                outcome_observed=1 if resolution_outcome == "yes" else 0,
                linked_at=datetime(2026, 6, 1, 0, 1, tzinfo=UTC),
            ),
        )


def test_expiration_transitions_watching_to_expired(store: DuckDBStore) -> None:
    _seed_analysis_with_resolution(store, initial_state="watching")
    report = run_expiration_pass(store, now=datetime(2026, 6, 1, 0, 5, tzinfo=UTC))
    assert report.expirations_written == 1
    with store.connection() as conn:
        latest = latest_watch_state(conn, analysis_id="an-1")
    assert latest is not None
    assert latest.state == "expired"
    assert latest.set_by == "system"


def test_expiration_transitions_acted_on_to_expired(store: DuckDBStore) -> None:
    _seed_analysis_with_resolution(store, initial_state="acted_on")
    report = run_expiration_pass(store, now=datetime(2026, 6, 1, 0, 5, tzinfo=UTC))
    assert report.expirations_written == 1
    with store.connection() as conn:
        latest = latest_watch_state(conn, analysis_id="an-1")
    assert latest is not None
    assert latest.state == "expired"


def test_expiration_skips_dismissed(store: DuckDBStore) -> None:
    """'dismissed' is already terminal; resolution doesn't re-expire it."""
    _seed_analysis_with_resolution(store, initial_state="dismissed")
    report = run_expiration_pass(store, now=datetime(2026, 6, 1, 0, 5, tzinfo=UTC))
    assert report.expirations_written == 0
    with store.connection() as conn:
        latest = latest_watch_state(conn, analysis_id="an-1")
    assert latest is not None
    assert latest.state == "dismissed"


def test_expiration_idempotent(store: DuckDBStore) -> None:
    _seed_analysis_with_resolution(store, initial_state="watching")
    run_expiration_pass(store, now=datetime(2026, 6, 1, 0, 5, tzinfo=UTC))
    second = run_expiration_pass(store, now=datetime(2026, 6, 1, 0, 6, tzinfo=UTC))
    # Second pass: already expired, so no new transitions.
    assert second.expirations_written == 0


def test_expiration_handles_no_resolutions(store: DuckDBStore) -> None:
    """Pass with no comparison_resolutions rows is a clean no-op."""
    report = run_expiration_pass(store, now=datetime(2026, 6, 1, tzinfo=UTC))
    assert report.expirations_written == 0
    assert report.analyses_examined == 0


def test_expiration_handles_no_watch_state(store: DuckDBStore) -> None:
    """An analysis with no watch state ever set is skipped silently."""
    # Seed but DON'T append watch state.
    now = datetime(2026, 5, 15, 12, tzinfo=UTC)
    cfg = BankrollConfig(
        config_id=str(uuid.uuid4()),
        analytical_bankroll_usd=1000.0,
        max_single_position_pct=0.05,
        kelly_fraction_default=0.5,
        min_edge_threshold=0.03,
        effective_at=now,
    )
    with store.connection() as conn:
        write_bankroll_config(conn, cfg)
        write_cycle(
            conn,
            AnalysisCycle(
                cycle_id="cy-2",
                started_at=now,
                completed_at=now,
                bankroll_config_id=cfg.config_id,
                analyses_total=1,
                analyses_with_positive_kelly=0,
                analyses_clamped_by_cap=0,
                analyses_clamped_by_liquidity=0,
            ),
        )
        write_comparison_cycle(
            conn,
            ComparisonCycle(
                cycle_id="cmp-cy-2",
                started_at=now,
                completed_at=now,
                comparisons_total=1,
                surfaced_count=1,
                suppressed_breakdown={},
                library_version_at_cycle=1,
                scan_id_consumed="scan-2",
            ),
        )
        persist_comparison(
            conn,
            Comparison(
                comparison_id="cmp-no-state",
                cycle_id="cmp-cy-2",
                mapping_id="m-1",
                class_id="cls",
                condition_id="0xnostate",
                outcome_token_id="tok-yes",
                polarity="aligned",
                scan_id="scan-2",
                model_probability=0.30,
                model_ci_lower=0.20,
                model_ci_upper=0.40,
                market_probability=0.10,
                market_best_bid=0.09,
                market_best_ask=0.11,
                market_last_trade_price=0.10,
                market_volume_24h=20000.0,
                market_spread_bps=200,
                market_snapshot_ts=now,
                delta=0.20,
                log_odds_delta=1.4,
                ci_overlap=False,
                expected_value=0.20,
                confidence_weighted_score=1.18,
                surfaced=True,
                computed_at=now,
            ),
        )
        persist_analysis(
            conn,
            Analysis(
                analysis_id="an-no-state",
                cycle_id="cy-2",
                comparison_id="cmp-no-state",
                class_id="cls",
                condition_id="0xnostate",
                bankroll_config_id=cfg.config_id,
                model_probability=0.30,
                market_probability=0.10,
                kelly_unclamped=0.20,
                kelly_negative=False,
                kelly_clamped_by_max_cap=False,
                kelly_clamped_by_liquidity=False,
                suggested_fraction=0.025,
                suggested_dollar_size=25.0,
                ev_per_dollar=0.20,
                bankroll_after_1_loss_pct=0.975,
                bankroll_after_3_losses_pct=0.928,
                bankroll_after_5_losses_pct=0.881,
                suggested_pct_of_24h_volume=0.00125,
                days_to_resolution=180,
                long_time_to_resolution=False,
                sub_threshold=False,
                sensitivity_analysis=None,
                invalidation_criteria=(),
                computed_at=now,
            ),
        )
        write_resolution_link(
            conn,
            ComparisonResolution(
                comparison_id="cmp-no-state",
                condition_id="0xnostate",
                resolution_outcome="yes",
                resolution_ts=datetime(2026, 6, 1, tzinfo=UTC),
                model_probability_at_comparison=0.30,
                market_probability_at_comparison=0.10,
                polarity_at_comparison="aligned",
                outcome_observed=1,
                linked_at=datetime(2026, 6, 1, 0, 1, tzinfo=UTC),
            ),
        )
    report = run_expiration_pass(store, now=datetime(2026, 6, 1, 0, 5, tzinfo=UTC))
    assert report.expirations_written == 0
    assert report.analyses_examined == 1
