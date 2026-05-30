"""T-RG-001 — module bootstrap acceptance tests."""

from __future__ import annotations

from click.testing import CliRunner

from razor_rooster.cli import main as top_level_cli
from razor_rooster.report_generator.cli import report
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)


def test_report_generator_imports() -> None:
    import razor_rooster.report_generator as rg

    assert rg.__doc__ is not None


def test_cli_group_help_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(report, ["--help"])
    assert result.exit_code == 0
    assert "report" in result.output.lower() or "crow" in result.output.lower()


def test_cli_wired_to_top_level() -> None:
    runner = CliRunner()
    result = runner.invoke(top_level_cli, ["report", "--help"])
    assert result.exit_code == 0
    assert "Crow" in result.output


def test_migrations_runner_callable() -> None:
    """The migrations runner exists; T-RG-010 will populate it with DDL."""
    assert callable(run_pending_report_generator_migrations)
