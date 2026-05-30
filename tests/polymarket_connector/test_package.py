"""T-PMC-001 — polymarket_connector module skeleton smoke tests."""

from __future__ import annotations

from click.testing import CliRunner

from razor_rooster.cli import main as razor_rooster_main
from razor_rooster.polymarket_connector import __doc__ as pkg_doc
from razor_rooster.polymarket_connector.cli import polymarket


def test_polymarket_package_imports() -> None:
    assert pkg_doc is not None
    assert "Polymarket" in pkg_doc or "polymarket" in pkg_doc


def test_polymarket_cli_group_help() -> None:
    runner = CliRunner()
    result = runner.invoke(polymarket, ["--help"])
    assert result.exit_code == 0
    assert "polymarket" in result.output.lower()


def test_polymarket_status_runs_against_missing_db() -> None:
    """The real `status` command now reports 'DuckDB store not found' when there's no DB."""
    runner = CliRunner()
    result = runner.invoke(polymarket, ["status", "--db", "/tmp/definitely-not-a-real-path.duckdb"])
    assert result.exit_code == 1
    output = result.output + (result.stderr if result.stderr_bytes else "")
    assert "DuckDB store not found" in output


def test_top_level_cli_registers_polymarket_group() -> None:
    runner = CliRunner()
    result = runner.invoke(razor_rooster_main, ["--help"])
    assert result.exit_code == 0
    assert "polymarket" in result.output
