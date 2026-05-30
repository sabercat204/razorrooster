"""T-MD-010 — schema migration acceptance tests."""

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
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "md.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        yield c
    store.close()


def test_migration_creates_six_tables(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name IN ("
        "'class_market_mappings', 'comparison_cycles', 'comparisons', "
        "'comparison_traces', 'comparison_resolutions', 'mispricing_detector_state'"
        ") ORDER BY table_name"
    ).fetchall()
    table_names = [r[0] for r in rows]
    assert table_names == [
        "class_market_mappings",
        "comparison_cycles",
        "comparison_resolutions",
        "comparison_traces",
        "comparisons",
        "mispricing_detector_state",
    ]


def test_migration_records_schema_migrations_row(conn: duckdb.DuckDBPyConnection) -> None:
    row = conn.execute("SELECT version FROM schema_migrations WHERE version = 4001").fetchone()
    assert row is not None
    assert row[0] == 4001


def test_migration_is_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    applied_again = run_pending_mispricing_migrations(conn)
    assert all(m.version != 4001 for m in applied_again)
    rows = conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 4001").fetchone()
    assert rows is not None and rows[0] == 1


def test_active_mapping_uniqueness_application_level(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """DuckDB doesn't support partial indexes, so the active-mapping
    uniqueness invariant is enforced via ``register_mapping``. Direct
    inserts at the SQL level can violate it; application code never does.
    """
    from razor_rooster.mispricing_detector.persistence.operations import (
        MappingExistsError,
        register_mapping,
    )

    register_mapping(
        conn,
        class_id="cls_unique",
        condition_id="0xabc",
        mapping_type="direct",
    )
    with pytest.raises(MappingExistsError):
        register_mapping(
            conn,
            class_id="cls_unique",
            condition_id="0xabc",
            mapping_type="direct",
        )


def test_comparisons_columns_present(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'comparisons' ORDER BY ordinal_position"
    ).fetchall()
    cols = {r[0] for r in rows}
    expected = {
        "comparison_id",
        "cycle_id",
        "polarity",
        "model_probability",
        "market_probability",
        "delta",
        "log_odds_delta",
        "ci_overlap",
        "expected_value",
        "confidence_weighted_score",
        "surfaced",
        "suppression_reasons",
        "stale_market_price",
        "no_market_price",
        "low_mapping_confidence",
    }
    assert expected.issubset(cols), f"missing columns: {expected - cols}"


def test_indexes_present(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT index_name FROM duckdb_indexes() "
        "WHERE index_name LIKE 'idx_class_market_%' "
        "   OR index_name LIKE 'idx_comparison%'"
    ).fetchall()
    index_names = {r[0] for r in rows}
    assert "idx_class_market_mappings_class" in index_names
    assert "idx_comparisons_cycle_surfaced" in index_names
