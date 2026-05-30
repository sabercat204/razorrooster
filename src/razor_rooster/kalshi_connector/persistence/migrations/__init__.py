"""Kalshi namespace migrations (T-KSI-010 / T-KSI-011).

Reuses the data_ingest migrations runner. Kalshi migrations use
versions >= 8001 to stay clear of data_ingest (1-999),
polymarket_connector (1001-1999), pattern_library (2001-2999),
signal_scanner (3001-3999), mispricing_detector (4001-4999),
position_engine (5001-5999), monitor (6001-6999), and
report_generator (7001-7999) ranges.
"""

from __future__ import annotations

import duckdb

from razor_rooster.data_ingest.persistence.migrations import (
    Migration,
    run_pending_migrations,
)


def run_pending_kalshi_migrations(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[Migration, ...]:
    """Apply any kalshi migrations not yet on this connection.

    T-KSI-001 ships an empty migrations directory; T-KSI-011 adds
    ``m8001_kalshi_initial.py`` with the actual DDL.
    """
    return run_pending_migrations(conn, package_name=__name__)
