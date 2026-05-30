"""T-SCAN-010 — schema migration acceptance tests.

Confirms the m3001 migration applies cleanly, the three tables exist
afterwards with the expected columns, and the ``schema_migrations``
row is recorded.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "scan.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        yield c
    store.close()


def test_migration_creates_three_scan_tables(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name LIKE 'scan_%' ORDER BY table_name"
    ).fetchall()
    table_names = [r[0] for r in rows]
    assert table_names == ["scan_records", "scan_summaries", "scan_traces"]


def test_migration_records_schema_migrations_row(conn: duckdb.DuckDBPyConnection) -> None:
    row = conn.execute(
        "SELECT version, description FROM schema_migrations WHERE version = 3001"
    ).fetchone()
    assert row is not None
    assert row[0] == 3001
    assert "signal" in row[1].lower() or "scan" in row[1].lower() or "initial" in row[1].lower()


def test_migration_is_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    """Re-running the runner adds no extra schema_migrations rows for 3001."""
    applied_again = run_pending_signal_scanner_migrations(conn)
    assert all(m.version != 3001 for m in applied_again)
    rows = conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 3001").fetchone()
    assert rows is not None and rows[0] == 1


def test_scan_records_has_expected_columns(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'scan_records' ORDER BY ordinal_position"
    ).fetchall()
    cols = [r[0] for r in rows]
    expected = {
        "scan_id",
        "class_id",
        "pattern_library_version",
        "base_rate",
        "posterior",
        "log_odds_shift",
        "is_candidate",
        "candidate_direction",
        "signature_confidence",
        "definition_drift_warning",
        "no_update_applied",
        "error",
    }
    assert expected.issubset(set(cols)), f"missing columns: {expected - set(cols)}"


def test_scan_indexes_present(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE index_name LIKE 'idx_scan_%'"
    ).fetchall()
    index_names = {r[0] for r in rows}
    assert "idx_scan_records_class_started" in index_names
    assert "idx_scan_records_candidate_started" in index_names
