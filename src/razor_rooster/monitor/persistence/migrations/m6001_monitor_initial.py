"""Monitor m6001 — initial schema (T-MON-010)."""

from __future__ import annotations

import duckdb

from razor_rooster.monitor.persistence.schemas import (
    ALL_MONITOR_DDL,
    MONITOR_INDEXES_DDL,
    MONITOR_TABLE_NAMES,
)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    for ddl in ALL_MONITOR_DDL:
        conn.execute(ddl)
    for index_ddl in MONITOR_INDEXES_DDL:
        conn.execute(index_ddl)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    for table in MONITOR_TABLE_NAMES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
