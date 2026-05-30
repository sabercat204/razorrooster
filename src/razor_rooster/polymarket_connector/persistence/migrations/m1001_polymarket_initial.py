"""Polymarket m1001 — initial schema (T-PMC-011).

Applies all seven Polymarket-namespaced tables plus their indexes. Version
1001 keeps Polymarket migrations out of the data_ingest 0001..0999 range
in the shared ``schema_migrations`` table.
"""

from __future__ import annotations

import duckdb

from razor_rooster.polymarket_connector.persistence.schemas import (
    ALL_POLYMARKET_DDL,
    POLYMARKET_INDEXES_DDL,
    POLYMARKET_TABLE_NAMES,
)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    """Create all Polymarket-namespace tables and indexes."""
    for ddl in ALL_POLYMARKET_DDL:
        conn.execute(ddl)
    for index_ddl in POLYMARKET_INDEXES_DDL:
        conn.execute(index_ddl)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop all Polymarket-namespace tables.

    Indexes are dropped automatically by DuckDB when their backing table
    is dropped. Operators run this only via an explicit CLI rollback —
    never auto-runs.
    """
    for table in POLYMARKET_TABLE_NAMES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
