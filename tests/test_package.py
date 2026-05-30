"""Smoke tests for the package skeleton (T-001/T-002 verification)."""

from __future__ import annotations

from razor_rooster import __version__


def test_version_is_present() -> None:
    assert isinstance(__version__, str)
    assert __version__.count(".") == 2


def test_cli_module_imports() -> None:
    """Importing the top-level CLI must not raise."""
    from razor_rooster import cli

    assert hasattr(cli, "main")


def test_data_ingest_cli_imports() -> None:
    """The data_ingest CLI group must be importable and registered."""
    from razor_rooster.data_ingest.cli import ingest

    assert ingest.name == "ingest"
