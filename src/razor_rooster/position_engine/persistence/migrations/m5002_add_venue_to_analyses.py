"""m5002 — add ``venue`` discriminator to ``analyses`` (T-PE-101).

Cross-subsystem migration that lands as part of the Kalshi connector
work (Phase 1.5; design §3.10). Adds a ``venue`` column to the
``analyses`` table so analyses copied from Kalshi-venue comparisons
remain traceable to the right venue end-to-end.

The default value is ``'polymarket'``: existing rows migrate in place
with that value, new Kalshi rows insert with ``'kalshi'``. A new
``idx_analyses_venue_computed`` covers the report-generator's
"latest analyses by venue" access pattern.

DuckDB quirks applied (see m4002 for the same pattern):

- ``ADD COLUMN ... NOT NULL DEFAULT ...`` is not supported in a single
  statement. The workaround is ADD with DEFAULT (which backfills) then
  ``ALTER COLUMN ... SET NOT NULL``.
- ``ALTER COLUMN ... SET NOT NULL`` fails when indexes reference the
  table. We drop the affected indexes first, alter, then recreate.
- Fresh installs hit the canonical DDL in m5001 once it has been
  updated to declare ``venue NOT NULL``. For those, this migration's
  ALTERs detect the existing NOT NULL state via PRAGMA and skip
  silently, only ensuring the new venue-aware index exists.
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
    """Add ``venue`` to the analyses table; rebuild indexes."""
    # Detect the fresh-install path: m5001 (after this PR) creates the
    # table with the canonical DDL that already includes
    # ``venue NOT NULL``. In that case both ADD COLUMN (IF NOT EXISTS)
    # and SET NOT NULL would be no-ops, but SET NOT NULL also fails on
    # the index dependency. Skip the whole ALTER sequence when the
    # column is already NOT NULL.
    if _column_is_not_null(conn, "analyses", "venue"):
        # Fresh install: table already has the column with the right
        # constraint. Just make sure the new venue-aware index exists.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analyses_venue_computed "
            "ON analyses (venue, computed_at)"
        )
        return

    # Upgrade path: drop affected indexes first.
    conn.execute("DROP INDEX IF EXISTS idx_analyses_cycle")
    conn.execute("DROP INDEX IF EXISTS idx_analyses_comparison")
    conn.execute("DROP INDEX IF EXISTS idx_analyses_class_computed")
    conn.execute("DROP INDEX IF EXISTS idx_analyses_venue_computed")

    if not _column_exists(conn, "analyses", "venue"):
        conn.execute("ALTER TABLE analyses ADD COLUMN venue VARCHAR DEFAULT 'polymarket'")
    conn.execute("UPDATE analyses SET venue = 'polymarket' WHERE venue IS NULL")
    conn.execute("ALTER TABLE analyses ALTER COLUMN venue SET NOT NULL")

    # Recreate indexes including the new venue-aware one.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analyses_cycle ON analyses (cycle_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analyses_comparison ON analyses (comparison_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analyses_class_computed ON analyses (class_id, computed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analyses_venue_computed ON analyses (venue, computed_at)"
    )


def down(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop the venue column and the new index. The pre-Kalshi indexes
    are not restored — operators wanting a full rollback should re-run
    m5001's index creation manually."""
    conn.execute("DROP INDEX IF EXISTS idx_analyses_venue_computed")
    conn.execute("ALTER TABLE analyses DROP COLUMN IF EXISTS venue")
