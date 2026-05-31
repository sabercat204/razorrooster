"""Calibration-backtest namespaced table DDL (T-CB-007; design §3.3, §3.13).

This module exposes the DuckDB DDL constants for the three calibration-backtest
tables — ``backtest_runs``, ``backtest_predictions``, ``backtest_traces`` — as
``Final`` strings, plus the small helper accessors :func:`list_tables` and
:func:`list_ddl`. It is intentionally **DDL-only**: the migration runner,
ordering, version tracking, and idempotency semantics live in the migration
modules ``m6001`` / ``m6002`` (T-CB-009) and the operations module (T-CB-010).
Schema-version-tracking rows recorded by the shared migration runner are
namespaced under :data:`SCHEMA_NAMESPACE`.

All timestamp columns use ``TIMESTAMPTZ`` per data_ingest REQ-NORM-002, mirroring
the convention used by the other razorrooster subsystems. Closed enumerations
(``status``, ``skip_reason``, ``polarity_source``, ``polarity``,
``compression_algorithm``) are enforced via inline ``CHECK`` constraints so the
database rejects malformed rows even when application-level validation is
bypassed (REQ-CB-PERSIST-001, design §3.13).
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Namespace + table-name constants
# ---------------------------------------------------------------------------

SCHEMA_NAMESPACE: Final[str] = "calibration_backtest"
"""Namespace recorded in any schema-version-tracking row owned by this
subsystem. Migrations live in the 6001+ range (clear of data_ingest,
polymarket_connector, pattern_library, signal_scanner, mispricing_detector,
position_engine, monitor, report_generator)."""

TABLE_RUNS: Final[str] = "backtest_runs"
TABLE_PREDICTIONS: Final[str] = "backtest_predictions"
TABLE_TRACES: Final[str] = "backtest_traces"

ALL_TABLES: Final[tuple[str, ...]] = (TABLE_RUNS, TABLE_PREDICTIONS, TABLE_TRACES)


# ---------------------------------------------------------------------------
# Migration version helpers
# ---------------------------------------------------------------------------

VERSION_6001: Final[int] = 6001
"""Schema version for ``m6001_calibration_backtest_initial`` — creates
``backtest_runs``, ``backtest_predictions``, ``backtest_traces`` with their
base columns, indexes, and constraints (design §3.3)."""

VERSION_6002: Final[int] = 6002
"""Schema version for ``m6002_polarity_source_columns`` — ensures
``polarity_source`` and ``mapping_mismatch_warning`` exist on
``backtest_predictions`` (design §3.3, OQ-CB-005). For v1 these columns are
already part of the m6001 DDL, so m6002 is a no-op idempotent upgrade."""

VERSION_6003: Final[int] = 6003
"""Schema version for ``m6003_freezer_indexes`` — adds
``(source_publication_ts DESC, source_id)`` indexes on the four canonical
``data_ingest`` tables (``time_series``, ``event_stream``,
``document_docket``, ``geospatial_indicator``) so the calibration-backtest
freezer (T-CB-014, design §3.5) stays under the design §6 latency budget on
multi-million-row corpora."""


# ---------------------------------------------------------------------------
# DDL — backtest_runs (design §3.3, table 1)
# ---------------------------------------------------------------------------

DDL_RUNS: Final[str] = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id                          VARCHAR     NOT NULL PRIMARY KEY,
    since_ts                        TIMESTAMPTZ NOT NULL,
    until_ts                        TIMESTAMPTZ NOT NULL,
    lag_days                        INTEGER     NOT NULL,
    class_ids_json                  JSON        NOT NULL,
    sectors_json                    JSON        NOT NULL,
    venues_json                     JSON        NOT NULL,
    library_version                 INTEGER     NOT NULL,
    system_revision                 VARCHAR     NOT NULL,
    started_at                      TIMESTAMPTZ NOT NULL,
    completed_at                    TIMESTAMPTZ NULL,
    status                          VARCHAR     NOT NULL
        CHECK (status IN ('in_progress', 'complete', 'failed')),
    error_summary                   TEXT        NULL,
    predictions_total               INTEGER     NOT NULL DEFAULT 0,
    predictions_scored              INTEGER     NOT NULL DEFAULT 0,
    predictions_skipped             INTEGER     NOT NULL DEFAULT 0,
    overall_brier                   DOUBLE      NULL,
    summary_json                    JSON        NULL,
    bin_count_global                INTEGER     NOT NULL,
    bin_count_per_sector_json       JSON        NOT NULL,
    fallback_polarity_count         INTEGER     NOT NULL DEFAULT 0,
    allow_recent                    BOOLEAN     NOT NULL DEFAULT FALSE,
    disclaimer_version              VARCHAR     NOT NULL
)
""".strip()


# ---------------------------------------------------------------------------
# DDL — backtest_predictions (design §3.3, table 2)
# ---------------------------------------------------------------------------
#
# The composite primary key (run_id, prediction_id) gives us per-run uniqueness
# without requiring application-side prediction_id collision avoidance across
# runs. Foreign keys to ``backtest_runs`` are intentionally omitted: DuckDB's
# FK support is limited and the project's exemplar persistence layers
# (data_ingest, report_generator) rely on application-level integrity. The
# closed-enum CHECK constraints below mirror the design §3.13 enumerations.

DDL_PREDICTIONS: Final[str] = """
CREATE TABLE IF NOT EXISTS backtest_predictions (
    run_id                          VARCHAR     NOT NULL,
    prediction_id                   VARCHAR     NOT NULL,
    class_id                        VARCHAR     NOT NULL,
    condition_id                    VARCHAR     NOT NULL,
    venue                           VARCHAR     NOT NULL,
    sector                          VARCHAR     NOT NULL,
    prediction_ts                   TIMESTAMPTZ NOT NULL,
    resolution_ts                   TIMESTAMPTZ NOT NULL,
    model_p                         DOUBLE      NULL,
    observed                        DOUBLE      NULL,
    polarity                        VARCHAR     NULL
        CHECK (polarity IS NULL OR polarity IN ('direct', 'inverted')),
    polarity_source                 VARCHAR     NOT NULL
        CHECK (polarity_source IN (
            'comparison_resolutions',
            'current_mapping_fallback',
            'no_polarity'
        )),
    mapping_mismatch_warning        BOOLEAN     NOT NULL DEFAULT FALSE,
    definition_version              INTEGER     NOT NULL,
    status                          VARCHAR     NOT NULL,
    skip_reason                     VARCHAR     NULL
        CHECK (skip_reason IS NULL OR skip_reason IN (
            'insufficient_lag',
            'invalid_resolution',
            'source_data_not_frozen',
            'no_polarity_resolution',
            'insufficient_data',
            'mapping_not_found',
            'exception'
        )),
    brier_contribution              DOUBLE      NULL,
    PRIMARY KEY (run_id, prediction_id)
)
""".strip()


# ---------------------------------------------------------------------------
# DDL — backtest_traces (design §3.3, table 3; §3.11)
# ---------------------------------------------------------------------------

DDL_TRACES: Final[str] = """
CREATE TABLE IF NOT EXISTS backtest_traces (
    run_id                          VARCHAR     NOT NULL,
    prediction_id                   VARCHAR     NOT NULL,
    trace_json_compressed           BLOB        NOT NULL,
    compression_algorithm           VARCHAR     NOT NULL DEFAULT 'zstd'
        CHECK (compression_algorithm IN ('zstd')),
    decompressed_size_bytes         INTEGER     NOT NULL,
    PRIMARY KEY (run_id, prediction_id)
)
""".strip()


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
#
# Per design §3.3 the canonical access patterns are:
#   - ``backtest_runs`` listed by status + recency for the run-history CLI
#   - ``backtest_runs`` filtered by (library_version, system_revision) when
#     pruning stale runs after a library bump (REQ-CB-PERSIST-001).
#   - ``backtest_predictions`` filtered by (run_id, sector / class_id / status)
#     for per-sector reliability rollups and skip-reason debugging.

INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_backtest_runs_status_started_at "
    "ON backtest_runs (status, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_backtest_runs_library_revision "
    "ON backtest_runs (library_version, system_revision)",
    "CREATE INDEX IF NOT EXISTS idx_backtest_predictions_run_sector "
    "ON backtest_predictions (run_id, sector)",
    "CREATE INDEX IF NOT EXISTS idx_backtest_predictions_run_class "
    "ON backtest_predictions (run_id, class_id)",
    "CREATE INDEX IF NOT EXISTS idx_backtest_predictions_run_status "
    "ON backtest_predictions (run_id, status)",
)


# ---------------------------------------------------------------------------
# Aggregated DDL exports
# ---------------------------------------------------------------------------

ALL_DDL: Final[tuple[str, ...]] = (DDL_RUNS, DDL_PREDICTIONS, DDL_TRACES)


# ---------------------------------------------------------------------------
# Pure helpers (no DB access — DB access lands in T-CB-010)
# ---------------------------------------------------------------------------


def list_tables() -> tuple[str, ...]:
    """Return the calibration-backtest table names in deterministic order."""
    return ALL_TABLES


def list_ddl() -> tuple[str, ...]:
    """Return every DDL statement (tables + indexes) in apply order."""
    return ALL_DDL + INDEXES


__all__ = [
    "ALL_DDL",
    "ALL_TABLES",
    "DDL_PREDICTIONS",
    "DDL_RUNS",
    "DDL_TRACES",
    "INDEXES",
    "SCHEMA_NAMESPACE",
    "TABLE_PREDICTIONS",
    "TABLE_RUNS",
    "TABLE_TRACES",
    "VERSION_6001",
    "VERSION_6002",
    "VERSION_6003",
    "list_ddl",
    "list_tables",
]
