"""Smoke tests for the top-level ``razor-rooster`` CLI entrypoint (T-CB-028).

These tests exercise the click registration surface only — they do not
spin up DuckDB or invoke any subsystem-specific work. The goal is to
catch import-time regressions where a freshly registered subgroup
breaks the top-level help command for every other subsystem.
"""

from __future__ import annotations

from click.testing import CliRunner

from razor_rooster.cli import main


def test_razor_rooster_help_exits_zero() -> None:
    """``razor-rooster --help`` must exit 0 and list calibration-backtest.

    Regression guard for T-CB-028: when the calibration_backtest
    subgroup is registered, the top-level CLI must still parse and
    render its help banner. The ``calibration-backtest`` substring also
    confirms the subgroup landed in the registrations block.
    """
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0, result.output
    assert "calibration-backtest" in result.output


def test_calibration_backtest_help_exits_zero() -> None:
    """``razor-rooster calibration-backtest --help`` must exit 0.

    Confirms the subgroup itself is wired and renders its own help
    banner without raising — this catches a class of misconfiguration
    where a subgroup is imported but not registered as a click command.
    """
    runner = CliRunner()
    result = runner.invoke(main, ["calibration-backtest", "--help"])
    assert result.exit_code == 0, result.output


def test_calibration_backtest_run_help_exits_zero() -> None:
    """``razor-rooster calibration-backtest run --help`` must list every flag."""
    runner = CliRunner()
    result = runner.invoke(main, ["calibration-backtest", "run", "--help"])
    assert result.exit_code == 0, result.output
    for flag in (
        "--since",
        "--until",
        "--lag-days",
        "--class-id",
        "--sector",
        "--venue",
        "--bin-count",
        "--bin-count-per-sector",
        "--allow-recent",
        "--format",
        "--db",
    ):
        assert flag in result.output, f"missing flag {flag!r} in --help output"
