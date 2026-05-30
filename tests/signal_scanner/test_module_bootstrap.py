"""T-SCAN-001 — module bootstrap acceptance tests.

Confirms the signal_scanner package imports cleanly, the CLI group
exists with the expected name and is wired to the top-level CLI, and
the migrations subpackage is discoverable.
"""

from __future__ import annotations

from click.testing import CliRunner

from razor_rooster.cli import main as top_level_cli
from razor_rooster.signal_scanner.cli import scan
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


def test_signal_scanner_imports() -> None:
    import razor_rooster.signal_scanner as ss

    assert ss.__doc__ is not None


def test_scan_cli_group_help_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(scan, ["--help"])
    assert result.exit_code == 0
    assert "scan" in result.output.lower()


def test_scan_cli_wired_to_top_level() -> None:
    """`razor-rooster scan --help` produces output through the top-level CLI."""
    runner = CliRunner()
    result = runner.invoke(top_level_cli, ["scan", "--help"])
    assert result.exit_code == 0
    assert "Nose" in result.output


def test_migrations_runner_callable() -> None:
    """The migrations runner exists; T-SCAN-010 will populate it with DDL."""
    assert callable(run_pending_signal_scanner_migrations)
