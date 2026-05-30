"""T-PL-001 — pattern_library module skeleton smoke tests."""

from __future__ import annotations

from click.testing import CliRunner

from razor_rooster.cli import main as razor_rooster_main
from razor_rooster.pattern_library import LIBRARY_VERSION
from razor_rooster.pattern_library.cli import pattern_library
from razor_rooster.pattern_library.version import current_version


def test_pattern_library_version_is_positive_int() -> None:
    assert isinstance(LIBRARY_VERSION, int)
    assert LIBRARY_VERSION >= 1


def test_current_version_returns_library_version() -> None:
    assert current_version() == LIBRARY_VERSION


def test_pattern_library_cli_help_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["--help"])
    assert result.exit_code == 0
    assert "pattern-library" in result.output.lower() or "bone pile" in result.output.lower()


def test_pattern_library_version_subcommand_prints_int() -> None:
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == str(LIBRARY_VERSION)


def test_top_level_cli_registers_pattern_library_group() -> None:
    runner = CliRunner()
    result = runner.invoke(razor_rooster_main, ["--help"])
    assert result.exit_code == 0
    assert "pattern-library" in result.output
