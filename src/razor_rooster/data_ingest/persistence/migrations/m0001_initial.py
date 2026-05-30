"""Initial migration — canonical schemas + operational tables (T-013).

Applies the four canonical schemas from T-010 and the operational tables and
freshness view from T-011.

The ``schema_migrations`` table itself is created by the migration runner
(via ``_ensure_schema_migrations_table``) before this migration runs, so we
do not re-create it here.
"""

from __future__ import annotations

import duckdb

from razor_rooster.data_ingest.persistence.operational_schemas import all_operational_ddl
from razor_rooster.data_ingest.persistence.schemas import (
    SchemaType,
    canonical_indexes_ddl,
    canonical_table_ddl,
)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply the canonical and operational schemas."""
    # Canonical schemas (event_stream, time_series, document_docket,
    # geospatial_indicator) plus their indexes.
    for schema in SchemaType:
        conn.execute(canonical_table_ddl(schema))
        for stmt in canonical_indexes_ddl(schema):
            conn.execute(stmt)

    # Operational tables and freshness view.
    for stmt in all_operational_ddl():
        conn.execute(stmt)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop everything this migration created.

    Operational view first (so the table it depends on can be dropped), then
    operational tables, then canonical schemas. ``IF EXISTS`` so the rollback
    is forgiving when partial state is encountered.
    """
    conn.execute("DROP VIEW IF EXISTS freshness")
    conn.execute("DROP TABLE IF EXISTS cycle_log")
    conn.execute("DROP TABLE IF EXISTS ingest_anomalies")
    conn.execute("DROP TABLE IF EXISTS backfill_state")
    conn.execute("DROP TABLE IF EXISTS sources")
    # NOTE: schema_migrations is the runner's table; we leave it for the runner
    # to manage (this migration's row is removed by the runner on rollback).
    for schema in SchemaType:
        conn.execute(f"DROP TABLE IF EXISTS {schema.value}")
