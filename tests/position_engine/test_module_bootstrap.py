"""T-PE-001 — module bootstrap acceptance tests."""

from __future__ import annotations

from click.testing import CliRunner

from razor_rooster.cli import main as top_level_cli
from razor_rooster.position_engine.cli import position_engine
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)


def test_position_engine_imports() -> None:
    import razor_rooster.position_engine as pe

    assert pe.__doc__ is not None


def test_cli_group_help_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(position_engine, ["--help"])
    assert result.exit_code == 0
    assert "position-engine" in result.output.lower() or "spur" in result.output.lower()


def test_cli_wired_to_top_level() -> None:
    runner = CliRunner()
    result = runner.invoke(top_level_cli, ["position-engine", "--help"])
    assert result.exit_code == 0
    assert "Spur" in result.output


def test_migrations_runner_callable() -> None:
    """The migrations runner exists; T-PE-010 will populate it with DDL."""
    assert callable(run_pending_position_engine_migrations)
