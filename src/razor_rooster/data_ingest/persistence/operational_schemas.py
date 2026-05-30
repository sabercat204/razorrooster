"""Operational tables for ``data_ingest`` (T-011).

These tables hold the bookkeeping state that the rest of the persistence layer
depends on:

- ``sources`` — per-source registration, last-fetch tracking, license posture.
  License columns are populated by per-source startup gates such as
  ``ACLED_TERMS_VERSIONED`` (see REQ-ACLED-LICENSE-001).
- ``backfill_state`` — per-source resume tokens for interruptible backfill
  (REQ-BACKFILL-002).
- ``ingest_anomalies`` — append-only anomaly log for ingest-time issues that
  do not warrant a full failure (OQ-007).
- ``cycle_log`` — one row per cycle execution, references the structured JSON
  log on disk.
- ``schema_migrations`` — version tracking for schema migrations (T-013).

The ``freshness`` view is computed over ``sources`` and is consumed by every
downstream subsystem that needs to know whether a source is stale.
"""

from __future__ import annotations

from typing import Final

_SOURCES_DDL: Final[str] = """\
CREATE TABLE IF NOT EXISTS sources (
    source_id                       VARCHAR     PRIMARY KEY,
    source_type                     VARCHAR     NOT NULL,
    cadence                         VARCHAR     NOT NULL,
    freshness_threshold_seconds     INTEGER     NOT NULL,
    last_successful_fetch           TIMESTAMPTZ NULL,
    last_failed_fetch               TIMESTAMPTZ NULL,
    last_failure_summary            TEXT        NULL,
    license                         VARCHAR     NOT NULL,
    license_terms_hash              VARCHAR     NULL,
    license_acknowledged_at         TIMESTAMPTZ NULL,
    license_noncommercial_required  BOOLEAN     NOT NULL DEFAULT FALSE,
    commercial_use_recorded_grant   BOOLEAN     NOT NULL DEFAULT FALSE,
    acknowledged_posture            VARCHAR     NULL,
    registered_at                   TIMESTAMPTZ NOT NULL,
    notes                           TEXT        NULL
);"""


_BACKFILL_STATE_DDL: Final[str] = """\
CREATE TABLE IF NOT EXISTS backfill_state (
    source_id              VARCHAR     PRIMARY KEY,
    started_at             TIMESTAMPTZ NOT NULL,
    last_resume_token      VARCHAR     NULL,
    records_persisted      BIGINT      NOT NULL DEFAULT 0,
    bytes_persisted        BIGINT      NOT NULL DEFAULT 0,
    status                 VARCHAR     NOT NULL,
    last_updated_at        TIMESTAMPTZ NOT NULL,
    notes                  TEXT        NULL
);"""


_INGEST_ANOMALIES_DDL: Final[str] = """\
CREATE TABLE IF NOT EXISTS ingest_anomalies (
    anomaly_id             VARCHAR     PRIMARY KEY,
    source_id              VARCHAR     NOT NULL,
    cycle_id               VARCHAR     NULL,
    anomaly_type           VARCHAR     NOT NULL,
    detected_at            TIMESTAMPTZ NOT NULL,
    details_json           JSON        NOT NULL
);"""


_CYCLE_LOG_DDL: Final[str] = """\
CREATE TABLE IF NOT EXISTS cycle_log (
    cycle_id               VARCHAR     PRIMARY KEY,
    started_at             TIMESTAMPTZ NOT NULL,
    completed_at           TIMESTAMPTZ NULL,
    log_path               VARCHAR     NULL,
    summary_json           JSON        NULL
);"""


_SCHEMA_MIGRATIONS_DDL: Final[str] = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    version                INTEGER     PRIMARY KEY,
    applied_at             TIMESTAMPTZ NOT NULL,
    description            VARCHAR     NOT NULL
);"""


_FRESHNESS_VIEW_DDL: Final[str] = """\
CREATE OR REPLACE VIEW freshness AS
SELECT
    s.source_id,
    s.last_successful_fetch,
    s.last_failed_fetch,
    s.freshness_threshold_seconds,
    CASE
        WHEN s.last_successful_fetch IS NULL THEN NULL
        ELSE EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - s.last_successful_fetch))
    END AS seconds_since_fetch,
    CASE
        WHEN s.last_successful_fetch IS NULL THEN TRUE
        WHEN EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - s.last_successful_fetch))
             > s.freshness_threshold_seconds THEN TRUE
        ELSE FALSE
    END AS is_stale
FROM sources s;"""


# Operational-table indexes that aren't already covered by primary keys.
_OPERATIONAL_INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_ingest_anomalies_source_id "
    "ON ingest_anomalies (source_id, detected_at);",
    "CREATE INDEX IF NOT EXISTS idx_ingest_anomalies_cycle_id ON ingest_anomalies (cycle_id);",
    "CREATE INDEX IF NOT EXISTS idx_cycle_log_started_at ON cycle_log (started_at);",
)


def all_operational_ddl() -> tuple[str, ...]:
    """Return the full set of operational-schema DDL statements.

    Order matters: ``sources`` is created before the ``freshness`` view that
    selects from it.
    """
    return (
        _SOURCES_DDL,
        _BACKFILL_STATE_DDL,
        _INGEST_ANOMALIES_DDL,
        _CYCLE_LOG_DDL,
        _SCHEMA_MIGRATIONS_DDL,
        *_OPERATIONAL_INDEXES,
        _FRESHNESS_VIEW_DDL,
    )


def sources_ddl() -> str:
    """The ``sources`` table DDL.

    Exposed separately so the source registry (T-032) can refer to it without
    re-emitting the whole operational set.
    """
    return _SOURCES_DDL


def freshness_view_ddl() -> str:
    """The ``freshness`` view DDL."""
    return _FRESHNESS_VIEW_DDL
