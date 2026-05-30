"""Report-generator m7003 — add ``threshold_tuning_log`` table.

T-RG-COMPAT-TUNINGLOG-001 (LOOM v0.43.0). One row per
successful ``razor-rooster report suggest-thresholds --apply``
write. Captures the applied_at timestamp, kind, knob, old/new
values, target percentile, optional operator note, and the
backup file path so retroactive review is possible.

Idempotent — runs on top of m7002 cleanly because the table
itself uses ``CREATE TABLE IF NOT EXISTS`` and the index uses
``CREATE INDEX IF NOT EXISTS``.
"""

from __future__ import annotations

import duckdb

from razor_rooster.report_generator.persistence.schemas import (
    REPORT_GENERATOR_INDEXES_DDL,
    THRESHOLD_TUNING_LOG_DDL,
)


def up(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(THRESHOLD_TUNING_LOG_DDL)
    # Re-running every index DDL is safe via IF NOT EXISTS; this
    # picks up the new tuning-log index.
    for ddl in REPORT_GENERATOR_INDEXES_DDL:
        conn.execute(ddl)


def down(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("DROP TABLE IF EXISTS threshold_tuning_log")
