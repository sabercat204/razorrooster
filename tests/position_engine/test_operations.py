"""T-PE-011 — persistence helpers acceptance tests."""

from __future__ import annotations

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
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.position_engine.models import (
    Analysis,
    AnalysisCycle,
    AnalysisTrace,
    BankrollConfig,
)
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import (
    append_watch_state,
    complete_cycle,
    get_analysis,
    get_analysis_trace,
    latest_bankroll_config,
    latest_watch_state,
    list_by_state,
    persist_analysis,
    persist_analysis_trace,
    query_analyses,
    query_cycle,
    write_bankroll_config,
    write_cycle,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "pe_ops.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_position_engine_migrations(c)
        yield c
    store.close()


def _bankroll_config(bankroll: float = 1000.0, when: datetime | None = None) -> BankrollConfig:
    return BankrollConfig(
        config_id=str(uuid.uuid4()),
        analytical_bankroll_usd=bankroll,
        max_single_position_pct=0.05,
        kelly_fraction_default=0.5,
        min_edge_threshold=0.03,
        effective_at=when or datetime(2026, 5, 15, 12, tzinfo=UTC),
    )


def _cycle(cycle_id: str, *, config_id: str) -> AnalysisCycle:
    return AnalysisCycle(
        cycle_id=cycle_id,
        started_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
        completed_at=None,
        bankroll_config_id=config_id,
        analyses_total=0,
        analyses_with_positive_kelly=0,
        analyses_clamped_by_cap=0,
        analyses_clamped_by_liquidity=0,
    )


def _analysis(
    analysis_id: str = "an-1",
    *,
    cycle_id: str = "cy-1",
    comparison_id: str = "cmp-1",
    config_id: str = "cfg-1",
    suggested_fraction: float = 0.025,
) -> Analysis:
    return Analysis(
        analysis_id=analysis_id,
        cycle_id=cycle_id,
        comparison_id=comparison_id,
        class_id="cls",
        condition_id="0xabc",
        bankroll_config_id=config_id,
        model_probability=0.30,
        market_probability=0.10,
        kelly_unclamped=0.20,
        kelly_negative=False,
        kelly_clamped_by_max_cap=False,
        kelly_clamped_by_liquidity=False,
        suggested_fraction=suggested_fraction,
        suggested_dollar_size=suggested_fraction * 1000.0,
        ev_per_dollar=0.10,
        bankroll_after_1_loss_pct=0.975,
        bankroll_after_3_losses_pct=0.928,
        bankroll_after_5_losses_pct=0.881,
        suggested_pct_of_24h_volume=0.025,
        days_to_resolution=180,
        long_time_to_resolution=False,
        sub_threshold=False,
        sensitivity_analysis={"perturbations": [{"delta_pct": 10, "fraction": 0.022}]},
        invalidation_criteria=[{"type": "precursor_shift", "variable_id": "v1", "threshold": 5.0}],
        computed_at=datetime(2026, 5, 15, 12, 1, tzinfo=UTC),
    )


def test_bankroll_config_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    config = _bankroll_config()
    write_bankroll_config(conn, config)
    fetched = latest_bankroll_config(conn)
    assert fetched is not None
    assert fetched.analytical_bankroll_usd == pytest.approx(1000.0)


def test_latest_bankroll_config_picks_most_recent(conn: duckdb.DuckDBPyConnection) -> None:
    older = _bankroll_config(bankroll=500.0, when=datetime(2026, 1, 1, tzinfo=UTC))
    newer = _bankroll_config(bankroll=2000.0, when=datetime(2026, 5, 15, tzinfo=UTC))
    write_bankroll_config(conn, older)
    write_bankroll_config(conn, newer)
    fetched = latest_bankroll_config(conn)
    assert fetched is not None
    assert fetched.analytical_bankroll_usd == pytest.approx(2000.0)


def test_write_then_query_cycle(conn: duckdb.DuckDBPyConnection) -> None:
    cfg = _bankroll_config()
    write_bankroll_config(conn, cfg)
    write_cycle(conn, _cycle("cy-1", config_id=cfg.config_id))
    fetched = query_cycle(conn, cycle_id="cy-1")
    assert fetched is not None
    assert fetched.cycle_id == "cy-1"


def test_complete_cycle_updates_aggregates(conn: duckdb.DuckDBPyConnection) -> None:
    cfg = _bankroll_config()
    write_bankroll_config(conn, cfg)
    write_cycle(conn, _cycle("cy-1", config_id=cfg.config_id))
    complete_cycle(
        conn,
        cycle_id="cy-1",
        completed_at=datetime(2026, 5, 15, 12, 5, tzinfo=UTC),
        analyses_total=8,
        analyses_with_positive_kelly=3,
        analyses_clamped_by_cap=1,
        analyses_clamped_by_liquidity=2,
        duration_seconds=4.2,
    )
    fetched = query_cycle(conn, cycle_id="cy-1")
    assert fetched is not None
    assert fetched.analyses_total == 8
    assert fetched.analyses_with_positive_kelly == 3
    assert fetched.duration_seconds == pytest.approx(4.2)


def test_persist_analysis_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    cfg = _bankroll_config()
    write_bankroll_config(conn, cfg)
    write_cycle(conn, _cycle("cy-1", config_id=cfg.config_id))
    persist_analysis(conn, _analysis(config_id=cfg.config_id))
    fetched = get_analysis(conn, analysis_id="an-1")
    assert fetched is not None
    assert fetched.suggested_fraction == pytest.approx(0.025)
    assert len(fetched.invalidation_criteria) == 1
    assert fetched.invalidation_criteria[0]["type"] == "precursor_shift"


def test_persist_analysis_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    cfg = _bankroll_config()
    write_bankroll_config(conn, cfg)
    write_cycle(conn, _cycle("cy-1", config_id=cfg.config_id))
    persist_analysis(conn, _analysis(config_id=cfg.config_id, suggested_fraction=0.025))
    persist_analysis(conn, _analysis(config_id=cfg.config_id, suggested_fraction=0.040))
    fetched = get_analysis(conn, analysis_id="an-1")
    assert fetched is not None
    assert fetched.suggested_fraction == pytest.approx(0.040)
    rows = conn.execute("SELECT COUNT(*) FROM analyses WHERE analysis_id = 'an-1'").fetchone()
    assert rows is not None and rows[0] == 1


def test_persist_analysis_trace_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    cfg = _bankroll_config()
    write_bankroll_config(conn, cfg)
    write_cycle(conn, _cycle("cy-1", config_id=cfg.config_id))
    persist_analysis(conn, _analysis(config_id=cfg.config_id))
    trace = AnalysisTrace(
        analysis_id="an-1",
        rendered_text="ANALYSIS: ... (rendered)",
        structured_dict={"section": "test", "value": 1},
    )
    persist_analysis_trace(conn, trace)
    fetched = get_analysis_trace(conn, analysis_id="an-1")
    assert fetched is not None
    assert "rendered" in fetched.rendered_text
    assert fetched.structured_dict["section"] == "test"


def test_query_analyses_filters(conn: duckdb.DuckDBPyConnection) -> None:
    cfg = _bankroll_config()
    write_bankroll_config(conn, cfg)
    write_cycle(conn, _cycle("cy-1", config_id=cfg.config_id))
    persist_analysis(conn, _analysis("an-1", config_id=cfg.config_id, comparison_id="cmp-A"))
    persist_analysis(conn, _analysis("an-2", config_id=cfg.config_id, comparison_id="cmp-B"))
    by_cycle = query_analyses(conn, cycle_id="cy-1")
    by_cmp = query_analyses(conn, comparison_id="cmp-A")
    assert len(by_cycle) == 2
    assert len(by_cmp) == 1
    assert by_cmp[0].analysis_id == "an-1"


def test_watch_state_append_and_latest(conn: duckdb.DuckDBPyConnection) -> None:
    cfg = _bankroll_config()
    write_bankroll_config(conn, cfg)
    write_cycle(conn, _cycle("cy-1", config_id=cfg.config_id))
    persist_analysis(conn, _analysis(config_id=cfg.config_id))
    append_watch_state(
        conn,
        analysis_id="an-1",
        state="watching",
        notes="initial",
        when=datetime(2026, 5, 15, 13, tzinfo=UTC),
    )
    append_watch_state(
        conn,
        analysis_id="an-1",
        state="acted_on",
        notes="acted",
        when=datetime(2026, 5, 16, 10, tzinfo=UTC),
    )
    latest = latest_watch_state(conn, analysis_id="an-1")
    assert latest is not None
    assert latest.state == "acted_on"
    assert latest.notes == "acted"


def test_list_by_state_returns_latest(conn: duckdb.DuckDBPyConnection) -> None:
    cfg = _bankroll_config()
    write_bankroll_config(conn, cfg)
    write_cycle(conn, _cycle("cy-1", config_id=cfg.config_id))
    persist_analysis(conn, _analysis("an-1", config_id=cfg.config_id))
    persist_analysis(conn, _analysis("an-2", config_id=cfg.config_id))
    append_watch_state(conn, analysis_id="an-1", state="watching")
    append_watch_state(conn, analysis_id="an-2", state="watching")
    # Move an-1 to acted_on.
    append_watch_state(conn, analysis_id="an-1", state="acted_on")
    watching = list_by_state(conn, state="watching")
    acted = list_by_state(conn, state="acted_on")
    assert {w.analysis_id for w in watching} == {"an-2"}
    assert {w.analysis_id for w in acted} == {"an-1"}


def test_watch_state_system_set_by(conn: duckdb.DuckDBPyConnection) -> None:
    """Auto-expiration writes set_by='system' rows."""
    cfg = _bankroll_config()
    write_bankroll_config(conn, cfg)
    write_cycle(conn, _cycle("cy-1", config_id=cfg.config_id))
    persist_analysis(conn, _analysis(config_id=cfg.config_id))
    append_watch_state(conn, analysis_id="an-1", state="watching")
    append_watch_state(
        conn,
        analysis_id="an-1",
        state="expired",
        set_by="system",
        notes="market resolved",
    )
    latest = latest_watch_state(conn, analysis_id="an-1")
    assert latest is not None
    assert latest.set_by == "system"
    assert latest.state == "expired"
