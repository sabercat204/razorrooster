"""Calibration-backtest m6001 — initial schema (T-CB-009).

Creates the three calibration-backtest tables defined in design §3.3:

* ``backtest_runs`` — one row per backtest invocation; idempotent insert /
  cached-summary contract per REQ-CB-RUN-004 and append-only per
  REQ-CB-PERSIST-001.
* ``backtest_predictions`` — one row per scored or skipped prediction inside
  a run; composite primary key ``(run_id, prediction_id)``. The
  ``polarity_source`` and ``mapping_mismatch_warning`` columns are part of
  the canonical DDL here so fresh installs and re-runs of m6002 see the
  schema in a single state (m6002 is intentionally idempotent / no-op v1).
* ``backtest_traces`` — per-prediction zstd-compressed trace BLOBs per
  REQ-CB-PERSIST-002 / D4. Stored separately from
  ``backtest_predictions`` so summary queries do not pessimise on the
  blob payload.

The DDL strings live in
:mod:`razor_rooster.calibration_backtest.persistence.schemas` so the same
canonical text is reused by tests, the schema-validation helper, and the
migration runner.

Idempotency: every DDL statement uses ``CREATE TABLE IF NOT EXISTS`` /
``CREATE INDEX IF NOT EXISTS`` so re-running ``up()`` on a fresh install or
on an already-migrated database is a safe no-op. The shared migration
runner (``data_ingest.persistence.migrations.run_pending_migrations``)
also guards via the ``schema_migrations`` registry table so this
``up()`` only fires once per logical version per connection.
"""

from __future__ import annotations

from typing import Final

import duckdb

from razor_rooster.calibration_backtest.persistence.schemas import (
    ALL_DDL,
    ALL_TABLES,
    INDEXES,
    VERSION_6001,
)

MIGRATION_ID: Final[int] = VERSION_6001
"""Version number recorded in the shared ``schema_migrations`` table when
this migration runs successfully (design §3.3, OQ-CB-002)."""

DESCRIPTION: Final[str] = (
    "Create calibration_backtest tables (backtest_runs, backtest_predictions, backtest_traces)"
)
"""Human-readable summary surfaced by ``schema_migrations.description``."""


def up(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply the calibration-backtest initial schema.

    Tables are created first, then their indexes; this ordering matters
    because ``CREATE INDEX`` requires the target table to already exist.
    """
    for ddl in ALL_DDL:
        conn.execute(ddl)
    for index_ddl in INDEXES:
        conn.execute(index_ddl)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop everything ``up()`` created.

    Tables are dropped in reverse order of creation so any (future) foreign
    keys are honoured. ``IF EXISTS`` makes the rollback forgiving when only
    a partial state is on disk (e.g. a previous failed migration). Indexes
    are removed implicitly when their parent table is dropped.
    """
    for table in reversed(ALL_TABLES):
        conn.execute(f"DROP TABLE IF EXISTS {table}")


__all__ = [
    "DESCRIPTION",
    "MIGRATION_ID",
    "down",
    "up",
]
