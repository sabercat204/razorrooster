"""T-MON-010 — schema migration acceptance tests."""

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
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "monitor.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_polymarket_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_position_engine_migrations(c)
        run_pending_monitor_migrations(c)
        yield c
    store.close()


def test_creates_three_monitor_tables(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name IN ('monitor_cycles', 'follow_ups', 'follow_up_notes') "
        "ORDER BY table_name"
    ).fetchall()
    table_names = [r[0] for r in rows]
    assert table_names == ["follow_up_notes", "follow_ups", "monitor_cycles"]


def test_records_schema_migrations_row(conn: duckdb.DuckDBPyConnection) -> None:
    row = conn.execute("SELECT version FROM schema_migrations WHERE version = 6001").fetchone()
    assert row is not None
    assert row[0] == 6001


def test_migration_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    applied_again = run_pending_monitor_migrations(conn)
    assert all(m.version != 6001 for m in applied_again)
    rows = conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 6001").fetchone()
    assert rows is not None and rows[0] == 1


def test_follow_ups_columns_present(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'follow_ups' ORDER BY ordinal_position"
    ).fetchall()
    cols = {r[0] for r in rows}
    expected = {
        "follow_up_id",
        "cycle_id",
        "analysis_id",
        "model_probability_shift",
        "model_shift_band",
        "market_probability_shift",
        "market_shift_band",
        "precursor_snapshot",
        "invalidation_evaluations",
        "invalidation_triggered_count",
        "resolution_status",
        "recommended_review",
        "primary_alert_tier",
        "alert_tiers",
        "reasoning_text",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_indexes_present(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT index_name FROM duckdb_indexes() "
        "WHERE index_name LIKE 'idx_follow_ups_%' "
        "   OR index_name LIKE 'idx_monitor_cycles_%' "
        "   OR index_name LIKE 'idx_follow_up_notes_%'"
    ).fetchall()
    index_names = {r[0] for r in rows}
    assert "idx_follow_ups_analysis_computed" in index_names
    assert "idx_follow_ups_cycle_alert" in index_names
    assert "idx_follow_up_notes_follow_up_set_at" in index_names
