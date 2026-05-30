"""T-SCAN-040 — scan CLI acceptance tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library import registry
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    PrecursorVariable,
    Sector,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.signal_scanner.cli import scan
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    registry._clear_for_tests()
    registry._set_discovered_for_tests(True)
    yield
    registry._clear_for_tests()


def _occurrences(_conn: object) -> pd.DataFrame:
    return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})


def _precursor(_conn: object, _start: datetime, _end: datetime) -> pd.Series:
    idx = pd.date_range(end=_end, periods=10, freq="D", tz="UTC")
    return pd.Series([8.0] * 10, index=idx, dtype=float)


def _make_class(class_id: str = "cli_test_class") -> EventClass:
    return EventClass(
        class_id=class_id,
        title=f"CLI test class {class_id}",
        description="Synthetic class for CLI tests",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_occurrences,
        precursors=(
            PrecursorVariable(
                variable_id="v1",
                title="First precursor",
                query=_precursor,
                direction="high_signals_event",
                lead_time_window=timedelta(days=180),
            ),
        ),
    )


def _seed_db(tmp_path: Path, class_id: str = "cli_test_class") -> tuple[DuckDBStore, Path]:
    db_path = tmp_path / "trough.duckdb"
    store = DuckDBStore(db_path)
    now = datetime(2026, 5, 15, tzinfo=UTC)
    with store.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        # Seed pl rows.
        import json

        conn.execute(
            "INSERT OR REPLACE INTO pl_event_classes ("
            "class_id, title, description, domain_sector, secondary_sectors, "
            "definition_version, outcome_type, registered_at, "
            "last_evaluated_at, library_version_at_last_eval, removed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            [
                class_id,
                "x",
                "x",
                "public_health",
                json.dumps([]),
                1,
                "binary",
                now,
                now,
                1,
            ],
        )
        conn.execute(
            "INSERT INTO pl_base_rates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                class_id,
                now - timedelta(days=365 * 10),
                now,
                5,
                0.05,
                0.025,
                0.075,
                0.5,
                0.5,
                1,
                1,
                now,
                now,
                False,
                False,
                False,
            ],
        )
        conn.execute(
            "INSERT INTO pl_precursor_signatures VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                class_id,
                "v1",
                1,
                1,
                "youden_j",
                5.0,
                "high_signals_event",
                180,
                8.0,
                6.0,
                8.0,
                10.0,
                3.0,
                1.5,
                3.0,
                4.5,
                0.7,
                0.2,
                20,
                200,
                0.8,
                False,
                now,
            ],
        )
    return store, db_path


def test_scan_run_command_executes(tmp_path: Path) -> None:
    store, db_path = _seed_db(tmp_path)
    cls = _make_class("cli_test_class")
    registry.register(cls)
    try:
        runner = CliRunner()
        result = runner.invoke(scan, ["run", "--db", str(db_path), "--max-workers", "1"])
        assert result.exit_code == 0, result.output
        assert "scan_id" in result.output
        assert "cli_test_class" in result.output
    finally:
        store.close()


def test_scan_show_command(tmp_path: Path) -> None:
    store, db_path = _seed_db(tmp_path)
    cls = _make_class("cli_test_class")
    registry.register(cls)
    runner = CliRunner()
    try:
        run_result = runner.invoke(scan, ["run", "--db", str(db_path), "--max-workers", "1"])
        assert run_result.exit_code == 0
        # Extract scan_id from the run output
        scan_id_line = next(
            line for line in run_result.output.splitlines() if line.startswith("scan_id")
        )
        scan_id = scan_id_line.split()[-1]
        show_result = runner.invoke(scan, ["show", scan_id, "--db", str(db_path)])
        assert show_result.exit_code == 0, show_result.output
        assert scan_id in show_result.output
        assert "cli_test_class" in show_result.output
    finally:
        store.close()


def test_scan_show_trace_command(tmp_path: Path) -> None:
    store, db_path = _seed_db(tmp_path)
    cls = _make_class("cli_test_class")
    registry.register(cls)
    runner = CliRunner()
    try:
        run_result = runner.invoke(scan, ["run", "--db", str(db_path), "--max-workers", "1"])
        scan_id_line = next(
            line for line in run_result.output.splitlines() if line.startswith("scan_id")
        )
        scan_id = scan_id_line.split()[-1]
        trace_result = runner.invoke(
            scan, ["show-trace", scan_id, "cli_test_class", "--db", str(db_path)]
        )
        assert trace_result.exit_code == 0, trace_result.output
        assert "cli_test_class" in trace_result.output

        json_result = runner.invoke(
            scan,
            [
                "show-trace",
                scan_id,
                "cli_test_class",
                "--json",
                "--db",
                str(db_path),
            ],
        )
        assert json_result.exit_code == 0
        assert "class_id" in json_result.output
    finally:
        store.close()


def test_scan_show_trace_unknown(tmp_path: Path) -> None:
    store, db_path = _seed_db(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(
            scan, ["show-trace", "no-such-scan", "no-class", "--db", str(db_path)]
        )
        assert result.exit_code == 1
        assert "not found" in result.output
    finally:
        store.close()


def test_scan_list_candidates_empty(tmp_path: Path) -> None:
    store, db_path = _seed_db(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(scan, ["list-candidates", "--db", str(db_path)])
        assert result.exit_code == 0
        assert "no candidate situations" in result.output
    finally:
        store.close()


def test_scan_prune_refuses_without_confirm(tmp_path: Path) -> None:
    store, db_path = _seed_db(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(
            scan, ["prune", "--before", "2026-01-01T00:00:00+00:00", "--db", str(db_path)]
        )
        assert result.exit_code == 2
        assert "without --confirm" in result.output
    finally:
        store.close()


def test_scan_prune_with_confirm_zero_rows(tmp_path: Path) -> None:
    store, db_path = _seed_db(tmp_path)
    runner = CliRunner()
    try:
        result = runner.invoke(
            scan,
            [
                "prune",
                "--before",
                "2020-01-01T00:00:00+00:00",
                "--confirm",
                "--db",
                str(db_path),
            ],
        )
        assert result.exit_code == 0
        assert "pruned 0 scan" in result.output
    finally:
        store.close()


def test_scan_run_no_db_complains(tmp_path: Path) -> None:
    """Without an existing DB the CLI exits with 1 and an instructive message."""
    runner = CliRunner()
    result = runner.invoke(scan, ["run", "--db", str(tmp_path / "absent.duckdb")])
    assert result.exit_code == 1
    assert "not found" in result.output
