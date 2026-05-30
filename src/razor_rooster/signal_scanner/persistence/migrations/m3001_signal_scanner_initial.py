"""Signal-scanner m3001 — initial schema (T-SCAN-010)."""

from __future__ import annotations

import duckdb

from razor_rooster.signal_scanner.persistence.schemas import (
    ALL_SCAN_DDL,
    SCAN_INDEXES_DDL,
    SCAN_TABLE_NAMES,
)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    for ddl in ALL_SCAN_DDL:
        conn.execute(ddl)
    for index_ddl in SCAN_INDEXES_DDL:
        conn.execute(index_ddl)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    for table in SCAN_TABLE_NAMES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
