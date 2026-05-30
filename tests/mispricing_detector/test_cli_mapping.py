"""T-MD-020 — operator mapping CLI acceptance tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.cli import mispricing
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "trough.duckdb"
    store = DuckDBStore(path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_polymarket_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
    store.close()
    yield path


def test_map_command_registers_mapping(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        mispricing,
        [
            "map",
            "test_class_a",
            "0xabc",
            "--type",
            "direct",
            "--notes",
            "operator note",
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "mapping_id" in result.output
    assert "test_class_a" in result.output


def test_map_command_supports_inverted_polarity(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        mispricing,
        [
            "map",
            "test_class_a",
            "0xabc",
            "--polarity",
            "inverted",
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "polarity:          inverted" in result.output


def test_map_command_rejects_duplicate(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(
        mispricing,
        ["map", "test_class_a", "0xabc", "--db", str(db_path)],
    )
    second = runner.invoke(
        mispricing,
        ["map", "test_class_a", "0xabc", "--db", str(db_path)],
    )
    assert second.exit_code == 2
    assert "already exists" in second.output


def test_unmap_command_removes_mapping(db_path: Path) -> None:
    runner = CliRunner()
    map_result = runner.invoke(mispricing, ["map", "test_class_a", "0xabc", "--db", str(db_path)])
    assert map_result.exit_code == 0
    mapping_id_line = next(
        line for line in map_result.output.splitlines() if line.startswith("mapping_id")
    )
    mapping_id = mapping_id_line.split()[-1]
    unmap_result = runner.invoke(mispricing, ["unmap", mapping_id, "--db", str(db_path)])
    assert unmap_result.exit_code == 0, unmap_result.output
    assert mapping_id in unmap_result.output


def test_list_mappings_command(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(mispricing, ["map", "cls_a", "0xabc", "--db", str(db_path)])
    runner.invoke(mispricing, ["map", "cls_b", "0xdef", "--db", str(db_path)])
    result = runner.invoke(mispricing, ["list-mappings", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "cls_a" in result.output
    assert "cls_b" in result.output


def test_list_mappings_empty(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(mispricing, ["list-mappings", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "no active mappings" in result.output


def test_list_mappings_filter_by_class(db_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(mispricing, ["map", "cls_a", "0xabc", "--db", str(db_path)])
    runner.invoke(mispricing, ["map", "cls_b", "0xdef", "--db", str(db_path)])
    result = runner.invoke(
        mispricing,
        ["list-mappings", "--class", "cls_a", "--db", str(db_path)],
    )
    assert result.exit_code == 0
    assert "cls_a" in result.output
    assert "cls_b" not in result.output
