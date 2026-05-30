"""Report-generator namespace migrations (T-RG-010).

Reuses the data_ingest migrations runner. report_generator
migrations use versions >= 7001 to stay clear of data_ingest
(1-999), polymarket_connector (1001-1999), pattern_library
(2001-2999), signal_scanner (3001-3999), mispricing_detector
(4001-4999), position_engine (5001-5999), and monitor (6001-6999)
ranges.
"""

from __future__ import annotations

import duckdb

from razor_rooster.data_ingest.persistence.migrations import (
    Migration,
    run_pending_migrations,
)


def run_pending_report_generator_migrations(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[Migration, ...]:
    """Apply any report_generator migrations not yet on this connection."""
    return run_pending_migrations(conn, package_name=__name__)
