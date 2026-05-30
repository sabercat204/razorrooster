"""Mispricing-detector namespaced table DDL (T-MD-010; design §3.3).

Six tables under the ``class_market_mappings`` / ``comparison_*`` /
``mispricing_detector_state`` namespace, all sharing schema-migration
version space 4001+ to stay clear of the four prior subsystems.
All timestamp columns use ``TIMESTAMPTZ`` per data_ingest REQ-NORM-002.

Tables:

- ``class_market_mappings`` — one row per (class, market) mapping.
  Both operator-curated and auto-derived mappings live here, but
  auto-mappings are computed fresh per cycle and not persisted by
  default — only operator-curated mappings are stable rows.
  Soft-delete via ``removed_at``; the unique constraint on
  ``(class_id, condition_id, polarity)`` enforces a single active
  mapping per pair.
- ``comparison_cycles`` — one row per cycle execution.
- ``comparisons`` — one row per (cycle, mapping) pair.
- ``comparison_traces`` — full reasoning trace JSON, separated for
  query-performance reasons (same pattern as scan_traces).
- ``comparison_resolutions`` — calibration backtest scaffolding;
  populated by the linkage pass per OQ-MD-005.
- ``mispricing_detector_state`` — single-row table tracking
  ``last_linkage_ts`` and similar per-subsystem state.
"""

from __future__ import annotations

from typing import Final

CLASS_MARKET_MAPPINGS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS class_market_mappings (
    mapping_id                     VARCHAR     PRIMARY KEY,
    class_id                       VARCHAR     NOT NULL,
    condition_id                   VARCHAR     NOT NULL,
    mapping_type                   VARCHAR     NOT NULL,
    mapping_confidence             VARCHAR     NOT NULL,
    polarity                       VARCHAR     NOT NULL DEFAULT 'aligned',
    mapped_by                      VARCHAR     NOT NULL,
    mapped_at                      TIMESTAMPTZ NOT NULL,
    removed_at                     TIMESTAMPTZ NULL,
    notes                          TEXT        NULL,
    venue                          VARCHAR     NOT NULL DEFAULT 'polymarket'
)
""".strip()


COMPARISON_CYCLES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS comparison_cycles (
    cycle_id                       VARCHAR     PRIMARY KEY,
    started_at                     TIMESTAMPTZ NOT NULL,
    completed_at                   TIMESTAMPTZ NULL,
    comparisons_total              INTEGER     NOT NULL,
    surfaced_count                 INTEGER     NOT NULL,
    suppressed_breakdown           JSON        NOT NULL,
    library_version_at_cycle       INTEGER     NOT NULL,
    scan_id_consumed               VARCHAR     NOT NULL,
    error_summary                  JSON        NULL
)
""".strip()


COMPARISONS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS comparisons (
    comparison_id                  VARCHAR     PRIMARY KEY,
    cycle_id                       VARCHAR     NOT NULL,
    mapping_id                     VARCHAR     NOT NULL,
    class_id                       VARCHAR     NOT NULL,
    condition_id                   VARCHAR     NOT NULL,
    outcome_token_id               VARCHAR     NOT NULL,
    polarity                       VARCHAR     NOT NULL,
    scan_id                        VARCHAR     NOT NULL,
    model_probability              DOUBLE      NOT NULL,
    model_ci_lower                 DOUBLE      NOT NULL,
    model_ci_upper                 DOUBLE      NOT NULL,
    market_probability             DOUBLE      NULL,
    market_best_bid                DOUBLE      NULL,
    market_best_ask                DOUBLE      NULL,
    market_last_trade_price        DOUBLE      NULL,
    market_volume_24h              DOUBLE      NULL,
    market_spread_bps              INTEGER     NULL,
    market_snapshot_ts             TIMESTAMPTZ NULL,
    delta                          DOUBLE      NULL,
    log_odds_delta                 DOUBLE      NULL,
    ci_overlap                     BOOLEAN     NOT NULL DEFAULT FALSE,
    expected_value                 DOUBLE      NULL,
    confidence_weighted_score      DOUBLE      NULL,
    surfaced                       BOOLEAN     NOT NULL DEFAULT FALSE,
    suppression_reasons            JSON        NULL,
    low_signature_confidence       BOOLEAN     NOT NULL DEFAULT FALSE,
    source_stale_warning           BOOLEAN     NOT NULL DEFAULT FALSE,
    library_stale_warning          BOOLEAN     NOT NULL DEFAULT FALSE,
    definition_drift_warning       BOOLEAN     NOT NULL DEFAULT FALSE,
    stale_market_price             BOOLEAN     NOT NULL DEFAULT FALSE,
    no_market_price                BOOLEAN     NOT NULL DEFAULT FALSE,
    degenerate_orderbook           BOOLEAN     NOT NULL DEFAULT FALSE,
    low_liquidity                  BOOLEAN     NOT NULL DEFAULT FALSE,
    low_mapping_confidence         BOOLEAN     NOT NULL DEFAULT FALSE,
    error                          TEXT        NULL,
    computed_at                    TIMESTAMPTZ NOT NULL,
    venue                          VARCHAR     NOT NULL DEFAULT 'polymarket'
)
""".strip()


COMPARISON_TRACES_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS comparison_traces (
    comparison_id                  VARCHAR     PRIMARY KEY,
    trace_json                     JSON        NOT NULL
)
""".strip()


COMPARISON_RESOLUTIONS_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS comparison_resolutions (
    comparison_id                  VARCHAR     PRIMARY KEY,
    condition_id                   VARCHAR     NOT NULL,
    resolution_outcome             VARCHAR     NOT NULL,
    resolution_ts                  TIMESTAMPTZ NOT NULL,
    model_probability_at_comparison DOUBLE     NOT NULL,
    market_probability_at_comparison DOUBLE    NULL,
    polarity_at_comparison         VARCHAR     NOT NULL,
    outcome_observed               INTEGER     NOT NULL,
    linked_at                      TIMESTAMPTZ NOT NULL,
    venue                          VARCHAR     NOT NULL DEFAULT 'polymarket'
)
""".strip()


MISPRICING_DETECTOR_STATE_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS mispricing_detector_state (
    state_key                      VARCHAR     PRIMARY KEY,
    state_value                    VARCHAR     NOT NULL,
    updated_at                     TIMESTAMPTZ NOT NULL
)
""".strip()


MISPRICING_INDEXES_DDL: Final[tuple[str, ...]] = (
    # DuckDB does not support partial indexes, so the active-mapping
    # uniqueness invariant is enforced at the application level by
    # ``register_mapping`` (which queries for an existing active row
    # with the same (class_id, venue, condition_id, polarity) before
    # insert; venue defaults to 'polymarket' so pre-Kalshi callers
    # behave identically).
    "CREATE INDEX IF NOT EXISTS idx_class_market_mappings_active "
    "ON class_market_mappings (class_id, venue, condition_id, polarity, removed_at)",
    "CREATE INDEX IF NOT EXISTS idx_class_market_mappings_class "
    "ON class_market_mappings (class_id)",
    "CREATE INDEX IF NOT EXISTS idx_class_market_mappings_market "
    "ON class_market_mappings (condition_id)",
    "CREATE INDEX IF NOT EXISTS idx_class_market_mappings_confidence "
    "ON class_market_mappings (mapping_confidence)",
    "CREATE INDEX IF NOT EXISTS idx_comparison_cycles_started ON comparison_cycles (started_at)",
    "CREATE INDEX IF NOT EXISTS idx_comparisons_cycle_surfaced ON comparisons (cycle_id, surfaced)",
    "CREATE INDEX IF NOT EXISTS idx_comparisons_class_computed "
    "ON comparisons (class_id, computed_at)",
    "CREATE INDEX IF NOT EXISTS idx_comparisons_market_computed "
    "ON comparisons (condition_id, computed_at)",
    "CREATE INDEX IF NOT EXISTS idx_comparisons_venue_class_computed "
    "ON comparisons (venue, class_id, computed_at)",
    "CREATE INDEX IF NOT EXISTS idx_comparison_resolutions_market "
    "ON comparison_resolutions (condition_id)",
    "CREATE INDEX IF NOT EXISTS idx_comparison_resolutions_ts "
    "ON comparison_resolutions (resolution_ts)",
)


ALL_MISPRICING_DDL: Final[tuple[str, ...]] = (
    CLASS_MARKET_MAPPINGS_DDL,
    COMPARISON_CYCLES_DDL,
    COMPARISONS_DDL,
    COMPARISON_TRACES_DDL,
    COMPARISON_RESOLUTIONS_DDL,
    MISPRICING_DETECTOR_STATE_DDL,
)


# Drop order is the inverse of create order.
MISPRICING_TABLE_NAMES: Final[tuple[str, ...]] = (
    "mispricing_detector_state",
    "comparison_resolutions",
    "comparison_traces",
    "comparisons",
    "comparison_cycles",
    "class_market_mappings",
)
