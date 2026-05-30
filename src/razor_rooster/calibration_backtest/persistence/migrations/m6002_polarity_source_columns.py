"""Calibration-backtest m6002 — polarity-source columns + supporting index.

T-CB-009 (Phase 2 persistence). Per design §3.3 / OQ-CB-005 the
``backtest_predictions`` table carries two polarity-related columns:

* ``polarity_source`` (``'comparison_resolutions' | 'current_mapping_fallback'``) —
  records which path resolved the prediction's polarity so the run summary
  can quantify how much of the score depended on the current-mapping
  fallback (OQ-CB-002, REQ-CB-REPLAY-003, P-CB-3).
* ``mapping_mismatch_warning`` (boolean) — flagged when a prediction had to
  fall back to the current ``class_market_mappings`` row because no
  matching ``comparison_resolutions.polarity_at_comparison`` was available
  (OQ-CB-005 step 2).

In v1 these columns are already part of the canonical m6001 DDL — fresh
installs apply m6001 + m6002 and the columns exist after the first
``CREATE TABLE IF NOT EXISTS``. This migration is therefore intentionally
shaped as an *idempotent forward-compat* upgrade so that:

1. The schema-upgrade path is exercised end-to-end (T-CB-009 verification).
2. Any future database that pre-dates the in-line m6001 column additions
   (e.g. a hypothetical hand-rolled schema migrated from an earlier prototype)
   gains the columns automatically.
3. We can add the supporting index ``idx_backtest_predictions_polarity_source``
   on ``(run_id, polarity_source)`` here without bloating m6001 — the
   index supports the run-summary aggregation
   ``SELECT polarity_source, COUNT(*) FROM backtest_predictions
    WHERE run_id = ? GROUP BY 1`` used to compute
   ``backtest_runs.fallback_polarity_count`` (design §3.3).

The migration uses ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` (the same
pattern as ``data_ingest`` m0002) so re-running on a fresh install is a
safe no-op: the columns already exist from m6001, and the ``IF NOT EXISTS``
guard short-circuits without raising. ``CREATE INDEX IF NOT EXISTS`` makes
the index step idempotent in the same fashion.

Note on ``polarity_source`` CHECK constraint: DuckDB does not support
``ALTER TABLE ... ADD CONSTRAINT`` for inline CHECKs after the fact, so
the closed-enumeration check lives entirely in the m6001 DDL. The
application layer (``persistence.operations``) and the
``models.PolaritySource`` enum guard the column at write time.
"""

from __future__ import annotations

import contextlib
from typing import Final

import duckdb

from razor_rooster.calibration_backtest.persistence.schemas import (
    TABLE_PREDICTIONS,
    VERSION_6002,
)

MIGRATION_ID: Final[int] = VERSION_6002
"""Version number recorded in the shared ``schema_migrations`` table when
this migration runs successfully (design §3.3, OQ-CB-005)."""

DESCRIPTION: Final[str] = (
    "Ensure backtest_predictions polarity_source / mapping_mismatch_warning "
    "columns exist and add (run_id, polarity_source) index"
)
"""Human-readable summary surfaced by ``schema_migrations.description``."""

INDEX_NAME: Final[str] = "idx_backtest_predictions_polarity_source"
"""Index added by this migration. Exposed for tests / ops introspection."""

_INDEX_DDL: Final[str] = (
    f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON {TABLE_PREDICTIONS} (run_id, polarity_source)"
)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    """Add (or no-op) the polarity-source columns and supporting index.

    Order matters: the columns must exist before the index can reference
    ``polarity_source``. Both column adds use ``IF NOT EXISTS`` so this is
    a clean no-op on fresh installs where m6001 already created them.
    """
    conn.execute(
        f"ALTER TABLE {TABLE_PREDICTIONS} ADD COLUMN IF NOT EXISTS polarity_source VARCHAR"
    )
    conn.execute(
        f"ALTER TABLE {TABLE_PREDICTIONS} "
        f"ADD COLUMN IF NOT EXISTS mapping_mismatch_warning BOOLEAN DEFAULT FALSE"
    )
    conn.execute(_INDEX_DDL)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop the supporting index added by ``up()``.

    Columns are intentionally NOT dropped: they are part of the canonical
    m6001 schema (design §3.3) and removing them would leave the table in
    a state the application code can no longer write to. To remove the
    columns entirely, roll back m6001. ``contextlib.suppress`` mirrors the
    ``data_ingest`` exemplar (m7004) for forgiving rollback on partial
    state — DuckDB's ``DROP INDEX IF EXISTS`` already handles the missing
    case, but defensively wrapping keeps parity with the project's
    rollback idiom.
    """
    with contextlib.suppress(duckdb.Error):
        conn.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")


__all__ = [
    "DESCRIPTION",
    "INDEX_NAME",
    "MIGRATION_ID",
    "down",
    "up",
]
