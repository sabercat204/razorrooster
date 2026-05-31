"""Unit tests for ``m6002_polarity_source_columns`` (T-CB-009).

Covers:

* ``MIGRATION_ID`` is 6002 and equal to ``schemas.VERSION_6002``.
* ``DESCRIPTION`` is set and human-readable.
* Module exposes callable ``up`` / ``down``.
* Applying m6002 after m6001 leaves ``polarity_source`` /
  ``mapping_mismatch_warning`` on ``backtest_predictions`` and adds the
  ``idx_backtest_predictions_polarity_source`` index on
  ``(run_id, polarity_source)``.
* Re-applying m6002 is idempotent (the underlying ALTER + CREATE INDEX
  use ``IF NOT EXISTS``).
* Applying m6002 against an empty database (i.e. before m6001) raises a
  DuckDB error — confirms migration ordering is enforced upstream by the
  ``run_pending_migrations`` runner via the discovered version order.
* ``down(conn)`` drops the index without removing the columns (the
  columns are part of the canonical m6001 DDL).
* The shared ``run_pending_migrations`` runner discovers and applies
  m6001 then m6002 in order, recording both versions in
  ``schema_migrations``.
"""

from __future__ import annotations

import duckdb
import pytest

from razor_rooster.calibration_backtest.persistence import schemas
from razor_rooster.calibration_backtest.persistence.migrations import (
    m6001_calibration_backtest_initial as m6001,
)
from razor_rooster.calibration_backtest.persistence.migrations import (
    m6002_polarity_source_columns as m6002,
)
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return [str(row[1]) for row in rows]


def _existing_index_names(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
    return {str(row[0]) for row in rows}


def _applied_versions(conn: duckdb.DuckDBPyConnection) -> list[int]:
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return [int(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


# ---------------------------------------------------------------------------
# Module-level metadata
# ---------------------------------------------------------------------------


def test_migration_id_is_6002() -> None:
    assert m6002.MIGRATION_ID == 6002
    assert m6002.MIGRATION_ID == schemas.VERSION_6002


def test_description_set() -> None:
    assert isinstance(m6002.DESCRIPTION, str)
    assert m6002.DESCRIPTION.strip(), "DESCRIPTION must not be empty"
    lowered = m6002.DESCRIPTION.lower()
    assert "polarity" in lowered, "DESCRIPTION should mention polarity given the migration's intent"


def test_module_exposes_up_and_down() -> None:
    assert callable(m6002.up)
    assert callable(m6002.down)


def test_index_name_constant_matches_naming_convention() -> None:
    # Index name should match the project's idx_<table>_<columns> convention.
    assert m6002.INDEX_NAME == "idx_backtest_predictions_polarity_source"


# ---------------------------------------------------------------------------
# Forward path: m6001 then m6002
# ---------------------------------------------------------------------------


def test_apply_after_m6001_creates_index(conn: duckdb.DuckDBPyConnection) -> None:
    m6001.up(conn)
    m6002.up(conn)
    assert m6002.INDEX_NAME in _existing_index_names(conn)


def test_apply_after_m6001_preserves_polarity_columns(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``polarity_source`` and ``mapping_mismatch_warning`` should be present.

    They are added by m6001 via the canonical DDL; m6002 confirms via
    ``ADD COLUMN IF NOT EXISTS`` that they exist (forward-compat against
    hypothetical pre-m6001 schemas).
    """
    m6001.up(conn)
    m6002.up(conn)
    cols = _table_columns(conn, schemas.TABLE_PREDICTIONS)
    assert "polarity_source" in cols
    assert "mapping_mismatch_warning" in cols


def test_apply_does_not_alter_existing_predictions_columns(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Re-applying m6002 must leave the m6001 column set unchanged."""
    m6001.up(conn)
    cols_before = _table_columns(conn, schemas.TABLE_PREDICTIONS)
    m6002.up(conn)
    cols_after = _table_columns(conn, schemas.TABLE_PREDICTIONS)
    assert cols_before == cols_after


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_apply_is_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    """Running ``up()`` twice on the same connection must not raise."""
    m6001.up(conn)
    m6002.up(conn)
    m6002.up(conn)
    cols = _table_columns(conn, schemas.TABLE_PREDICTIONS)
    assert "polarity_source" in cols
    assert "mapping_mismatch_warning" in cols
    assert m6002.INDEX_NAME in _existing_index_names(conn)


# ---------------------------------------------------------------------------
# Order enforcement: m6002 alone fails on an empty database
# ---------------------------------------------------------------------------


def test_apply_without_m6001_raises(conn: duckdb.DuckDBPyConnection) -> None:
    """``backtest_predictions`` does not exist yet — the ALTER must fail.

    This confirms that migration order matters: the shared runner enforces
    ordering by discovering modules numerically and applying them in
    sequence.
    """
    with pytest.raises(duckdb.Error):
        m6002.up(conn)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_down_drops_index(conn: duckdb.DuckDBPyConnection) -> None:
    m6001.up(conn)
    m6002.up(conn)
    assert m6002.INDEX_NAME in _existing_index_names(conn)

    m6002.down(conn)
    assert m6002.INDEX_NAME not in _existing_index_names(conn)


def test_down_keeps_polarity_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """``down()`` must NOT remove the columns (they belong to m6001)."""
    m6001.up(conn)
    m6002.up(conn)
    m6002.down(conn)
    cols = _table_columns(conn, schemas.TABLE_PREDICTIONS)
    assert "polarity_source" in cols
    assert "mapping_mismatch_warning" in cols


def test_down_is_idempotent_on_clean_db(conn: duckdb.DuckDBPyConnection) -> None:
    """``down()`` on a database without the index must not raise.

    Wrapped in ``contextlib.suppress(duckdb.Error)`` per the m7004 exemplar
    pattern; ``DROP INDEX IF EXISTS`` already handles the missing case.
    """
    m6002.down(conn)  # no m6001, no index — should be a safe no-op.


# ---------------------------------------------------------------------------
# Integration with the shared migration runner
# ---------------------------------------------------------------------------


def test_run_pending_applies_m6001_then_m6002(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """End-to-end: the shared runner discovers and applies migrations in order.

    The discovered set covers every ``m6###`` module currently under
    :mod:`razor_rooster.calibration_backtest.persistence.migrations`. m6001
    and m6002 must lead the order; later versions (e.g. m6003 freezer
    indexes from T-CB-014) are appended after them.
    """
    applied = run_pending_calibration_backtest_migrations(conn)
    versions = [m.version for m in applied]
    # m6001 and m6002 must appear in order at the head of the discovered set.
    assert versions[:2] == [schemas.VERSION_6001, schemas.VERSION_6002], (
        f"expected [6001, 6002, ...], got {versions}"
    )
    assert versions == sorted(versions), (
        f"migration discovery must be version-ordered, got {versions}"
    )
    # All discovered versions are recorded in schema_migrations.
    assert _applied_versions(conn) == versions
    # The m6002 index should exist after the runner finishes.
    assert m6002.INDEX_NAME in _existing_index_names(conn)


def test_run_pending_is_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    """Running the runner twice should apply nothing the second time."""
    first = run_pending_calibration_backtest_migrations(conn)
    second = run_pending_calibration_backtest_migrations(conn)
    # First call applies every discovered ``m6###`` migration; the count
    # tracks the number of modules in the migrations package and grows as
    # new migrations land (m6001, m6002, m6003, ...). The contract this
    # test guards is the *idempotency* property: a second call applies
    # zero migrations.
    assert len(first) >= 2
    assert {schemas.VERSION_6001, schemas.VERSION_6002}.issubset({m.version for m in first})
    assert len(second) == 0
