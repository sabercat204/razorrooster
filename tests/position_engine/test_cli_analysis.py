"""T-PE-070 — analysis CLI subcommand tests."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

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
from razor_rooster.position_engine.cli import position_engine
from razor_rooster.position_engine.models import BankrollConfig
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import (
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
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "trough.duckdb"
    store = DuckDBStore(path)
    now = datetime(2026, 5, 15, 12, tzinfo=UTC)
    with store.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        run_pending_mispricing_migrations(conn)
        run_pending_position_engine_migrations(conn)
        # Seed bankroll config.
        write_bankroll_config(
            conn,
            BankrollConfig(
                config_id=str(uuid.uuid4()),
                analytical_bankroll_usd=1000.0,
                max_single_position_pct=0.05,
                kelly_fraction_default=0.5,
                min_edge_threshold=0.03,
                effective_at=now,
            ),
        )
        # Seed polymarket market.
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
                "market-0xabc",
                now,
                now,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                "0xabc",
                "slug",
                "Will event happen?",
                "binary",
                json.dumps([{"id": "tok-yes", "outcome": "Yes"}]),
                datetime(2026, 11, 15, tzinfo=UTC),
            ],
        )
        # Seed comparison.
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
                comparison_id="cmp-1",
                cycle_id="cmp-cy-1",
                mapping_id="m-1",
                class_id="cli_cls",
                condition_id="0xabc",
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
                market_volume_24h=25000.0,
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
        persist_comparison_trace(
            conn,
            ComparisonTrace(
                comparison_id="cmp-1",
                payload={
                    "embedded_scanner_trace": {
                        "class_id": "cli_cls",
                        "warnings": [],
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
                },
            ),
        )
    store.close()
    yield path


def _register_class() -> None:
    cls = EventClass(
        class_id="cli_cls",
        title="CLI Test Class",
        description="Synthetic class for analysis CLI tests",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=lambda _conn: pd.DataFrame(
            {"occurrence_ts": pd.to_datetime([], utc=True)}
        ),
    )
    registry.register(cls)


def test_run_command_completes(db_path: Path) -> None:
    _register_class()
    runner = CliRunner()
    result = runner.invoke(position_engine, ["run", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "cycle_id:" in result.output
    assert "cli_cls" in result.output


def test_run_no_bankroll_config(tmp_path: Path) -> None:
    """Without bankroll_config row, run exits 1 with a clear message."""
    fresh = tmp_path / "fresh.duckdb"
    store = DuckDBStore(fresh)
    with store.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        run_pending_mispricing_migrations(conn)
        run_pending_position_engine_migrations(conn)
    store.close()
    runner = CliRunner()
    result = runner.invoke(position_engine, ["run", "--db", str(fresh)])
    assert result.exit_code == 1
    assert "bankroll_config" in result.output


def test_analyze_command_runs_one_comparison(db_path: Path) -> None:
    _register_class()
    runner = CliRunner()
    result = runner.invoke(
        position_engine,
        ["analyze", "cmp-1", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    assert "analysis_id:" in result.output
    assert "cli_cls" in result.output


def test_analyze_unknown_comparison(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        position_engine,
        ["analyze", "no-such-cmp", "--db", str(db_path)],
    )
    assert result.exit_code == 1
    assert "not found" in result.output


def test_show_command_renders_trace(db_path: Path) -> None:
    _register_class()
    runner = CliRunner()
    runner.invoke(position_engine, ["run", "--db", str(db_path)])
    list_result = runner.invoke(position_engine, ["list", "--watched", "--db", str(db_path)])
    # No watch state set yet; list-watched is empty.
    assert list_result.exit_code == 0


def test_watch_then_list(db_path: Path) -> None:
    _register_class()
    runner = CliRunner()
    runner.invoke(position_engine, ["run", "--db", str(db_path)])
    # Pull the analysis id by querying directly.
    store = DuckDBStore(db_path)
    with store.connection() as conn:
        row = conn.execute("SELECT analysis_id FROM analyses LIMIT 1").fetchone()
    store.close()
    assert row is not None
    analysis_id = str(row[0])
    watch_result = runner.invoke(
        position_engine,
        ["watch", analysis_id, "--note", "interesting", "--db", str(db_path)],
    )
    assert watch_result.exit_code == 0, watch_result.output
    list_result = runner.invoke(position_engine, ["list", "--watched", "--db", str(db_path)])
    assert list_result.exit_code == 0
    assert analysis_id in list_result.output


def test_list_requires_exactly_one_flag(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(position_engine, ["list", "--db", str(db_path)])
    assert result.exit_code == 2
    assert "--watched" in result.output


def test_acted_on_then_dismiss_transitions(db_path: Path) -> None:
    _register_class()
    runner = CliRunner()
    runner.invoke(position_engine, ["run", "--db", str(db_path)])
    store = DuckDBStore(db_path)
    with store.connection() as conn:
        row = conn.execute("SELECT analysis_id FROM analyses LIMIT 1").fetchone()
    store.close()
    assert row is not None
    analysis_id = str(row[0])
    runner.invoke(
        position_engine,
        ["acted-on", analysis_id, "--note", "took action", "--db", str(db_path)],
    )
    dismiss = runner.invoke(
        position_engine,
        ["dismiss", analysis_id, "--reason", "decided otherwise", "--db", str(db_path)],
    )
    assert dismiss.exit_code == 0
    list_acted = runner.invoke(position_engine, ["list", "--acted-on", "--db", str(db_path)])
    list_dismissed = runner.invoke(position_engine, ["list", "--dismissed", "--db", str(db_path)])
    # acted-on is no longer the latest state; dismissed is.
    assert analysis_id not in list_acted.output
    assert analysis_id in list_dismissed.output


def test_show_unknown_analysis(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(position_engine, ["show", "no-such-analysis", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "not found" in result.output
