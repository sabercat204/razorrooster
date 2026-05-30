"""T-RG-010 — report_generator schema + migration tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "rg_schema.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_report_generator_migrations(c)
        yield c
    store.close()


def test_report_log_table_exists(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute("SELECT name FROM (SHOW TABLES) WHERE name = 'report_log'").fetchall()
    assert rows == [("report_log",)]


def test_report_log_index_exists(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'report_log'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_report_log_generated_at" in names


def test_migrations_recorded(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute("SELECT version FROM schema_migrations WHERE version = 7001").fetchall()
    assert rows == [(7001,)]


def test_migrations_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    run_pending_report_generator_migrations(conn)
    run_pending_report_generator_migrations(conn)
    rows = conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 7001").fetchone()
    assert rows is not None and rows[0] == 1
