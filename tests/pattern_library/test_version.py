"""T-PL-020 — library version + bump-recording tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.pattern_library.version import (
    LIBRARY_VERSION,
    BumpReason,
    bump_for_reason,
    current_version,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "pl_version.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_pattern_library_migrations(conn)
    try:
        yield s
    finally:
        s.close()


def test_current_version_returns_constant() -> None:
    assert current_version() == LIBRARY_VERSION


def test_bump_for_reason_records_row(store: DuckDBStore) -> None:
    when = datetime(2026, 5, 14, tzinfo=UTC)
    with store.connection() as conn:
        bump = bump_for_reason(
            conn,
            reason=BumpReason.CLASS_ADDED,
            affected_class_ids=("c1", "c2"),
            notes="seed_class added",
            when=when,
        )
        row = conn.execute(
            "SELECT library_version, bump_reason, notes FROM pl_library_versions "
            "WHERE library_version = ?",
            [bump.library_version],
        ).fetchone()

    assert bump.library_version == LIBRARY_VERSION
    assert bump.bump_reason == BumpReason.CLASS_ADDED
    assert bump.affected_class_ids == ("c1", "c2")
    assert bump.bumped_at == when
    assert row is not None
    assert row[0] == LIBRARY_VERSION
    assert row[1] == BumpReason.CLASS_ADDED
    assert row[2] == "seed_class added"


def test_bump_for_reason_idempotent(store: DuckDBStore) -> None:
    """Recording the same library_version twice is a no-op."""
    with store.connection() as conn:
        bump_for_reason(conn, reason=BumpReason.CODE_CHANGE)
        bump_for_reason(conn, reason=BumpReason.CLASS_ADDED)
        rows = conn.execute(
            "SELECT COUNT(*) FROM pl_library_versions WHERE library_version = ?",
            [LIBRARY_VERSION],
        ).fetchone()
    assert rows is not None
    assert rows[0] == 1


def test_bump_for_reason_rejects_unknown_reason(store: DuckDBStore) -> None:
    with store.connection() as conn, pytest.raises(ValueError, match="unknown bump_reason"):
        bump_for_reason(conn, reason="not_a_reason")


def test_bump_reason_constants() -> None:
    assert BumpReason.CODE_CHANGE == "code_change"
    assert BumpReason.CLASS_ADDED == "class_added"
    assert BumpReason.CLASS_MODIFIED == "class_modified"
    assert BumpReason.CLASS_REMOVED == "class_removed"
