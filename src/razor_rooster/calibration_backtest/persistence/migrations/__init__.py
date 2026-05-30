"""Calibration-backtest DuckDB migrations (T-CB-001, T-CB-009).

Reuses the shared migration runner exposed by
:mod:`razor_rooster.data_ingest.persistence.migrations`. Discovery is
auto-magical: any module in this package whose filename matches
``m####_<description>.py`` and exposes ``up(conn)`` / ``down(conn)`` is
picked up by :func:`razor_rooster.data_ingest.persistence.migrations.run_pending_migrations`
when called with ``package_name=__name__``.

Migration version numbers live in the 6001+ range per design §3.3, clear
of data_ingest (0001-0999), polymarket_connector (1001-1999),
pattern_library (2001-2999), signal_scanner (3001-3999),
mispricing_detector (4001-4999), and position_engine (5001-5999).
"""

from __future__ import annotations

import duckdb

from razor_rooster.data_ingest.persistence.migrations import (
    Migration,
    run_pending_migrations,
)


def run_pending_calibration_backtest_migrations(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[Migration, ...]:
    """Apply any calibration-backtest migrations not yet on this connection."""
    return run_pending_migrations(conn, package_name=__name__)


__all__ = [
    "run_pending_calibration_backtest_migrations",
]
