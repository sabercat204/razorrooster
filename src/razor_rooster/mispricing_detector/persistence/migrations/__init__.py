"""Mispricing-detector namespace migrations (T-MD-010).

Discovery and application reuse the data_ingest migrations runner via
its ``package_name`` parameter. The shared ``schema_migrations`` table
is the same; mispricing_detector migrations use versions >= 4001 to
stay clear of data_ingest (``0001..0999``), polymarket_connector
(``1001..1999``), pattern_library (``2001..2999``), and signal_scanner
(``3001..3999``) ranges.
"""

from __future__ import annotations

import duckdb

from razor_rooster.data_ingest.persistence.migrations import (
    Migration,
    run_pending_migrations,
)


def run_pending_mispricing_migrations(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[Migration, ...]:
    """Apply any mispricing_detector migrations not yet on this connection."""
    return run_pending_migrations(conn, package_name=__name__)
