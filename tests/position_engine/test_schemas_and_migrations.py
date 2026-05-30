"""T-PE-010 — schema migration acceptance tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "pe.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_position_engine_migrations(c)
        yield c
    store.close()


def test_creates_five_position_engine_tables(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name IN ("
        "'bankroll_config', 'analysis_cycles', 'analyses', "
        "'analysis_traces', 'watch_states') ORDER BY table_name"
    ).fetchall()
    table_names = [r[0] for r in rows]
    assert table_names == [
        "analyses",
        "analysis_cycles",
        "analysis_traces",
        "bankroll_config",
        "watch_states",
    ]


def test_records_schema_migrations_row(conn: duckdb.DuckDBPyConnection) -> None:
    row = conn.execute("SELECT version FROM schema_migrations WHERE version = 5001").fetchone()
    assert row is not None
    assert row[0] == 5001


def test_migration_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    applied_again = run_pending_position_engine_migrations(conn)
    assert all(m.version != 5001 for m in applied_again)
    rows = conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 5001").fetchone()
    assert rows is not None and rows[0] == 1


def test_analyses_columns_present(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'analyses' ORDER BY ordinal_position"
    ).fetchall()
    cols = {r[0] for r in rows}
    expected = {
        "analysis_id",
        "comparison_id",
        "kelly_unclamped",
        "kelly_negative",
        "suggested_fraction",
        "suggested_dollar_size",
        "ev_per_dollar",
        "bankroll_after_1_loss_pct",
        "bankroll_after_3_losses_pct",
        "bankroll_after_5_losses_pct",
        "long_time_to_resolution",
        "sub_threshold",
        "sensitivity_analysis",
        "invalidation_criteria",
        "low_mapping_confidence",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_indexes_present(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT index_name FROM duckdb_indexes() "
        "WHERE index_name LIKE 'idx_analyses_%' "
        "   OR index_name LIKE 'idx_watch_%' "
        "   OR index_name LIKE 'idx_bankroll_%'"
    ).fetchall()
    index_names = {r[0] for r in rows}
    assert "idx_analyses_cycle" in index_names
    assert "idx_analyses_comparison" in index_names
    assert "idx_watch_states_analysis_set_at" in index_names
    assert "idx_bankroll_config_effective" in index_names
