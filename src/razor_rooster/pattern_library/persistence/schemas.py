"""Pattern-library namespaced table DDL (T-PL-011; design §3.4).

Eight ``pl_*`` tables live alongside the data_ingest canonical schemas
and the polymarket_* namespace in the same DuckDB store. All timestamp
columns use ``TIMESTAMPTZ`` per data_ingest REQ-NORM-002.

The tables fall in two groups:

- **Per-class outputs** (``pl_outcomes``, ``pl_base_rates``,
  ``pl_precursor_signatures``, ``pl_analogue_features``,
  ``pl_calibration``) — written by the refresh runner.
- **Operational metadata** (``pl_event_classes``, ``pl_library_versions``,
  ``pl_refresh_log``) — written by registry sync and refresh
  orchestration.

Versioning columns (`library_version`, `definition_version`) appear on
every output table so downstream consumers can detect mismatches.
"""

from __future__ import annotations

from typing import Final

PL_EVENT_CLASSES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS pl_event_classes (
    class_id                       VARCHAR     PRIMARY KEY,
    title                          TEXT        NOT NULL,
    description                    TEXT        NOT NULL,
    domain_sector                  VARCHAR     NOT NULL,
    secondary_sectors              JSON        NULL,
    definition_version             INTEGER     NOT NULL,
    outcome_type                   VARCHAR     NOT NULL,
    registered_at                  TIMESTAMPTZ NOT NULL,
    last_evaluated_at              TIMESTAMPTZ NULL,
    library_version_at_last_eval   INTEGER     NULL,
    removed_at                     TIMESTAMPTZ NULL
)
""".strip()


PL_OUTCOMES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS pl_outcomes (
    class_id                       VARCHAR     NOT NULL,
    occurrence_id                  VARCHAR     NOT NULL,
    occurrence_ts                  TIMESTAMPTZ NOT NULL,
    end_ts                         TIMESTAMPTZ NULL,
    description                    TEXT        NULL,
    source_records                 JSON        NOT NULL,
    library_version                INTEGER     NOT NULL,
    definition_version             INTEGER     NOT NULL,
    computed_at                    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (class_id, occurrence_id)
)
""".strip()


PL_BASE_RATES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS pl_base_rates (
    class_id                       VARCHAR     NOT NULL,
    window_start                   TIMESTAMPTZ NOT NULL,
    window_end                     TIMESTAMPTZ NOT NULL,
    occurrences                    INTEGER     NOT NULL,
    rate_per_year                  DOUBLE      NOT NULL,
    credible_interval_lower        DOUBLE      NOT NULL,
    credible_interval_upper        DOUBLE      NOT NULL,
    prior_alpha                    DOUBLE      NOT NULL,
    prior_beta                     DOUBLE      NOT NULL,
    library_version                INTEGER     NOT NULL,
    definition_version             INTEGER     NOT NULL,
    data_as_of                     TIMESTAMPTZ NOT NULL,
    computed_at                    TIMESTAMPTZ NOT NULL,
    low_sample_warning             BOOLEAN     NOT NULL DEFAULT FALSE,
    source_stale_warning           BOOLEAN     NOT NULL DEFAULT FALSE,
    stale                          BOOLEAN     NOT NULL DEFAULT FALSE,
    PRIMARY KEY (class_id, window_start, window_end, library_version)
)
""".strip()


PL_PRECURSOR_SIGNATURES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS pl_precursor_signatures (
    class_id                       VARCHAR     NOT NULL,
    variable_id                    VARCHAR     NOT NULL,
    library_version                INTEGER     NOT NULL,
    definition_version             INTEGER     NOT NULL,
    threshold_method               VARCHAR     NOT NULL,
    threshold_value                DOUBLE      NULL,
    direction                      VARCHAR     NOT NULL,
    lead_time_window_days          INTEGER     NOT NULL,
    pre_event_mean                 DOUBLE      NULL,
    pre_event_p25                  DOUBLE      NULL,
    pre_event_p50                  DOUBLE      NULL,
    pre_event_p75                  DOUBLE      NULL,
    baseline_mean                  DOUBLE      NULL,
    baseline_p25                   DOUBLE      NULL,
    baseline_p50                   DOUBLE      NULL,
    baseline_p75                   DOUBLE      NULL,
    hit_rate                       DOUBLE      NULL,
    false_positive_rate            DOUBLE      NULL,
    sample_size_events             INTEGER     NOT NULL,
    sample_size_baseline           INTEGER     NOT NULL,
    confidence_score               DOUBLE      NOT NULL,
    low_confidence_warning         BOOLEAN     NOT NULL DEFAULT FALSE,
    computed_at                    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (class_id, variable_id, library_version)
)
""".strip()


PL_ANALOGUE_FEATURES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS pl_analogue_features (
    class_id                       VARCHAR     NOT NULL,
    point_id                       VARCHAR     NOT NULL,
    timestamp                      TIMESTAMPTZ NOT NULL,
    is_event                       BOOLEAN     NOT NULL,
    feature_vector_raw             JSON        NOT NULL,
    feature_vector_normalized      JSON        NOT NULL,
    library_version                INTEGER     NOT NULL,
    definition_version             INTEGER     NOT NULL,
    computed_at                    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (class_id, point_id, library_version)
)
""".strip()


PL_CALIBRATION_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS pl_calibration (
    class_id                       VARCHAR     NOT NULL,
    library_version                INTEGER     NOT NULL,
    definition_version             INTEGER     NOT NULL,
    method                         VARCHAR     NOT NULL,
    brier_score                    DOUBLE      NULL,
    reliability_bins               JSON        NOT NULL,
    prediction_trace_path          VARCHAR     NOT NULL,
    computed_at                    TIMESTAMPTZ NOT NULL,
    notes                          TEXT        NULL,
    PRIMARY KEY (class_id, library_version, method)
)
""".strip()


PL_LIBRARY_VERSIONS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS pl_library_versions (
    library_version                INTEGER     PRIMARY KEY,
    bumped_at                      TIMESTAMPTZ NOT NULL,
    bump_reason                    VARCHAR     NOT NULL,
    affected_class_ids             JSON        NULL,
    notes                          TEXT        NULL
)
""".strip()


PL_REFRESH_LOG_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS pl_refresh_log (
    refresh_id                     VARCHAR     PRIMARY KEY,
    started_at                     TIMESTAMPTZ NOT NULL,
    ended_at                       TIMESTAMPTZ NULL,
    library_version                INTEGER     NOT NULL,
    classes_processed              JSON        NOT NULL,
    error_summary                  JSON        NULL
)
""".strip()


PL_INDEXES_DDL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_pl_event_classes_sector ON pl_event_classes (domain_sector)",
    "CREATE INDEX IF NOT EXISTS idx_pl_outcomes_class_ts ON pl_outcomes (class_id, occurrence_ts)",
    "CREATE INDEX IF NOT EXISTS idx_pl_base_rates_class "
    "ON pl_base_rates (class_id, library_version)",
    "CREATE INDEX IF NOT EXISTS idx_pl_signatures_class "
    "ON pl_precursor_signatures (class_id, library_version)",
    "CREATE INDEX IF NOT EXISTS idx_pl_analogue_class_isevent "
    "ON pl_analogue_features (class_id, library_version, is_event)",
    "CREATE INDEX IF NOT EXISTS idx_pl_calibration_class "
    "ON pl_calibration (class_id, library_version)",
    "CREATE INDEX IF NOT EXISTS idx_pl_refresh_started_at ON pl_refresh_log (started_at)",
)


ALL_PL_DDL: Final[tuple[str, ...]] = (
    PL_EVENT_CLASSES_DDL,
    PL_OUTCOMES_DDL,
    PL_BASE_RATES_DDL,
    PL_PRECURSOR_SIGNATURES_DDL,
    PL_ANALOGUE_FEATURES_DDL,
    PL_CALIBRATION_DDL,
    PL_LIBRARY_VERSIONS_DDL,
    PL_REFRESH_LOG_DDL,
)


# Drop order is the inverse of create. Dependent tables drop first.
PL_TABLE_NAMES: Final[tuple[str, ...]] = (
    "pl_refresh_log",
    "pl_library_versions",
    "pl_calibration",
    "pl_analogue_features",
    "pl_precursor_signatures",
    "pl_base_rates",
    "pl_outcomes",
    "pl_event_classes",
)
