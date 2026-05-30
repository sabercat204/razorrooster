"""Monitor namespaced table DDL (T-MON-010; design §3.3).

Three tables under the monitor namespace, all sharing schema-migration
version space 6001+ to stay clear of the six prior subsystems.
All timestamp columns use ``TIMESTAMPTZ`` per data_ingest REQ-NORM-002.

Tables:

- ``monitor_cycles`` — one row per cycle execution.
- ``follow_ups`` — one row per (cycle, analysis) pair. Captures the
  full point-in-time snapshot of one watched analysis: analysis-time
  values copied for trajectory queries, current-state values, per-
  dimension shifts with band classification, precursor snapshot,
  invalidation evaluations, alert ranking, reasoning text.
- ``follow_up_notes`` — append-only log of operator notes attached to
  follow-ups (OQ-MON-004 resolution).
"""

from __future__ import annotations

from typing import Final

MONITOR_CYCLES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS monitor_cycles (
    cycle_id                       VARCHAR     PRIMARY KEY,
    started_at                     TIMESTAMPTZ NOT NULL,
    completed_at                   TIMESTAMPTZ NULL,
    follow_ups_total               INTEGER     NOT NULL,
    follow_ups_with_alerts         INTEGER     NOT NULL,
    alerts_by_tier                 JSON        NOT NULL,
    duration_seconds               DOUBLE      NULL,
    error_summary                  JSON        NULL
)
""".strip()


FOLLOW_UPS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS follow_ups (
    follow_up_id                   VARCHAR     PRIMARY KEY,
    cycle_id                       VARCHAR     NOT NULL,
    analysis_id                    VARCHAR     NOT NULL,
    venue                          VARCHAR     NOT NULL DEFAULT 'polymarket',
    analysis_model_p               DOUBLE      NOT NULL,
    analysis_market_p              DOUBLE      NULL,
    analysis_computed_at           TIMESTAMPTZ NOT NULL,
    current_scan_id                VARCHAR     NULL,
    current_model_p                DOUBLE      NULL,
    current_model_ci               JSON        NULL,
    current_market_p               DOUBLE      NULL,
    current_market_snapshot_ts     TIMESTAMPTZ NULL,
    model_probability_shift        DOUBLE      NULL,
    model_shift_band               VARCHAR     NULL,
    market_probability_shift       DOUBLE      NULL,
    market_shift_band              VARCHAR     NULL,
    precursor_snapshot             JSON        NOT NULL,
    days_since_analysis            INTEGER     NOT NULL,
    days_to_resolution             INTEGER     NULL,
    time_decay_alert               BOOLEAN     NOT NULL DEFAULT FALSE,
    invalidation_evaluations       JSON        NOT NULL,
    invalidation_triggered_count   INTEGER     NOT NULL,
    resolution_status              VARCHAR     NOT NULL,
    recommended_review             BOOLEAN     NOT NULL,
    primary_alert_tier             VARCHAR     NULL,
    alert_tiers                    JSON        NOT NULL,
    reasoning_text                 TEXT        NOT NULL,
    source_stale_warning           BOOLEAN     NOT NULL DEFAULT FALSE,
    library_stale_warning          BOOLEAN     NOT NULL DEFAULT FALSE,
    error                          TEXT        NULL,
    computed_at                    TIMESTAMPTZ NOT NULL
)
""".strip()


FOLLOW_UP_NOTES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS follow_up_notes (
    note_id                        VARCHAR     PRIMARY KEY,
    follow_up_id                   VARCHAR     NOT NULL,
    note_text                      TEXT        NOT NULL,
    set_at                         TIMESTAMPTZ NOT NULL,
    set_by                         VARCHAR     NOT NULL
)
""".strip()


MONITOR_INDEXES_DDL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_monitor_cycles_started ON monitor_cycles (started_at)",
    "CREATE INDEX IF NOT EXISTS idx_follow_ups_analysis_computed "
    "ON follow_ups (analysis_id, computed_at)",
    "CREATE INDEX IF NOT EXISTS idx_follow_ups_cycle_alert "
    "ON follow_ups (cycle_id, primary_alert_tier)",
    "CREATE INDEX IF NOT EXISTS idx_follow_ups_recommended "
    "ON follow_ups (recommended_review, computed_at)",
    "CREATE INDEX IF NOT EXISTS idx_follow_up_notes_follow_up_set_at "
    "ON follow_up_notes (follow_up_id, set_at DESC)",
)


ALL_MONITOR_DDL: Final[tuple[str, ...]] = (
    MONITOR_CYCLES_DDL,
    FOLLOW_UPS_DDL,
    FOLLOW_UP_NOTES_DDL,
)


# Drop order is the inverse of create order.
MONITOR_TABLE_NAMES: Final[tuple[str, ...]] = (
    "follow_up_notes",
    "follow_ups",
    "monitor_cycles",
)
