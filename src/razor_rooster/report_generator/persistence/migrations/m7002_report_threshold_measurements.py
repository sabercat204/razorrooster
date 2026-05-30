"""Report-generator m7002 — add ``report_threshold_measurements`` table.

T-RG-COMPAT-MEAS-001 (LOOM v0.40.0). One row per (report_id,
measurement_kind) recording the distribution of the underlying
signal at cycle time so operators can see whether their
configured threshold is well-calibrated for the corpus.

Idempotent — runs on top of m7001 cleanly because the table
itself uses ``CREATE TABLE IF NOT EXISTS`` and the index uses
``CREATE INDEX IF NOT EXISTS``.
"""

from __future__ import annotations

import duckdb

from razor_rooster.report_generator.persistence.schemas import (
    REPORT_GENERATOR_INDEXES_DDL,
    REPORT_THRESHOLD_MEASUREMENTS_DDL,
)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(REPORT_THRESHOLD_MEASUREMENTS_DDL)
    # Re-running every index DDL is safe via IF NOT EXISTS; this
    # picks up the new measurement-kind index without re-running the
    # report_log index logic in the m7001 path.
    for ddl in REPORT_GENERATOR_INDEXES_DDL:
        conn.execute(ddl)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("DROP TABLE IF EXISTS report_threshold_measurements")
