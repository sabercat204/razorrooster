"""Pattern-library m2001 — initial schema (T-PL-011)."""

from __future__ import annotations

import duckdb

from razor_rooster.pattern_library.persistence.schemas import (
    ALL_PL_DDL,
    PL_INDEXES_DDL,
    PL_TABLE_NAMES,
)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    for ddl in ALL_PL_DDL:
        conn.execute(ddl)
    for index_ddl in PL_INDEXES_DDL:
        conn.execute(index_ddl)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    for table in PL_TABLE_NAMES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
