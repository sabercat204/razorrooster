"""T-RG-050 — CLI subcommand tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.monitor.persistence.migrations import (
    run_pending_monitor_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.report_generator.cli import report
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "rg_cli.duckdb"
    store = DuckDBStore(path)
    with store.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        run_pending_mispricing_migrations(conn)
        run_pending_position_engine_migrations(conn)
        run_pending_monitor_migrations(conn)
        run_pending_report_generator_migrations(conn)
    store.close()
    yield path


def test_version_subcommand(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(report, ["version"])
    assert result.exit_code == 0
    assert "7001+" in result.output


def test_generate_writes_report_to_db(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        report,
        ["generate", "--db", str(db_path), "--quiet"],
    )
    assert result.exit_code == 0, result.output
    assert "report_id:" in result.output
    assert "sections_rendered:" in result.output


def test_generate_writes_markdown(db_path: Path, tmp_path: Path) -> None:
    md_path = tmp_path / "out" / "report.md"
    runner = CliRunner()
    result = runner.invoke(
        report,
        [
            "generate",
            "--db",
            str(db_path),
            "--markdown",
            str(md_path),
            "--quiet",
        ],
    )
    assert result.exit_code == 0, result.output
    assert md_path.exists()
    assert f"markdown_path: {md_path}" in result.output


def test_generate_invalid_since(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        report,
        ["generate", "--db", str(db_path), "--since", "not-a-date", "--quiet"],
    )
    assert result.exit_code == 1
    assert "Invalid --since" in result.output


def test_show_existing_report(db_path: Path) -> None:
    runner = CliRunner()
    gen = runner.invoke(report, ["generate", "--db", str(db_path), "--quiet"])
    assert gen.exit_code == 0
    # Pull the report_id from the output.
    report_id = next(
        line.split(": ", 1)[1].strip()
        for line in gen.output.splitlines()
        if line.startswith("report_id:")
    )
    show_result = runner.invoke(report, ["show", report_id, "--db", str(db_path)])
    assert show_result.exit_code == 0, show_result.output
    assert "RAZOR-ROOSTER REPORT" in show_result.output


def test_show_missing_report(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(report, ["show", "nope", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "No report found" in result.output


def test_list_empty(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(report, ["list", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "No reports" in result.output


def test_list_after_generate(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(report, ["generate", "--db", str(db_path), "--quiet"])
    runner.invoke(report, ["generate", "--db", str(db_path), "--quiet"])
    result = runner.invoke(report, ["list", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    # Two generated_at lines.
    lines = [ln for ln in result.output.splitlines() if ln and not ln.startswith("No reports")]
    assert len(lines) == 2


def test_list_invalid_since(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        report,
        ["list", "--db", str(db_path), "--since", "not-a-date"],
    )
    assert result.exit_code == 1
    assert "Invalid --since" in result.output


def test_latest_no_reports(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(report, ["latest", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "No reports yet" in result.output


def test_latest_after_generate(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(report, ["generate", "--db", str(db_path), "--quiet"])
    result = runner.invoke(report, ["latest", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "RAZOR-ROOSTER REPORT" in result.output
