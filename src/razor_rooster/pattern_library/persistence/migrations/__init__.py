"""Pattern-library namespace migrations (T-PL-011).

Discovery and application reuse the data_ingest migrations runner via
its ``package_name`` parameter. The shared ``schema_migrations`` table
is the same; pattern_library migrations use versions ≥ 2001 to stay
clear of data_ingest (``0001..0999``) and polymarket_connector
(``1001..1999``) ranges.
"""

from __future__ import annotations

import duckdb

from razor_rooster.data_ingest.persistence.migrations import (
    Migration,
    run_pending_migrations,
)


def run_pending_pattern_library_migrations(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[Migration, ...]:
    """Apply any pattern-library migrations not yet on this connection."""
    return run_pending_migrations(conn, package_name=__name__)
