"""T-MD-001 — module bootstrap acceptance tests.

Confirms the mispricing_detector package imports cleanly, the CLI group
exists with the expected name and is wired to the top-level CLI, and
the migrations subpackage is discoverable.
"""

from __future__ import annotations

from click.testing import CliRunner

from razor_rooster.cli import main as top_level_cli
from razor_rooster.mispricing_detector.cli import mispricing
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)


def test_mispricing_detector_imports() -> None:
    import razor_rooster.mispricing_detector as md

    assert md.__doc__ is not None


def test_mispricing_cli_group_help_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(mispricing, ["--help"])
    assert result.exit_code == 0
    assert "mispricing" in result.output.lower()


def test_mispricing_cli_wired_to_top_level() -> None:
    runner = CliRunner()
    result = runner.invoke(top_level_cli, ["mispricing", "--help"])
    assert result.exit_code == 0
    assert "Liver" in result.output


def test_migrations_runner_callable() -> None:
    """The migrations runner exists; T-MD-010 will populate it with DDL."""
    assert callable(run_pending_mispricing_migrations)
