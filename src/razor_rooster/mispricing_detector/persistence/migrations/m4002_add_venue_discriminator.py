"""m4002 — add ``venue`` discriminator to mispricing tables (T-MD-101).

Cross-subsystem migration that lands as part of the Kalshi connector
work (Phase 1.5; design §3.10). Adds a ``venue`` column to the three
mispricing tables (``class_market_mappings``, ``comparisons``,
``comparison_resolutions``) so a single class can be mapped to both
Polymarket and Kalshi simultaneously.

The default value is ``'polymarket'``: existing rows migrate in place
with that value, new Kalshi rows insert with ``'kalshi'``. The
application-level uniqueness check on
``(class_id, condition_id, polarity)`` becomes
``(class_id, venue, condition_id, polarity)``; the index is dropped
and recreated to match (the new index includes ``venue`` as a leading
key after ``class_id``).

DuckDB quirks worked around here:

- ``ADD COLUMN ... NOT NULL DEFAULT ...`` is not supported in a single
  statement. The workaround is ADD with DEFAULT (which backfills) then
  ``ALTER COLUMN ... SET NOT NULL``.
- ``ALTER COLUMN ... SET NOT NULL`` fails when indexes reference the
  table. We drop the affected indexes first, alter, then recreate.
- Fresh installs hit the canonical DDL in m4001 (which already declares
  the column as NOT NULL DEFAULT). For those, this migration's ALTERs
  detect the existing NOT NULL state via PRAGMA and skip silently.
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
    """Add ``venue`` to the three mispricing tables; rebuild indexes."""
    # Detect the fresh-install path: m4001 just created tables with the
    # canonical DDL that already includes ``venue NOT NULL``. In that
    # case both ADD COLUMN (IF NOT EXISTS) and SET NOT NULL would be
    # no-ops, but SET NOT NULL also fails on the index dependency. Skip
    # the whole ALTER sequence per-table when the column is already
    # NOT NULL.
    tables = (
        "class_market_mappings",
        "comparisons",
        "comparison_resolutions",
    )
    needs_alter = {t for t in tables if not _column_is_not_null(conn, t, "venue")}

    if not needs_alter:
        # Fresh install: tables already have the column with the right
        # constraint. Just make sure the new venue-aware comparisons
        # index exists (the canonical DDL declares it, but check defensively).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comparisons_venue_class_computed "
            "ON comparisons (venue, class_id, computed_at)"
        )
        return

    # Upgrade path: drop affected indexes first.
    conn.execute("DROP INDEX IF EXISTS idx_class_market_mappings_active")
    conn.execute("DROP INDEX IF EXISTS idx_class_market_mappings_class")
    conn.execute("DROP INDEX IF EXISTS idx_class_market_mappings_market")
    conn.execute("DROP INDEX IF EXISTS idx_class_market_mappings_confidence")
    conn.execute("DROP INDEX IF EXISTS idx_comparisons_cycle_surfaced")
    conn.execute("DROP INDEX IF EXISTS idx_comparisons_class_computed")
    conn.execute("DROP INDEX IF EXISTS idx_comparisons_market_computed")
    conn.execute("DROP INDEX IF EXISTS idx_comparisons_venue_class_computed")
    conn.execute("DROP INDEX IF EXISTS idx_comparison_resolutions_market")
    conn.execute("DROP INDEX IF EXISTS idx_comparison_resolutions_ts")

    if "class_market_mappings" in needs_alter:
        if not _column_exists(conn, "class_market_mappings", "venue"):
            conn.execute(
                "ALTER TABLE class_market_mappings ADD COLUMN venue VARCHAR DEFAULT 'polymarket'"
            )
        conn.execute("UPDATE class_market_mappings SET venue = 'polymarket' WHERE venue IS NULL")
        conn.execute("ALTER TABLE class_market_mappings ALTER COLUMN venue SET NOT NULL")

    if "comparisons" in needs_alter:
        if not _column_exists(conn, "comparisons", "venue"):
            conn.execute("ALTER TABLE comparisons ADD COLUMN venue VARCHAR DEFAULT 'polymarket'")
        conn.execute("UPDATE comparisons SET venue = 'polymarket' WHERE venue IS NULL")
        conn.execute("ALTER TABLE comparisons ALTER COLUMN venue SET NOT NULL")

    if "comparison_resolutions" in needs_alter:
        if not _column_exists(conn, "comparison_resolutions", "venue"):
            conn.execute(
                "ALTER TABLE comparison_resolutions ADD COLUMN venue VARCHAR DEFAULT 'polymarket'"
            )
        conn.execute("UPDATE comparison_resolutions SET venue = 'polymarket' WHERE venue IS NULL")
        conn.execute("ALTER TABLE comparison_resolutions ALTER COLUMN venue SET NOT NULL")

    # Recreate indexes including the new venue-aware shapes.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_class_market_mappings_active "
        "ON class_market_mappings (class_id, venue, condition_id, polarity, removed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_class_market_mappings_class "
        "ON class_market_mappings (class_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_class_market_mappings_market "
        "ON class_market_mappings (condition_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_class_market_mappings_confidence "
        "ON class_market_mappings (mapping_confidence)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_comparisons_cycle_surfaced "
        "ON comparisons (cycle_id, surfaced)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_comparisons_class_computed "
        "ON comparisons (class_id, computed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_comparisons_market_computed "
        "ON comparisons (condition_id, computed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_comparisons_venue_class_computed "
        "ON comparisons (venue, class_id, computed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_comparison_resolutions_market "
        "ON comparison_resolutions (condition_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_comparison_resolutions_ts "
        "ON comparison_resolutions (resolution_ts)"
    )


def down(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop the venue columns and the new index. The pre-Kalshi index is
    not restored — operators wanting a full rollback should re-run
    m4001's index creation manually."""
    conn.execute("DROP INDEX IF EXISTS idx_class_market_mappings_active")
    conn.execute("DROP INDEX IF EXISTS idx_comparisons_venue_class_computed")
    conn.execute("ALTER TABLE comparison_resolutions DROP COLUMN IF EXISTS venue")
    conn.execute("ALTER TABLE comparisons DROP COLUMN IF EXISTS venue")
    conn.execute("ALTER TABLE class_market_mappings DROP COLUMN IF EXISTS venue")
