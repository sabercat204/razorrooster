"""T-PE-020 — bankroll config CLI + loader acceptance tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

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
from razor_rooster.position_engine.cli import position_engine
from razor_rooster.position_engine.config.loader import (
    BankrollValidationBounds,
    BankrollValidationError,
    load_config,
    validate_bankroll_inputs,
)
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import (
    latest_bankroll_config,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "trough.duckdb"
    store = DuckDBStore(path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_polymarket_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_position_engine_migrations(c)
    store.close()
    yield path


def test_load_config_uses_defaults_when_yaml_missing(tmp_path: Path) -> None:
    cfg = load_config(path=tmp_path / "missing.yaml")
    assert cfg.bankroll_defaults.kelly_fraction_default == pytest.approx(0.5)
    assert cfg.long_resolution_days_threshold == 365
    assert cfg.bankroll_validation.kelly_fraction_default_max == pytest.approx(0.5)


def test_load_config_reads_real_yaml() -> None:
    """The shipped config/position_engine.yaml validates."""
    cfg = load_config()  # uses default path
    assert cfg.bankroll_defaults.analytical_bankroll_usd > 0
    assert cfg.liquidity_feasibility.threshold_for("public_health") > 0
    # Validation bounds match design.
    assert cfg.bankroll_validation.kelly_fraction_default_max == pytest.approx(0.5)
    assert cfg.bankroll_validation.max_single_position_pct_max == pytest.approx(0.25)


def test_validate_bankroll_inputs_accepts_defaults() -> None:
    validate_bankroll_inputs(
        analytical_bankroll_usd=1000.0,
        max_single_position_pct=0.05,
        kelly_fraction_default=0.5,
        min_edge_threshold=0.03,
    )


def test_validate_bankroll_inputs_rejects_negative_bankroll() -> None:
    with pytest.raises(BankrollValidationError):
        validate_bankroll_inputs(
            analytical_bankroll_usd=-100.0,
            max_single_position_pct=0.05,
            kelly_fraction_default=0.5,
            min_edge_threshold=0.03,
        )


def test_validate_bankroll_inputs_rejects_excessive_kelly() -> None:
    """OQ-PE-001: kelly_fraction_default capped at 0.5."""
    with pytest.raises(BankrollValidationError) as exc_info:
        validate_bankroll_inputs(
            analytical_bankroll_usd=1000.0,
            max_single_position_pct=0.05,
            kelly_fraction_default=0.9,
            min_edge_threshold=0.03,
        )
    assert "0.5" in str(exc_info.value)


def test_validate_bankroll_inputs_rejects_excessive_max_pct() -> None:
    with pytest.raises(BankrollValidationError):
        validate_bankroll_inputs(
            analytical_bankroll_usd=1000.0,
            max_single_position_pct=0.50,
            kelly_fraction_default=0.5,
            min_edge_threshold=0.03,
        )


def test_validate_bankroll_inputs_rejects_negative_max_pct() -> None:
    with pytest.raises(BankrollValidationError):
        validate_bankroll_inputs(
            analytical_bankroll_usd=1000.0,
            max_single_position_pct=-0.01,
            kelly_fraction_default=0.5,
            min_edge_threshold=0.03,
        )


def test_validate_bankroll_inputs_custom_bounds() -> None:
    bounds = BankrollValidationBounds(kelly_fraction_default_max=0.75)
    # With looser bounds, 0.6 is now allowed.
    validate_bankroll_inputs(
        analytical_bankroll_usd=1000.0,
        max_single_position_pct=0.05,
        kelly_fraction_default=0.6,
        min_edge_threshold=0.03,
        bounds=bounds,
    )


def test_config_command_writes_bankroll(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        position_engine,
        [
            "config",
            "--bankroll",
            "5000",
            "--no-prompt",
            "--acknowledge-analytical",
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "analytical_bankroll_usd:" in result.output
    # Disclaimer must show.
    assert "analytical bankroll" in result.output.lower()
    store = DuckDBStore(db_path)
    with store.connection() as conn:
        cfg = latest_bankroll_config(conn)
    store.close()
    assert cfg is not None
    assert cfg.analytical_bankroll_usd == pytest.approx(5000.0)


def test_config_command_no_prompt_requires_acknowledge(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        position_engine,
        ["config", "--bankroll", "5000", "--no-prompt", "--db", str(db_path)],
    )
    assert result.exit_code == 2
    assert "--acknowledge-analytical" in result.output


def test_config_command_rejects_invalid_kelly(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        position_engine,
        [
            "config",
            "--bankroll",
            "1000",
            "--kelly-fraction",
            "0.9",
            "--no-prompt",
            "--acknowledge-analytical",
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code == 2
    assert "kelly_fraction_default" in result.output


def test_config_command_replaces_previous(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(
        position_engine,
        [
            "config",
            "--bankroll",
            "1000",
            "--no-prompt",
            "--acknowledge-analytical",
            "--db",
            str(db_path),
        ],
    )
    second = runner.invoke(
        position_engine,
        [
            "config",
            "--bankroll",
            "2000",
            "--no-prompt",
            "--acknowledge-analytical",
            "--db",
            str(db_path),
        ],
    )
    assert second.exit_code == 0
    assert "replaces config_id:" in second.output
    store = DuckDBStore(db_path)
    with store.connection() as conn:
        cfg = latest_bankroll_config(conn)
        rows = conn.execute("SELECT COUNT(*) FROM bankroll_config").fetchone()
    store.close()
    assert cfg is not None
    assert cfg.analytical_bankroll_usd == pytest.approx(2000.0)
    assert rows is not None and rows[0] == 2
