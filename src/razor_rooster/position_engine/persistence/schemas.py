"""Position-engine namespaced table DDL (T-PE-010; design §3.3).

Five tables under the ``bankroll_config`` / ``analysis_*`` /
``watch_states`` namespace, all sharing schema-migration version
space 5001+ to stay clear of the five prior subsystems.
All timestamp columns use ``TIMESTAMPTZ`` per data_ingest REQ-NORM-002.

Tables:

- ``bankroll_config`` — per-update snapshot of the operator's
  declared bankroll + sizing knobs. Latest row wins; updates append
  rather than overwrite for audit.
- ``analysis_cycles`` — one row per cycle execution.
- ``analyses`` — one row per (cycle, comparison) pair.
- ``analysis_traces`` — rendered text + structured-JSON form per
  analysis. Stored separately so analysis queries don't drag the
  full text blob.
- ``watch_states`` — append-only log of operator watch-state
  transitions. Latest row per analysis wins.
"""

from __future__ import annotations

from typing import Final

BANKROLL_CONFIG_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS bankroll_config (
    config_id                      VARCHAR     PRIMARY KEY,
    analytical_bankroll_usd        DOUBLE      NOT NULL,
    max_single_position_pct        DOUBLE      NOT NULL,
    kelly_fraction_default         DOUBLE      NOT NULL,
    min_edge_threshold             DOUBLE      NOT NULL,
    effective_at                   TIMESTAMPTZ NOT NULL,
    updated_by                     VARCHAR     NOT NULL,
    notes                          TEXT        NULL
)
""".strip()


ANALYSIS_CYCLES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS analysis_cycles (
    cycle_id                       VARCHAR     PRIMARY KEY,
    started_at                     TIMESTAMPTZ NOT NULL,
    completed_at                   TIMESTAMPTZ NULL,
    bankroll_config_id             VARCHAR     NOT NULL,
    analyses_total                 INTEGER     NOT NULL,
    analyses_with_positive_kelly   INTEGER     NOT NULL,
    analyses_clamped_by_cap        INTEGER     NOT NULL,
    analyses_clamped_by_liquidity  INTEGER     NOT NULL,
    duration_seconds               DOUBLE      NULL,
    error_summary                  JSON        NULL
)
""".strip()


ANALYSES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS analyses (
    analysis_id                    VARCHAR     PRIMARY KEY,
    cycle_id                       VARCHAR     NOT NULL,
    comparison_id                  VARCHAR     NOT NULL,
    class_id                       VARCHAR     NOT NULL,
    condition_id                   VARCHAR     NOT NULL,
    venue                          VARCHAR     NOT NULL DEFAULT 'polymarket',
    bankroll_config_id             VARCHAR     NOT NULL,
    model_probability              DOUBLE      NOT NULL,
    market_probability             DOUBLE      NULL,
    kelly_unclamped                DOUBLE      NOT NULL,
    kelly_negative                 BOOLEAN     NOT NULL DEFAULT FALSE,
    kelly_clamped_by_max_cap       BOOLEAN     NOT NULL DEFAULT FALSE,
    kelly_clamped_by_liquidity     BOOLEAN     NOT NULL DEFAULT FALSE,
    suggested_fraction             DOUBLE      NOT NULL,
    suggested_dollar_size          DOUBLE      NOT NULL,
    ev_per_dollar                  DOUBLE      NULL,
    bankroll_after_1_loss_pct      DOUBLE      NOT NULL,
    bankroll_after_3_losses_pct    DOUBLE      NOT NULL,
    bankroll_after_5_losses_pct    DOUBLE      NOT NULL,
    suggested_pct_of_24h_volume    DOUBLE      NULL,
    days_to_resolution             INTEGER     NULL,
    long_time_to_resolution        BOOLEAN     NOT NULL DEFAULT FALSE,
    sub_threshold                  BOOLEAN     NOT NULL DEFAULT FALSE,
    sensitivity_analysis           JSON        NULL,
    invalidation_criteria          JSON        NOT NULL,
    low_signature_confidence       BOOLEAN     NOT NULL DEFAULT FALSE,
    source_stale_warning           BOOLEAN     NOT NULL DEFAULT FALSE,
    library_stale_warning          BOOLEAN     NOT NULL DEFAULT FALSE,
    definition_drift_warning       BOOLEAN     NOT NULL DEFAULT FALSE,
    low_mapping_confidence         BOOLEAN     NOT NULL DEFAULT FALSE,
    low_liquidity                  BOOLEAN     NOT NULL DEFAULT FALSE,
    error                          TEXT        NULL,
    computed_at                    TIMESTAMPTZ NOT NULL
)
""".strip()


ANALYSIS_TRACES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS analysis_traces (
    analysis_id                    VARCHAR     PRIMARY KEY,
    rendered_text                  TEXT        NOT NULL,
    structured_dict                JSON        NOT NULL
)
""".strip()


WATCH_STATES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS watch_states (
    state_id                       VARCHAR     PRIMARY KEY,
    analysis_id                    VARCHAR     NOT NULL,
    state                          VARCHAR     NOT NULL,
    notes                          TEXT        NULL,
    set_at                         TIMESTAMPTZ NOT NULL,
    set_by                         VARCHAR     NOT NULL
)
""".strip()


POSITION_ENGINE_INDEXES_DDL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_bankroll_config_effective "
    "ON bankroll_config (effective_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_analysis_cycles_started ON analysis_cycles (started_at)",
    "CREATE INDEX IF NOT EXISTS idx_analyses_cycle ON analyses (cycle_id)",
    "CREATE INDEX IF NOT EXISTS idx_analyses_comparison ON analyses (comparison_id)",
    "CREATE INDEX IF NOT EXISTS idx_analyses_class_computed ON analyses (class_id, computed_at)",
    "CREATE INDEX IF NOT EXISTS idx_analyses_venue_computed ON analyses (venue, computed_at)",
    "CREATE INDEX IF NOT EXISTS idx_watch_states_analysis_set_at "
    "ON watch_states (analysis_id, set_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_watch_states_state ON watch_states (state)",
)


ALL_POSITION_ENGINE_DDL: Final[tuple[str, ...]] = (
    BANKROLL_CONFIG_DDL,
    ANALYSIS_CYCLES_DDL,
    ANALYSES_DDL,
    ANALYSIS_TRACES_DDL,
    WATCH_STATES_DDL,
)


# Drop order is the inverse of create order.
POSITION_ENGINE_TABLE_NAMES: Final[tuple[str, ...]] = (
    "watch_states",
    "analysis_traces",
    "analyses",
    "analysis_cycles",
    "bankroll_config",
)
