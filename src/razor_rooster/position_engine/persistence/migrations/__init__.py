"""Position-engine namespace migrations (T-PE-010).

Reuses the data_ingest migrations runner via ``package_name``.
position_engine migrations use versions >= 5001 to stay clear of
data_ingest (1-999), polymarket_connector (1001-1999),
pattern_library (2001-2999), signal_scanner (3001-3999), and
mispricing_detector (4001-4999) ranges.
"""

from __future__ import annotations

import duckdb

from razor_rooster.data_ingest.persistence.migrations import (
    Migration,
    run_pending_migrations,
)


def run_pending_position_engine_migrations(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[Migration, ...]:
    """Apply any position_engine migrations not yet on this connection."""
    return run_pending_migrations(conn, package_name=__name__)
