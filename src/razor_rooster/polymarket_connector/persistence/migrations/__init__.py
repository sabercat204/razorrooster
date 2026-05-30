"""Polymarket-namespace migrations (T-PMC-011).

Discovery and application reuse the ``data_ingest`` migrations runner via
its ``package_name`` parameter. The ``schema_migrations`` table is shared
across packages; Polymarket migrations use versions ≥ 1001 to stay clear
of the data_ingest ``0001..0999`` range.

Operators apply Polymarket migrations by calling
:func:`run_pending_polymarket_migrations` after the data_ingest
migrations run on store open. Both runners are idempotent.
"""

from __future__ import annotations

import duckdb

from razor_rooster.data_ingest.persistence.migrations import (
    Migration,
    run_pending_migrations,
)


def run_pending_polymarket_migrations(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[Migration, ...]:
    """Apply any Polymarket-namespace migrations not yet on this connection.

    Idempotent: a no-op if the store is already at the latest version.
    """
    return run_pending_migrations(
        conn,
        package_name=__name__,
    )
