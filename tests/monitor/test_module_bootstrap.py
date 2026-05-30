"""T-MON-001 — module bootstrap acceptance tests."""

from __future__ import annotations

from click.testing import CliRunner

from razor_rooster.cli import main as top_level_cli
from razor_rooster.monitor.cli import monitor
from razor_rooster.monitor.persistence.migrations import (
    run_pending_monitor_migrations,
)


def test_monitor_imports() -> None:
    import razor_rooster.monitor as mon

    assert mon.__doc__ is not None


def test_cli_group_help_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(monitor, ["--help"])
    assert result.exit_code == 0
    assert "comb" in result.output.lower() or "monitor" in result.output.lower()


def test_cli_wired_to_top_level() -> None:
    runner = CliRunner()
    result = runner.invoke(top_level_cli, ["monitor", "--help"])
    assert result.exit_code == 0
    assert "Comb" in result.output


def test_migrations_runner_callable() -> None:
    """The migrations runner exists; T-MON-010 will populate it with DDL."""
    assert callable(run_pending_monitor_migrations)
