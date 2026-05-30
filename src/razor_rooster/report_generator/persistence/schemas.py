"""Report-generator namespaced table DDL (T-RG-010; design §3.3).

One ``report_log`` table sharing schema-migration version space
7001+ to stay clear of the seven prior subsystems. v0.40.0 adds
``report_threshold_measurements`` (T-RG-COMPAT-MEAS-001) — one
row per (report_id, measurement_kind) recording the
distribution of the underlying signal so operators can see
whether their configured threshold is well-calibrated for the
corpus.

All timestamp columns use ``TIMESTAMPTZ`` per data_ingest REQ-NORM-002.
"""

from __future__ import annotations

from typing import Final

REPORT_LOG_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS report_log (
    report_id                      VARCHAR     PRIMARY KEY,
    generated_at                   TIMESTAMPTZ NOT NULL,
    since_ts                       TIMESTAMPTZ NOT NULL,
    until_ts                       TIMESTAMPTZ NOT NULL,
    sections_enabled               JSON        NOT NULL,
    sections_rendered              JSON        NOT NULL,
    sections_failed                JSON        NOT NULL,
    library_version                INTEGER     NOT NULL,
    disclaimer_version_hash        VARCHAR     NOT NULL,
    rendered_terminal_text         TEXT        NOT NULL,
    rendered_markdown_text         TEXT        NULL,
    markdown_path                  VARCHAR     NULL,
    rendered_html_text             TEXT        NULL,
    html_path                      VARCHAR     NULL,
    duration_seconds               DOUBLE      NULL
)
""".strip()


# v0.40.0: per-cycle threshold-distribution measurements
# (T-RG-COMPAT-MEAS-001).
#
# ``measurement_kind`` is a bounded enum (currently
# ``cross_venue_spread_bps``; future kinds will follow the same
# wire format). The distribution stats are persisted as JSON so
# new percentiles can be added without an ALTER TABLE.
#
# ``configured_threshold`` is the global threshold value at the
# time of measurement, so a historical record is preserved even
# if the operator changes the config later.
REPORT_THRESHOLD_MEASUREMENTS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS report_threshold_measurements (
    report_id                      VARCHAR     NOT NULL,
    measurement_kind               VARCHAR     NOT NULL,
    measured_at                    TIMESTAMPTZ NOT NULL,
    n_observations                 INTEGER     NOT NULL,
    n_above_threshold              INTEGER     NOT NULL,
    configured_threshold           DOUBLE      NOT NULL,
    distribution_json              JSON        NOT NULL,
    PRIMARY KEY (report_id, measurement_kind)
)
""".strip()


# v0.43.0: tuning-log table records every successful
# ``--apply`` from ``razor-rooster report suggest-thresholds``
# so operators can review retroactively how thresholds drifted
# (T-RG-COMPAT-TUNINGLOG-001). One row per write; newest-first
# ordering enforced by the index.
THRESHOLD_TUNING_LOG_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS threshold_tuning_log (
    log_id                         VARCHAR     PRIMARY KEY,
    applied_at                     TIMESTAMPTZ NOT NULL,
    measurement_kind               VARCHAR     NOT NULL,
    knob                           VARCHAR     NOT NULL,
    previous_value                 DOUBLE      NULL,
    new_value                      DOUBLE      NOT NULL,
    target_percentile              DOUBLE      NULL,
    backup_path                    VARCHAR     NULL,
    note                           VARCHAR     NULL
)
""".strip()


REPORT_GENERATOR_INDEXES_DDL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_report_log_generated_at ON report_log (generated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_report_threshold_measurements_kind_at "
    "ON report_threshold_measurements (measurement_kind, measured_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_threshold_tuning_log_applied_at "
    "ON threshold_tuning_log (applied_at DESC)",
)


ALL_REPORT_GENERATOR_DDL: Final[tuple[str, ...]] = (
    REPORT_LOG_DDL,
    REPORT_THRESHOLD_MEASUREMENTS_DDL,
    THRESHOLD_TUNING_LOG_DDL,
)


REPORT_GENERATOR_TABLE_NAMES: Final[tuple[str, ...]] = (
    "report_log",
    "report_threshold_measurements",
    "threshold_tuning_log",
)
