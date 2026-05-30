"""T-PE-050 / T-PE-051 — analyzer + cycle runner tests."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.models import (
    Comparison,
    ComparisonCycle,
    ComparisonTrace,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    persist_comparison,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    persist_trace as persist_comparison_trace,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    write_cycle as write_comparison_cycle,
)
from razor_rooster.pattern_library import registry
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    Sector,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.position_engine.engines.analyzer import (
    NoBankrollConfigError,
    analyze_comparison,
    run_cycle,
)
from razor_rooster.position_engine.frame.linter import LinterCatalog
from razor_rooster.position_engine.models import BankrollConfig
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import (
    get_analysis_trace,
    query_analyses,
    query_cycle,
    write_bankroll_config,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    registry._clear_for_tests()
    registry._set_discovered_for_tests(True)
    yield
    registry._clear_for_tests()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "pe_analyzer.duckdb"
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


def _occurrences(_conn: object) -> pd.DataFrame:
    return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})


def _make_class(class_id: str = "test_cls") -> EventClass:
    return EventClass(
        class_id=class_id,
        title=f"Test class {class_id}",
        description="Synthetic class for analyzer tests",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_occurrences,
    )


def _seed_bankroll_config(
    store: DuckDBStore,
    *,
    bankroll: float = 1000.0,
) -> BankrollConfig:
    cfg = BankrollConfig(
        config_id=str(uuid.uuid4()),
        analytical_bankroll_usd=bankroll,
        max_single_position_pct=0.05,
        kelly_fraction_default=0.5,
        min_edge_threshold=0.03,
        effective_at=datetime(2026, 5, 15, 10, tzinfo=UTC),
    )
    with store.connection() as conn:
        write_bankroll_config(conn, cfg)
    return cfg


def _seed_polymarket_market(
    store: DuckDBStore,
    *,
    condition_id: str = "0xabc",
    end_date: datetime | None = None,
) -> None:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    end_date = end_date or datetime(2026, 11, 15, tzinfo=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO polymarket_markets ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "condition_id, slug, question, description, category, subcategory, "
            "tags, event_id, market_type, outcome_tokens, end_date, active, "
            "closed, resolved, volume_lifetime, created_at_polymarket, "
            "last_updated_polymarket, removed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, "
            "?, ?, ?, TRUE, FALSE, FALSE, NULL, NULL, NULL, NULL)",
            [
                "polymarket",
                f"market-{condition_id}",
                now,
                now,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                condition_id,
                "slug",
                "Will event happen in 2026?",
                "binary",
                json.dumps([{"id": "tok-yes", "outcome": "Yes"}]),
                end_date,
            ],
        )


def _seed_comparison(
    store: DuckDBStore,
    *,
    comparison_id: str = "cmp-1",
    class_id: str = "test_cls",
    condition_id: str = "0xabc",
    model_p: float = 0.30,
    market_p: float | None = 0.10,
    surfaced: bool = True,
    delta: float | None = 0.20,
    log_odds_delta: float | None = 1.4,
    market_volume_24h: float | None = 25000.0,
    embedded_scanner_warnings: tuple[str, ...] = (),
) -> None:
    now = datetime(2026, 5, 15, 12, tzinfo=UTC)
    with store.connection() as conn:
        write_comparison_cycle(
            conn,
            ComparisonCycle(
                cycle_id="cycle-cmp-1",
                started_at=now,
                completed_at=now,
                comparisons_total=1,
                surfaced_count=1 if surfaced else 0,
                suppressed_breakdown={},
                library_version_at_cycle=1,
                scan_id_consumed="scan-1",
            ),
        )
        persist_comparison(
            conn,
            Comparison(
                comparison_id=comparison_id,
                cycle_id="cycle-cmp-1",
                mapping_id="m-1",
                class_id=class_id,
                condition_id=condition_id,
                outcome_token_id="tok-yes",
                polarity="aligned",
                scan_id="scan-1",
                model_probability=model_p,
                model_ci_lower=model_p * 0.7,
                model_ci_upper=model_p * 1.3,
                market_probability=market_p,
                market_best_bid=(market_p - 0.005) if market_p is not None else None,
                market_best_ask=(market_p + 0.005) if market_p is not None else None,
                market_last_trade_price=market_p,
                market_volume_24h=market_volume_24h,
                market_spread_bps=200,
                market_snapshot_ts=now,
                delta=delta,
                log_odds_delta=log_odds_delta,
                ci_overlap=False,
                expected_value=delta,
                confidence_weighted_score=1.18 if surfaced else None,
                surfaced=surfaced,
                computed_at=now,
            ),
        )
        embedded_trace = {
            "class_id": class_id,
            "warnings": list(embedded_scanner_warnings),
            "precursors": [
                {
                    "variable_id": "v1",
                    "title": "Synthetic precursor",
                    "current_value": 8.0,
                    "threshold": 5.0,
                    "direction": "high_signals_event",
                    "fired": True,
                    "hit_rate": 0.7,
                    "false_positive_rate": 0.2,
                    "likelihood_ratio_applied": 3.5,
                }
            ],
        }
        persist_comparison_trace(
            conn,
            ComparisonTrace(
                comparison_id=comparison_id,
                payload={"embedded_scanner_trace": embedded_trace},
            ),
        )


def test_analyze_comparison_strong_signal(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    cfg = _seed_bankroll_config(store)
    _seed_polymarket_market(store)
    _seed_comparison(store, model_p=0.30, market_p=0.10)
    result = analyze_comparison(
        store=store,
        cycle_id="cy-1",
        comparison_id="cmp-1",
        bankroll_config=cfg,
        pe_config=__import__(
            "razor_rooster.position_engine.config.loader", fromlist=["load_config"]
        ).load_config(),
        now=datetime(2026, 5, 15, 13, tzinfo=UTC),
    )
    assert result is not None
    analysis, trace = result
    assert analysis.error is None
    assert analysis.kelly_unclamped > 0
    assert analysis.suggested_fraction > 0
    assert analysis.bankroll_after_5_losses_pct < 1.0
    assert "DISCLAIMER" in trace.rendered_text
    assert "if the operator chose to act" in trace.rendered_text


def test_analyze_comparison_sub_threshold_skips_math(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    cfg = _seed_bankroll_config(store)
    _seed_polymarket_market(store)
    # Tiny edge: |delta| = 0.005 < min_edge_threshold (0.03).
    _seed_comparison(store, model_p=0.10, market_p=0.105, delta=-0.005)
    result = analyze_comparison(
        store=store,
        cycle_id="cy-1",
        comparison_id="cmp-1",
        bankroll_config=cfg,
        pe_config=__import__(
            "razor_rooster.position_engine.config.loader", fromlist=["load_config"]
        ).load_config(),
        now=datetime(2026, 5, 15, 13, tzinfo=UTC),
    )
    assert result is not None
    analysis, trace = result
    assert analysis.sub_threshold is True
    assert analysis.suggested_fraction == 0.0
    assert "min_edge_threshold" in trace.rendered_text


def test_analyze_comparison_kelly_negative(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    cfg = _seed_bankroll_config(store)
    _seed_polymarket_market(store)
    # Model below market: Kelly negative.
    _seed_comparison(store, model_p=0.05, market_p=0.30, delta=-0.25)
    result = analyze_comparison(
        store=store,
        cycle_id="cy-1",
        comparison_id="cmp-1",
        bankroll_config=cfg,
        pe_config=__import__(
            "razor_rooster.position_engine.config.loader", fromlist=["load_config"]
        ).load_config(),
        now=datetime(2026, 5, 15, 13, tzinfo=UTC),
    )
    assert result is not None
    analysis, _ = result
    assert analysis.kelly_negative is True
    assert analysis.suggested_fraction == 0.0


def test_analyze_comparison_clamps_by_liquidity(store: DuckDBStore) -> None:
    """A market with low 24h volume forces the suggested fraction down."""
    cls = _make_class()
    registry.register(cls)
    cfg = _seed_bankroll_config(store, bankroll=10000.0)
    _seed_polymarket_market(store)
    # Big edge but tiny market.
    _seed_comparison(
        store,
        model_p=0.50,
        market_p=0.10,
        delta=0.40,
        log_odds_delta=2.2,
        market_volume_24h=500.0,
    )
    result = analyze_comparison(
        store=store,
        cycle_id="cy-1",
        comparison_id="cmp-1",
        bankroll_config=cfg,
        pe_config=__import__(
            "razor_rooster.position_engine.config.loader", fromlist=["load_config"]
        ).load_config(),
        now=datetime(2026, 5, 15, 13, tzinfo=UTC),
    )
    assert result is not None
    analysis, _ = result
    assert analysis.kelly_clamped_by_liquidity is True
    assert analysis.low_liquidity is True


def test_analyze_comparison_long_resolution_flag(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    cfg = _seed_bankroll_config(store)
    # End date 2 years out.
    _seed_polymarket_market(store, end_date=datetime(2028, 5, 15, tzinfo=UTC))
    _seed_comparison(store, model_p=0.30, market_p=0.10)
    result = analyze_comparison(
        store=store,
        cycle_id="cy-1",
        comparison_id="cmp-1",
        bankroll_config=cfg,
        pe_config=__import__(
            "razor_rooster.position_engine.config.loader", fromlist=["load_config"]
        ).load_config(),
        now=datetime(2026, 5, 15, 13, tzinfo=UTC),
    )
    assert result is not None
    analysis, trace = result
    assert analysis.long_time_to_resolution is True
    assert "long-resolution" in trace.rendered_text


def test_analyze_comparison_unknown_returns_none(store: DuckDBStore) -> None:
    cfg = _seed_bankroll_config(store)
    result = analyze_comparison(
        store=store,
        cycle_id="cy-1",
        comparison_id="missing",
        bankroll_config=cfg,
        pe_config=__import__(
            "razor_rooster.position_engine.config.loader", fromlist=["load_config"]
        ).load_config(),
        now=datetime(2026, 5, 15, 13, tzinfo=UTC),
    )
    assert result is None


def test_analyze_comparison_failure_isolation(store: DuckDBStore) -> None:
    """Missing market metadata is handled in compute_liquidity (clamp to 0)."""
    cls = _make_class()
    registry.register(cls)
    cfg = _seed_bankroll_config(store)
    # Don't seed polymarket_markets.
    _seed_comparison(store, model_p=0.30, market_p=0.10)
    result = analyze_comparison(
        store=store,
        cycle_id="cy-1",
        comparison_id="cmp-1",
        bankroll_config=cfg,
        pe_config=__import__(
            "razor_rooster.position_engine.config.loader", fromlist=["load_config"]
        ).load_config(),
        now=datetime(2026, 5, 15, 13, tzinfo=UTC),
    )
    # Even with no market metadata for end_date, the analysis still
    # produces; days_to_resolution is just None.
    assert result is not None
    analysis, _ = result
    assert analysis.days_to_resolution is None


def test_run_cycle_no_bankroll_config_raises(store: DuckDBStore) -> None:
    with pytest.raises(NoBankrollConfigError):
        run_cycle(store, now=datetime(2026, 5, 15, 13, tzinfo=UTC))


def test_run_cycle_persists_analyses(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    _seed_bankroll_config(store)
    _seed_polymarket_market(store)
    _seed_comparison(store, model_p=0.30, market_p=0.10)
    report = run_cycle(store, now=datetime(2026, 5, 15, 13, tzinfo=UTC))
    assert report.completed_at is not None
    assert report.analyses_total == 1
    assert report.analyses_with_positive_kelly == 1
    with store.connection() as conn:
        analysis = next(iter(query_analyses(conn, cycle_id=report.cycle_id)))
        cycle = query_cycle(conn, cycle_id=report.cycle_id)
        analysis_trace = get_analysis_trace(conn, analysis_id=analysis.analysis_id)
    assert cycle is not None
    assert cycle.analyses_total == 1
    assert analysis.suggested_fraction > 0
    assert analysis_trace is not None
    assert "DISCLAIMER" in analysis_trace.rendered_text


def test_run_cycle_skips_suppressed_by_default(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    _seed_bankroll_config(store)
    _seed_polymarket_market(store)
    _seed_comparison(store, model_p=0.30, market_p=0.10, surfaced=False)
    report = run_cycle(store, now=datetime(2026, 5, 15, 13, tzinfo=UTC))
    assert report.analyses_total == 0


def test_run_cycle_includes_suppressed_when_flag_set(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    _seed_bankroll_config(store)
    _seed_polymarket_market(store)
    _seed_comparison(store, model_p=0.30, market_p=0.10, surfaced=False)
    report = run_cycle(
        store,
        include_suppressed=True,
        now=datetime(2026, 5, 15, 13, tzinfo=UTC),
    )
    assert report.analyses_total == 1


def test_run_cycle_records_clamped_count(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    _seed_bankroll_config(store)
    _seed_polymarket_market(store)
    _seed_comparison(store, model_p=0.80, market_p=0.10, delta=0.70, log_odds_delta=2.2)
    report = run_cycle(store, now=datetime(2026, 5, 15, 13, tzinfo=UTC))
    # Big edge → Kelly above max cap.
    assert report.analyses_clamped_by_cap >= 1


def test_linter_catches_imperative_in_class_title(store: DuckDBStore) -> None:
    """If a class title accidentally contained imperative phrasing, the
    linter would refuse to ship the rendered output. Demonstrates the
    safety net is wired (T-PE-041 acceptance).
    """
    cls = EventClass(
        class_id="test_cls",
        # Class titles never contain forbidden phrases in production,
        # but if one slipped in, the linter must fire.
        title="Take this position Buy this market",
        description="adversarial test class with imperative title",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_occurrences,
    )
    registry.register(cls)
    _seed_bankroll_config(store)
    _seed_polymarket_market(store)
    _seed_comparison(store, model_p=0.30, market_p=0.10)
    # Use the project linter catalog.
    catalog = LinterCatalog.from_yaml()
    report = run_cycle(
        store,
        linter_catalog=catalog,
        now=datetime(2026, 5, 15, 13, tzinfo=UTC),
    )
    # The cycle catches the linter rejection per-comparison and logs it
    # as an error rather than crashing.
    assert any("ImperativeLanguageDetected" in e for e in report.errors)
