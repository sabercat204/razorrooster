"""Signal-scanner namespaced table DDL (T-SCAN-010; design §3.3).

Three ``scan_*`` tables live alongside the data_ingest canonical
schemas, the ``polymarket_*`` namespace, and the ``pl_*`` namespace
in the same DuckDB store. All timestamp columns use ``TIMESTAMPTZ``
per data_ingest REQ-NORM-002.

- ``scan_summaries`` — one row per scan execution. Aggregate stats.
- ``scan_records`` — one row per (scan_id, class_id) pair. Per-class
  posterior, divergence, candidate flag, warnings, error.
- ``scan_traces`` — full reasoning trace JSON per (scan_id, class_id).
  Stored separately so queries against ``scan_records`` aren't
  pessimized by full-trace blobs.

Per design §3.3 the scan tables share schema-migration version space
with the rest of the system; signal_scanner uses versions 3001+.
"""

from __future__ import annotations

from typing import Final

SCAN_SUMMARIES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS scan_summaries (
    scan_id                        VARCHAR     PRIMARY KEY,
    scan_started_at                TIMESTAMPTZ NOT NULL,
    scan_completed_at              TIMESTAMPTZ NULL,
    pattern_library_version        INTEGER     NOT NULL,
    classes_total                  INTEGER     NOT NULL,
    classes_succeeded              INTEGER     NOT NULL,
    classes_failed                 INTEGER     NOT NULL,
    classes_skipped                INTEGER     NOT NULL,
    candidates_count               INTEGER     NOT NULL,
    library_stale_warning          BOOLEAN     NOT NULL DEFAULT FALSE,
    config_snapshot                JSON        NULL,
    error_summary                  JSON        NULL
)
""".strip()


SCAN_RECORDS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS scan_records (
    scan_id                        VARCHAR     NOT NULL,
    class_id                       VARCHAR     NOT NULL,
    class_definition_version       INTEGER     NOT NULL,
    pattern_library_version        INTEGER     NOT NULL,
    data_as_of                     TIMESTAMPTZ NOT NULL,
    scan_started_at                TIMESTAMPTZ NOT NULL,
    scan_completed_at              TIMESTAMPTZ NULL,
    base_rate                      DOUBLE      NOT NULL,
    base_rate_ci_lower             DOUBLE      NOT NULL,
    base_rate_ci_upper             DOUBLE      NOT NULL,
    posterior                      DOUBLE      NOT NULL,
    posterior_ci_lower             DOUBLE      NOT NULL,
    posterior_ci_upper             DOUBLE      NOT NULL,
    log_odds_shift                 DOUBLE      NOT NULL,
    is_candidate                   BOOLEAN     NOT NULL DEFAULT FALSE,
    candidate_direction            VARCHAR     NULL,
    signature_confidence           DOUBLE      NULL,
    low_signature_confidence       BOOLEAN     NOT NULL DEFAULT FALSE,
    source_stale_warning           BOOLEAN     NOT NULL DEFAULT FALSE,
    library_stale_warning          BOOLEAN     NOT NULL DEFAULT FALSE,
    definition_drift_warning       BOOLEAN     NOT NULL DEFAULT FALSE,
    no_update_applied              BOOLEAN     NOT NULL DEFAULT FALSE,
    no_update_reason               VARCHAR     NULL,
    error                          TEXT        NULL,
    PRIMARY KEY (scan_id, class_id)
)
""".strip()


SCAN_TRACES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS scan_traces (
    scan_id                        VARCHAR     NOT NULL,
    class_id                       VARCHAR     NOT NULL,
    trace_json                     JSON        NOT NULL,
    PRIMARY KEY (scan_id, class_id)
)
""".strip()


SCAN_INDEXES_DDL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_scan_summaries_started_at ON scan_summaries (scan_started_at)",
    "CREATE INDEX IF NOT EXISTS idx_scan_records_class_started "
    "ON scan_records (class_id, scan_started_at)",
    "CREATE INDEX IF NOT EXISTS idx_scan_records_candidate_started "
    "ON scan_records (is_candidate, scan_started_at)",
    "CREATE INDEX IF NOT EXISTS idx_scan_records_library_version "
    "ON scan_records (pattern_library_version)",
)


ALL_SCAN_DDL: Final[tuple[str, ...]] = (
    SCAN_SUMMARIES_DDL,
    SCAN_RECORDS_DDL,
    SCAN_TRACES_DDL,
)


# Drop order: traces before records (FK-shaped though not enforced),
# records before summaries (records reference scan_id).
SCAN_TABLE_NAMES: Final[tuple[str, ...]] = (
    "scan_traces",
    "scan_records",
    "scan_summaries",
)
