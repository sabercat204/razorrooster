"""Kalshi m8001 — initial schema (T-KSI-010 / T-KSI-011)."""

from __future__ import annotations

import duckdb

from razor_rooster.kalshi_connector.persistence.schemas import (
    ALL_KALSHI_DDL,
    KALSHI_INDEXES_DDL,
    KALSHI_TABLE_NAMES,
)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    for ddl in ALL_KALSHI_DDL:
        conn.execute(ddl)
    for index_ddl in KALSHI_INDEXES_DDL:
        conn.execute(index_ddl)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    for table in KALSHI_TABLE_NAMES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
