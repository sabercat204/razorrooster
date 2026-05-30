"""Mispricing-detector m4001 — initial schema (T-MD-010)."""

from __future__ import annotations

import duckdb

from razor_rooster.mispricing_detector.persistence.schemas import (
    ALL_MISPRICING_DDL,
    MISPRICING_INDEXES_DDL,
    MISPRICING_TABLE_NAMES,
)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    for ddl in ALL_MISPRICING_DDL:
        conn.execute(ddl)
    for index_ddl in MISPRICING_INDEXES_DDL:
        conn.execute(index_ddl)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    for table in MISPRICING_TABLE_NAMES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
