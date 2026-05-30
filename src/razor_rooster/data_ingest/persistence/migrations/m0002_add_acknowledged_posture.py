"""m0002 — add ``sources.acknowledged_posture`` column (T-DI-101).

The Kalshi connector's ToS gate (T-KSI-021) records whether the
operator acknowledged the Terms under a ``read_only`` (v1) or
``trading`` (v2+) posture. The Polymarket connector's existing
acknowledgement is migrated to ``read_only`` so the column is
uniformly populated for already-acknowledged sources.

DuckDB's ``ADD COLUMN IF NOT EXISTS`` keeps this migration idempotent
even on fresh installs where ``m0001`` already includes the column
in the canonical DDL.
"""

from __future__ import annotations

import duckdb


def up(conn: duckdb.DuckDBPyConnection) -> None:
    """Add the ``acknowledged_posture`` column and backfill Polymarket rows."""
    conn.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS acknowledged_posture VARCHAR NULL")
    # Backfill: any Polymarket source row that already carries an
    # acknowledgement (license_acknowledged_at IS NOT NULL) gets
    # tagged 'read_only' since v1 Polymarket is read-only by design.
    conn.execute(
        "UPDATE sources "
        "SET acknowledged_posture = 'read_only' "
        "WHERE source_id LIKE 'polymarket%' "
        "  AND license_acknowledged_at IS NOT NULL "
        "  AND acknowledged_posture IS NULL"
    )


def down(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop the column. ``IF EXISTS`` for partial-state rollback."""
    conn.execute("ALTER TABLE sources DROP COLUMN IF EXISTS acknowledged_posture")
