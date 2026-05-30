"""Position-engine m5001 — initial schema (T-PE-010)."""

from __future__ import annotations

import duckdb

from razor_rooster.position_engine.persistence.schemas import (
    ALL_POSITION_ENGINE_DDL,
    POSITION_ENGINE_INDEXES_DDL,
    POSITION_ENGINE_TABLE_NAMES,
)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    for ddl in ALL_POSITION_ENGINE_DDL:
        conn.execute(ddl)
    for index_ddl in POSITION_ENGINE_INDEXES_DDL:
        conn.execute(index_ddl)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    for table in POSITION_ENGINE_TABLE_NAMES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
