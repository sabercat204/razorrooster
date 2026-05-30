"""Report-generator m7001 — initial schema (T-RG-010)."""

from __future__ import annotations

import duckdb

from razor_rooster.report_generator.persistence.schemas import (
    ALL_REPORT_GENERATOR_DDL,
    REPORT_GENERATOR_INDEXES_DDL,
    REPORT_GENERATOR_TABLE_NAMES,
)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    for ddl in ALL_REPORT_GENERATOR_DDL:
        conn.execute(ddl)
    for index_ddl in REPORT_GENERATOR_INDEXES_DDL:
        conn.execute(index_ddl)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    for table in REPORT_GENERATOR_TABLE_NAMES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
