"""T-KSI-001 — module bootstrap acceptance tests."""

from __future__ import annotations

from click.testing import CliRunner

from razor_rooster.cli import main as top_level_cli
from razor_rooster.kalshi_connector.cli import kalshi
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)


def test_kalshi_connector_imports() -> None:
    import razor_rooster.kalshi_connector as ksi

    assert ksi.__doc__ is not None


def test_cli_group_help_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(kalshi, ["--help"])
    assert result.exit_code == 0
    assert "kalshi" in result.output.lower() or "stamp" in result.output.lower()


def test_cli_wired_to_top_level() -> None:
    runner = CliRunner()
    result = runner.invoke(top_level_cli, ["kalshi", "--help"])
    assert result.exit_code == 0
    assert "Stamp" in result.output


def test_version_subcommand_prints_namespace() -> None:
    runner = CliRunner()
    result = runner.invoke(kalshi, ["version"])
    assert result.exit_code == 0
    assert "8001+" in result.output


def test_migrations_runner_callable() -> None:
    """The migrations runner exists; T-KSI-011 will populate it with DDL."""
    assert callable(run_pending_kalshi_migrations)
