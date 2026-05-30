"""T-013 verification — schema migrations framework.

Verifies:
- Discovery picks up valid migration modules and parses them correctly.
- Discovery rejects malformed modules.
- Discovery rejects duplicate version numbers.
- ``run_pending_migrations`` applies migrations in order.
- Re-running ``run_pending_migrations`` after the first run is a no-op.
- Each migration is transactional: a failing migration leaves no partial state.
- ``rollback_migration`` reverses an applied migration.
- The bundled m0001 produces all canonical and operational tables plus the
  freshness view.
"""

from __future__ import annotations

import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.migrations import (
    MigrationApplicationError,
    MigrationDiscoveryError,
    MigrationError,
    applied_versions,
    discover_migrations,
    rollback_migration,
    run_pending_migrations,
)


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


def test_discover_finds_real_m0001() -> None:
    discovered = discover_migrations()
    assert any(m.version == 1 and "initial" in m.description for m in discovered)


def test_discover_returns_versions_in_order() -> None:
    discovered = discover_migrations()
    versions = [m.version for m in discovered]
    assert versions == sorted(versions)


def test_run_pending_applies_m0001(conn: duckdb.DuckDBPyConnection) -> None:
    applied = run_pending_migrations(conn)
    assert len(applied) >= 1
    assert applied[0].version == 1

    rows = conn.execute(
        "SELECT version, description FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert rows[0][0] == 1
    assert "initial" in rows[0][1]


def test_run_pending_creates_canonical_and_operational_tables(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    run_pending_migrations(conn)
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    table_names = {r[0] for r in rows}
    expected = {
        # canonical
        "event_stream",
        "time_series",
        "document_docket",
        "geospatial_indicator",
        # operational
        "sources",
        "backfill_state",
        "ingest_anomalies",
        "cycle_log",
        "schema_migrations",
    }
    assert expected <= table_names

    # freshness view
    view_rows = conn.execute(
        "SELECT view_name FROM duckdb_views() WHERE schema_name = 'main'"
    ).fetchall()
    assert "freshness" in {r[0] for r in view_rows}


def test_run_pending_is_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    first = run_pending_migrations(conn)
    second = run_pending_migrations(conn)
    assert len(first) >= 1
    assert second == ()


def test_applied_versions_returns_recorded_versions(conn: duckdb.DuckDBPyConnection) -> None:
    run_pending_migrations(conn)
    versions = applied_versions(conn)
    assert 1 in versions


def test_rollback_reverses_m0001(conn: duckdb.DuckDBPyConnection) -> None:
    run_pending_migrations(conn)
    # Sanity: tables exist.
    table_count_before = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchone()
    assert table_count_before is not None
    assert table_count_before[0] > 1

    rollback_migration(conn, version=1)

    # Operational tables and canonical tables should be gone (schema_migrations
    # remains, but its row for version 1 is removed).
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    table_names = {r[0] for r in rows}
    for t in (
        "event_stream",
        "time_series",
        "document_docket",
        "geospatial_indicator",
        "sources",
        "backfill_state",
        "ingest_anomalies",
        "cycle_log",
    ):
        assert t not in table_names

    versions = applied_versions(conn)
    assert 1 not in versions

    # After rollback, run_pending_migrations should re-apply.
    re_applied = run_pending_migrations(conn)
    assert any(m.version == 1 for m in re_applied)


def test_rollback_unknown_version_raises(conn: duckdb.DuckDBPyConnection) -> None:
    run_pending_migrations(conn)
    with pytest.raises(MigrationError):
        rollback_migration(conn, version=9999)


def test_rollback_unapplied_version_raises(conn: duckdb.DuckDBPyConnection) -> None:
    # Don't apply anything; rollback of v1 should fail because it isn't applied.
    with pytest.raises(MigrationError):
        rollback_migration(conn, version=1)


def _write_synthetic_migration_package(
    tmp_path: Path,
    name: str,
    files: dict[str, str],
) -> str:
    """Create a Python package on disk and add its parent to sys.path.

    Returns the package's importable name.
    """
    pkg_dir = tmp_path / name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    for filename, content in files.items():
        (pkg_dir / filename).write_text(textwrap.dedent(content))
    sys.path.insert(0, str(tmp_path))
    return name


def test_discovery_rejects_malformed_module(tmp_path: Path) -> None:
    # A file matching the migration naming pattern but missing 'down'.
    pkg = _write_synthetic_migration_package(
        tmp_path,
        "test_pkg_malformed",
        {
            "m0001_bad.py": """\
                def up(conn):
                    pass
                # intentionally no `down`
                """,
        },
    )
    try:
        with pytest.raises(MigrationDiscoveryError):
            discover_migrations(pkg)
    finally:
        sys.path.remove(str(tmp_path))


def test_discovery_rejects_duplicate_version(tmp_path: Path) -> None:
    pkg = _write_synthetic_migration_package(
        tmp_path,
        "test_pkg_duplicate",
        {
            "m0001_a.py": """\
                def up(conn):
                    pass
                def down(conn):
                    pass
                """,
            "m0001_b.py": """\
                def up(conn):
                    pass
                def down(conn):
                    pass
                """,
        },
    )
    try:
        with pytest.raises(MigrationDiscoveryError):
            discover_migrations(pkg)
    finally:
        sys.path.remove(str(tmp_path))


def test_failing_migration_rolls_back_transaction(
    tmp_path: Path, conn: duckdb.DuckDBPyConnection
) -> None:
    """A migration that raises during ``up()`` must leave no partial state."""
    pkg = _write_synthetic_migration_package(
        tmp_path,
        "test_pkg_failing",
        {
            "m0001_will_fail.py": """\
                def up(conn):
                    conn.execute("CREATE TABLE created_before_failure (x INTEGER)")
                    raise RuntimeError("simulated migration failure")
                def down(conn):
                    conn.execute("DROP TABLE IF EXISTS created_before_failure")
                """,
        },
    )
    try:
        with pytest.raises(MigrationApplicationError):
            run_pending_migrations(conn, package_name=pkg)

        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        assert "created_before_failure" not in {r[0] for r in rows}
        assert applied_versions(conn) == ()
    finally:
        sys.path.remove(str(tmp_path))


def test_synthetic_migration_runs_and_records_version(
    tmp_path: Path, conn: duckdb.DuckDBPyConnection
) -> None:
    pkg = _write_synthetic_migration_package(
        tmp_path,
        "test_pkg_ok",
        {
            "m0001_make_a_table.py": """\
                def up(conn):
                    conn.execute("CREATE TABLE synth (x INTEGER)")
                def down(conn):
                    conn.execute("DROP TABLE synth")
                """,
        },
    )
    try:
        applied = run_pending_migrations(conn, package_name=pkg)
        assert len(applied) == 1
        assert applied[0].version == 1
        rows = conn.execute("SELECT COUNT(*) FROM synth").fetchall()
        assert rows == [(0,)]
    finally:
        sys.path.remove(str(tmp_path))


def test_applied_at_is_recorded_in_utc(conn: duckdb.DuckDBPyConnection) -> None:
    run_pending_migrations(conn)
    rows = conn.execute("SELECT applied_at FROM schema_migrations WHERE version = 1").fetchall()
    assert len(rows) == 1
    applied_at = rows[0][0]
    assert isinstance(applied_at, datetime)
    # TIMESTAMPTZ in DuckDB returns a tz-aware datetime; we wrote UTC, so
    # the stored value should round-trip with tzinfo set.
    assert applied_at.tzinfo is not None
    # Allow some clock skew (tests run quickly, so this is loose).
    delta = datetime.now(tz=UTC) - applied_at
    assert abs(delta.total_seconds()) < 60
