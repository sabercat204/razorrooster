"""Unit tests for calibration_backtest.persistence.schemas (T-CB-007).

Covers:
* table-name constants matching the design doc;
* DDL strings containing every column listed in design §3.3;
* DDL strings using ``CREATE TABLE IF NOT EXISTS``;
* INDEXES tuple is well-formed (each entry begins with
  ``CREATE INDEX IF NOT EXISTS``);
* helper accessors :func:`list_tables` / :func:`list_ddl` shape;
* every DDL statement parses against an in-memory DuckDB connection and the
  resulting tables expose the expected column count;
* DDL is idempotent (executing twice does not error — the
  ``IF NOT EXISTS`` clause).
"""

from __future__ import annotations

import duckdb
import pytest

from razor_rooster.calibration_backtest.persistence import schemas
from razor_rooster.calibration_backtest.persistence.schemas import (
    ALL_DDL,
    ALL_TABLES,
    DDL_PREDICTIONS,
    DDL_RUNS,
    DDL_TRACES,
    INDEXES,
    SCHEMA_NAMESPACE,
    TABLE_PREDICTIONS,
    TABLE_RUNS,
    TABLE_TRACES,
    VERSION_6001,
    VERSION_6002,
    list_ddl,
    list_tables,
)

# ---------------------------------------------------------------------------
# Expected column sets (design §3.3 — kept here so tests fail loudly if the
# DDL drifts from the design doc).
# ---------------------------------------------------------------------------

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
# Constant sanity checks
# ---------------------------------------------------------------------------


def test_table_constants_match_design() -> None:
    assert TABLE_RUNS == "backtest_runs"
    assert TABLE_PREDICTIONS == "backtest_predictions"
    assert TABLE_TRACES == "backtest_traces"


def test_schema_namespace_is_calibration_backtest() -> None:
    assert SCHEMA_NAMESPACE == "calibration_backtest"


def test_migration_version_helpers_in_6001_range() -> None:
    assert VERSION_6001 == 6001
    assert VERSION_6002 == 6002


def test_all_tables_tuple_is_three_unique_entries() -> None:
    assert ALL_TABLES == (TABLE_RUNS, TABLE_PREDICTIONS, TABLE_TRACES)
    assert len(set(ALL_TABLES)) == 3


def test_all_ddl_tuple_pairs_with_all_tables() -> None:
    assert len(ALL_DDL) == len(ALL_TABLES) == 3


# ---------------------------------------------------------------------------
# DDL substring checks (no SQL parsing — substring is enough to catch drift).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("column", EXPECTED_RUNS_COLUMNS)
def test_ddl_runs_contains_all_columns(column: str) -> None:
    assert column in DDL_RUNS, f"DDL_RUNS missing column {column!r}"


@pytest.mark.parametrize("column", EXPECTED_PREDICTIONS_COLUMNS)
def test_ddl_predictions_contains_all_columns(column: str) -> None:
    assert column in DDL_PREDICTIONS, f"DDL_PREDICTIONS missing column {column!r}"


@pytest.mark.parametrize("column", EXPECTED_TRACES_COLUMNS)
def test_ddl_traces_contains_all_columns(column: str) -> None:
    assert column in DDL_TRACES, f"DDL_TRACES missing column {column!r}"


@pytest.mark.parametrize("ddl", ALL_DDL)
def test_ddl_includes_create_table_if_not_exists(ddl: str) -> None:
    assert "CREATE TABLE IF NOT EXISTS" in ddl


def test_ddl_runs_primary_key_on_run_id() -> None:
    assert "run_id" in DDL_RUNS
    assert "PRIMARY KEY" in DDL_RUNS


def test_ddl_predictions_composite_primary_key() -> None:
    assert "PRIMARY KEY (run_id, prediction_id)" in DDL_PREDICTIONS


def test_ddl_traces_composite_primary_key() -> None:
    assert "PRIMARY KEY (run_id, prediction_id)" in DDL_TRACES


def test_ddl_runs_status_check_constraint() -> None:
    assert "status IN ('in_progress', 'complete', 'failed')" in DDL_RUNS


def test_ddl_predictions_polarity_check_constraint() -> None:
    assert "polarity IN ('direct', 'inverted')" in DDL_PREDICTIONS


def test_ddl_predictions_polarity_source_check_constraint() -> None:
    assert "'comparison_resolutions'" in DDL_PREDICTIONS
    assert "'current_mapping_fallback'" in DDL_PREDICTIONS


def test_ddl_predictions_skip_reason_closed_enum() -> None:
    for reason in (
        "insufficient_lag",
        "invalid_resolution",
        "source_data_not_frozen",
        "no_polarity_resolution",
        "insufficient_data",
        "exception",
    ):
        assert f"'{reason}'" in DDL_PREDICTIONS


def test_ddl_traces_compression_algorithm_default_zstd() -> None:
    assert "DEFAULT 'zstd'" in DDL_TRACES
    assert "compression_algorithm IN ('zstd')" in DDL_TRACES


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index_ddl", INDEXES)
def test_indexes_well_formed(index_ddl: str) -> None:
    assert index_ddl.startswith("CREATE INDEX IF NOT EXISTS")


def test_indexes_cover_design_required_pairs() -> None:
    joined = "\n".join(INDEXES)
    # design §3.3 explicitly calls out these two on backtest_runs.
    assert "(status, started_at)" in joined
    assert "(library_version, system_revision)" in joined


# ---------------------------------------------------------------------------
# Helper accessors
# ---------------------------------------------------------------------------


def test_list_tables_returns_three() -> None:
    tables = list_tables()
    assert len(tables) == 3
    assert tables == ALL_TABLES


def test_list_ddl_includes_indexes() -> None:
    ddl = list_ddl()
    assert len(ddl) == len(ALL_DDL) + len(INDEXES)
    # Tables must come before indexes so index creation can reference the table.
    assert ddl[: len(ALL_DDL)] == ALL_DDL
    assert ddl[len(ALL_DDL) :] == INDEXES


# ---------------------------------------------------------------------------
# DuckDB executable round-trip
# ---------------------------------------------------------------------------


def _table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return [str(row[1]) for row in rows]


def test_ddl_runs_is_executable_against_in_memory_duckdb() -> None:
    with duckdb.connect(":memory:") as conn:
        conn.execute(DDL_RUNS)
        cols = _table_columns(conn, TABLE_RUNS)
    assert sorted(cols) == sorted(EXPECTED_RUNS_COLUMNS)


def test_ddl_predictions_is_executable_against_in_memory_duckdb() -> None:
    with duckdb.connect(":memory:") as conn:
        conn.execute(DDL_PREDICTIONS)
        cols = _table_columns(conn, TABLE_PREDICTIONS)
    assert sorted(cols) == sorted(EXPECTED_PREDICTIONS_COLUMNS)


def test_ddl_traces_is_executable_against_in_memory_duckdb() -> None:
    with duckdb.connect(":memory:") as conn:
        conn.execute(DDL_TRACES)
        cols = _table_columns(conn, TABLE_TRACES)
    assert sorted(cols) == sorted(EXPECTED_TRACES_COLUMNS)


def test_ddl_idempotent() -> None:
    """Each DDL must be re-runnable thanks to ``IF NOT EXISTS``."""
    with duckdb.connect(":memory:") as conn:
        for ddl in list_ddl():
            conn.execute(ddl)
        # Second pass is the actual idempotency check — must not raise.
        for ddl in list_ddl():
            conn.execute(ddl)
        for table in ALL_TABLES:
            assert _table_columns(conn, table), f"table {table} disappeared after re-apply"


def test_full_schema_applies_in_one_pass() -> None:
    """Sanity: all tables + indexes can be created in a single connection."""
    with duckdb.connect(":memory:") as conn:
        for ddl in list_ddl():
            conn.execute(ddl)
        existing = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        assert set(ALL_TABLES).issubset(existing)


def test_module_exports_match_dunder_all() -> None:
    # Each name in ``__all__`` must resolve on the module.
    for name in schemas.__all__:
        assert hasattr(schemas, name), f"schemas.__all__ references missing attribute {name!r}"
