"""m6002 — add ``venue`` discriminator to ``follow_ups`` (T-MON-101).

Cross-subsystem migration that lands as part of the Kalshi connector
work (Phase 1.5; design §3.10). Adds a ``venue`` column to the
``follow_ups`` table so monitor follow-ups copied from Kalshi-venue
analyses remain traceable to the right venue end-to-end.

The default value is ``'polymarket'``: existing rows migrate in place
with that value, new Kalshi rows insert with ``'kalshi'``.

DuckDB quirks applied (see m4002 / m5002 for the same pattern):

- ``ADD COLUMN ... NOT NULL DEFAULT ...`` is not supported in a single
  statement. The workaround is ADD with DEFAULT (which backfills) then
  ``ALTER COLUMN ... SET NOT NULL``.
- ``ALTER COLUMN ... SET NOT NULL`` fails when indexes reference the
  table. We drop the affected indexes first, alter, then recreate.
- Fresh installs hit the canonical DDL in m6001 once it has been
  updated to declare ``venue NOT NULL``. For those, this migration's
  ALTERs detect the existing NOT NULL state via PRAGMA and skip
  silently.
"""

from __future__ import annotations

import duckdb


def _column_is_not_null(conn: duckdb.DuckDBPyConnection, table: str, column: str) -> bool:
    """Return True when the named column exists and is declared NOT NULL.

    PRAGMA table_info returns ``(cid, name, type, notnull, dflt, pk)``;
    the fourth field is the boolean we need.
    """
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    for row in rows:
        if row[1] == column:
            return bool(row[3])
    return False


def _column_exists(conn: duckdb.DuckDBPyConnection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    return any(row[1] == column for row in rows)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    """Add ``venue`` to the follow_ups table; rebuild indexes."""
    if _column_is_not_null(conn, "follow_ups", "venue"):
        # Fresh install: nothing to do — the canonical DDL in m6001
        # already declares the column NOT NULL.
        return

    # Upgrade path: drop affected indexes first.
    conn.execute("DROP INDEX IF EXISTS idx_follow_ups_analysis_computed")
    conn.execute("DROP INDEX IF EXISTS idx_follow_ups_cycle_alert")
    conn.execute("DROP INDEX IF EXISTS idx_follow_ups_recommended")

    if not _column_exists(conn, "follow_ups", "venue"):
        conn.execute("ALTER TABLE follow_ups ADD COLUMN venue VARCHAR DEFAULT 'polymarket'")
    conn.execute("UPDATE follow_ups SET venue = 'polymarket' WHERE venue IS NULL")
    conn.execute("ALTER TABLE follow_ups ALTER COLUMN venue SET NOT NULL")

    # Recreate indexes.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_follow_ups_analysis_computed "
        "ON follow_ups (analysis_id, computed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_follow_ups_cycle_alert "
        "ON follow_ups (cycle_id, primary_alert_tier)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_follow_ups_recommended "
        "ON follow_ups (recommended_review, computed_at)"
    )


def down(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop the venue column. The pre-Kalshi indexes are not restored —
    operators wanting a full rollback should re-run m6001's index
    creation manually."""
    conn.execute("ALTER TABLE follow_ups DROP COLUMN IF EXISTS venue")
