"""Tests for the ``razor-rooster gui`` click subcommand."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from razor_rooster.cli import main as razor_main
from razor_rooster.gui.cli import _resolve_db_path, _resolve_port


def test_gui_help_lists_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(razor_main, ["--help"])
    assert result.exit_code == 0
    assert "gui" in result.output


def test_gui_subcommand_help() -> None:
    runner = CliRunner()
    result = runner.invoke(razor_main, ["gui", "--help"])
    assert result.exit_code == 0
    assert "Launch the read-only operator GUI" in result.output
    assert "--port" in result.output
    assert "--host" in result.output


def test_gui_refuses_non_loopback_host(tmp_path: Path) -> None:
    # Create an empty file so the existence check passes.
    db = tmp_path / "x.duckdb"
    db.write_bytes(b"")
    runner = CliRunner()
    result = runner.invoke(razor_main, ["gui", "--host", "0.0.0.0", "--db", str(db)])
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "loopback" in combined.lower()


def test_gui_refuses_missing_db(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        razor_main,
        ["gui", "--db", str(tmp_path / "does-not-exist.duckdb")],
    )
    assert result.exit_code != 0
    assert "DuckDB store not found" in (result.output + (result.stderr or ""))


def test_resolve_db_path_explicit_wins() -> None:
    out = _resolve_db_path("/tmp/explicit.duckdb")
    assert str(out) == "/tmp/explicit.duckdb"


def test_resolve_db_path_env_fallback(monkeypatch) -> None:
    monkeypatch.setenv("RAZOR_ROOSTER_DB", "/tmp/from-env.duckdb")
    out = _resolve_db_path(None)
    assert str(out) == "/tmp/from-env.duckdb"


def test_resolve_port_default(monkeypatch) -> None:
    monkeypatch.delenv("RAZORROO_GUI_PORT", raising=False)
    assert _resolve_port(None) == 8765


def test_resolve_port_env(monkeypatch) -> None:
    monkeypatch.setenv("RAZORROO_GUI_PORT", "9876")
    assert _resolve_port(None) == 9876


def test_resolve_port_explicit_wins(monkeypatch) -> None:
    monkeypatch.setenv("RAZORROO_GUI_PORT", "9876")
    assert _resolve_port(8500) == 8500
