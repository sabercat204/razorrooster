"""Unit tests for ``m6001_calibration_backtest_initial`` (T-CB-009).

Covers:

* ``MIGRATION_ID`` is in the 6001+ range and equal to ``VERSION_6001``.
* ``DESCRIPTION`` is set and human-readable.
* ``up(conn)`` creates the three tables defined in design §3.3 with the
  full column set from ``schemas.EXPECTED_*_COLUMNS``.
* ``up(conn)`` creates the indexes declared in :data:`schemas.INDEXES`.
* ``up(conn)`` is idempotent — running it twice is a safe no-op.
* A single ``up(conn)`` invocation produces all three tables on the
  connection.
* ``down(conn)`` drops the three tables.
"""

from __future__ import annotations

import duckdb
import pytest

from razor_rooster.calibration_backtest.persistence import schemas
from razor_rooster.calibration_backtest.persistence.migrations import (
    m6001_calibration_backtest_initial as m6001,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return [str(row[1]) for row in rows]


def _existing_table_names(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _existing_index_names(conn: duckdb.DuckDBPyConnection) -> set[str]:
    # ``duckdb_indexes()`` returns one row per index. The ``index_name`` column
    # is the second column in DuckDB >= 0.10.
    rows = conn.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
    return {str(row[0]) for row in rows}


# Expected column sets — kept in sync with tests/calibration_backtest/test_schemas.py
EXPECTED_RUNS_COLUMNS: tuple[str, ...] = (
    "run_id",
    "since_ts",
    "until_ts",
    "lag_days",
    "class_ids_json",
    "sectors_json",
    "venues_json",
    "library_version",
    "system_revision",
    "started_at",
    "completed_at",
    "status",
    "error_summary",
    "predictions_total",
    "predictions_scored",
    "predictions_skipped",
    "overall_brier",
    "summary_json",
    "bin_count_global",
    "bin_count_per_sector_json",
    "fallback_polarity_count",
    "allow_recent",
    "disclaimer_version",
)

EXPECTED_PREDICTIONS_COLUMNS: tuple[str, ...] = (
    "run_id",
    "prediction_id",
    "class_id",
    "condition_id",
    "venue",
    "sector",
    "prediction_ts",
    "resolution_ts",
    "model_p",
    "observed",
    "polarity",
    "polarity_source",
    "mapping_mismatch_warning",
    "definition_version",
    "status",
    "skip_reason",
    "brier_contribution",
)

EXPECTED_TRACES_COLUMNS: tuple[str, ...] = (
    "run_id",
    "prediction_id",
    "trace_json_compressed",
    "compression_algorithm",
    "decompressed_size_bytes",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


# ---------------------------------------------------------------------------
# Module-level metadata
# ---------------------------------------------------------------------------


def test_migration_id_is_6001() -> None:
    assert m6001.MIGRATION_ID == 6001
    # Cross-check: schemas.VERSION_6001 is the canonical source-of-truth
    # constant referenced by the migration module.
    assert m6001.MIGRATION_ID == schemas.VERSION_6001


def test_description_set() -> None:
    assert isinstance(m6001.DESCRIPTION, str)
    assert m6001.DESCRIPTION.strip(), "DESCRIPTION must not be empty"
    # Should reference the calibration-backtest tables for readability.
    lowered = m6001.DESCRIPTION.lower()
    assert "calibration_backtest" in lowered or "backtest" in lowered


def test_module_exposes_up_and_down() -> None:
    assert callable(m6001.up)
    assert callable(m6001.down)


# ---------------------------------------------------------------------------
# apply / up — table creation
# ---------------------------------------------------------------------------


def test_apply_creates_backtest_runs_table(conn: duckdb.DuckDBPyConnection) -> None:
    m6001.up(conn)
    cols = _table_columns(conn, schemas.TABLE_RUNS)
    assert sorted(cols) == sorted(EXPECTED_RUNS_COLUMNS)


def test_apply_creates_backtest_predictions_table(conn: duckdb.DuckDBPyConnection) -> None:
    m6001.up(conn)
    cols = _table_columns(conn, schemas.TABLE_PREDICTIONS)
    assert sorted(cols) == sorted(EXPECTED_PREDICTIONS_COLUMNS)


def test_apply_creates_backtest_traces_table(conn: duckdb.DuckDBPyConnection) -> None:
    m6001.up(conn)
    cols = _table_columns(conn, schemas.TABLE_TRACES)
    assert sorted(cols) == sorted(EXPECTED_TRACES_COLUMNS)


def test_apply_creates_all_three_tables_in_one_call(conn: duckdb.DuckDBPyConnection) -> None:
    m6001.up(conn)
    existing = _existing_table_names(conn)
    assert set(schemas.ALL_TABLES).issubset(existing), (
        f"missing tables after one apply(): {set(schemas.ALL_TABLES) - existing}"
    )


# ---------------------------------------------------------------------------
# apply / up — indexes
# ---------------------------------------------------------------------------


def test_apply_creates_indexes(conn: duckdb.DuckDBPyConnection) -> None:
    m6001.up(conn)
    index_names = _existing_index_names(conn)
    # Each explicit index defined in schemas.INDEXES must show up in
    # duckdb_indexes(). The DDL strings carry the index name after
    # ``CREATE INDEX IF NOT EXISTS``; extract them for comparison.
    expected_index_names: set[str] = set()
    for index_ddl in schemas.INDEXES:
        # DDL shape: "CREATE INDEX IF NOT EXISTS <name> ON <table> (<cols>)"
        prefix = "CREATE INDEX IF NOT EXISTS "
        assert index_ddl.startswith(prefix), f"unexpected DDL shape: {index_ddl!r}"
        remainder = index_ddl[len(prefix) :]
        name = remainder.split(" ", 1)[0]
        expected_index_names.add(name)

    missing = expected_index_names - index_names
    assert not missing, f"indexes missing after apply(): {missing}"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_apply_is_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    """Running ``up()`` twice on the same connection must not raise."""
    m6001.up(conn)
    m6001.up(conn)
    # Tables and columns should be unchanged after the second call.
    for table, expected_columns in (
        (schemas.TABLE_RUNS, EXPECTED_RUNS_COLUMNS),
        (schemas.TABLE_PREDICTIONS, EXPECTED_PREDICTIONS_COLUMNS),
        (schemas.TABLE_TRACES, EXPECTED_TRACES_COLUMNS),
    ):
        cols = _table_columns(conn, table)
        assert sorted(cols) == sorted(expected_columns)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_down_drops_all_tables(conn: duckdb.DuckDBPyConnection) -> None:
    m6001.up(conn)
    # Sanity: tables exist before down().
    assert set(schemas.ALL_TABLES).issubset(_existing_table_names(conn))

    m6001.down(conn)
    remaining = _existing_table_names(conn)
    for table in schemas.ALL_TABLES:
        assert table not in remaining, f"table {table!r} still present after down()"


def test_down_is_idempotent_on_clean_db(conn: duckdb.DuckDBPyConnection) -> None:
    """``down()`` on a database that has not yet been migrated must not raise."""
    m6001.down(conn)  # should be a no-op thanks to ``IF EXISTS``.
    remaining = _existing_table_names(conn)
    for table in schemas.ALL_TABLES:
        assert table not in remaining
