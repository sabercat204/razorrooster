"""Kalshi-namespaced table DDL (T-KSI-010; design §3.3).

Every Kalshi-sourced row carries the provenance prefix from
``data_ingest`` design §4 (source_id, source_record_id, fetch_ts,
connector_version, source_payload_json, superseded_at,
source_publication_ts) where applicable. The ``kalshi_historical_cutoff``
table is single-row state (no provenance), and ``kalshi_sector_mapping``
is operator-or-heuristic curated rather than source-fetched.

These are not canonical schemas in the ``data_ingest`` sense — they
live under the ``kalshi_*`` namespace and are specific to this
subsystem, sibling to the ``polymarket_*`` namespace. ``data_ingest``'s
four canonical schemas are unchanged.

All timestamp columns use ``TIMESTAMPTZ`` per ``data_ingest``
REQ-NORM-002. Schema-migration version space 8001+.

Per design §3.3, the Kalshi orderbook is YES-side-only. NO-side prices
are derived by callers (``no_bid = 1 - yes_ask``). The schema therefore
stores only YES quotes and makes the NO derivation a read-time
computation rather than a write-time duplication.
"""

from __future__ import annotations

from typing import Final

# Provenance prefix shared with data_ingest canonical schemas.
_PROVENANCE_COLUMNS: Final[str] = """
    source_id                 VARCHAR     NOT NULL,
    source_record_id          VARCHAR     NOT NULL,
    source_publication_ts     TIMESTAMPTZ NOT NULL,
    fetch_ts                  TIMESTAMPTZ NOT NULL,
    connector_version         VARCHAR     NOT NULL,
    source_payload_json       JSON        NOT NULL,
    superseded_at             TIMESTAMPTZ NULL
""".strip()


KALSHI_SERIES_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS kalshi_series (
    {_PROVENANCE_COLUMNS},
    series_ticker             VARCHAR     NOT NULL,
    title                     TEXT        NOT NULL,
    category                  VARCHAR     NULL,
    frequency                 VARCHAR     NULL,
    tags                      JSON        NULL,
    settlement_source         TEXT        NULL,
    contract_url              VARCHAR     NULL,
    created_at                TIMESTAMPTZ NULL,
    last_updated_at           TIMESTAMPTZ NULL,
    removed_at                TIMESTAMPTZ NULL
)
""".strip()


KALSHI_EVENTS_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS kalshi_events (
    {_PROVENANCE_COLUMNS},
    event_ticker              VARCHAR     NOT NULL,
    series_ticker             VARCHAR     NOT NULL,
    title                     TEXT        NOT NULL,
    sub_title                 TEXT        NULL,
    category                  VARCHAR     NULL,
    mutually_exclusive        BOOLEAN     NOT NULL DEFAULT FALSE,
    expected_expiration_time  TIMESTAMPTZ NULL,
    strike_period             VARCHAR     NULL,
    status                    VARCHAR     NOT NULL,
    created_at                TIMESTAMPTZ NULL,
    last_updated_at           TIMESTAMPTZ NULL,
    removed_at                TIMESTAMPTZ NULL
)
""".strip()


KALSHI_MARKETS_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS kalshi_markets (
    {_PROVENANCE_COLUMNS},
    ticker                    VARCHAR     NOT NULL,
    event_ticker              VARCHAR     NOT NULL,
    series_ticker             VARCHAR     NOT NULL,
    title                     TEXT        NOT NULL,
    sub_title                 TEXT        NULL,
    market_type               VARCHAR     NOT NULL,
    strike_type               VARCHAR     NULL,
    floor_strike              DOUBLE      NULL,
    cap_strike                DOUBLE      NULL,
    open_time                 TIMESTAMPTZ NULL,
    close_time                TIMESTAMPTZ NULL,
    expiration_time           TIMESTAMPTZ NULL,
    expected_expiration_time  TIMESTAMPTZ NULL,
    latest_expiration_time    TIMESTAMPTZ NULL,
    settlement_timer_seconds  INTEGER     NULL,
    status                    VARCHAR     NOT NULL,
    yes_sub_title             TEXT        NULL,
    no_sub_title              TEXT        NULL,
    result                    VARCHAR     NULL,
    can_close_early           BOOLEAN     NULL,
    expiration_value          DOUBLE      NULL,
    category                  VARCHAR     NULL,
    risk_limit_cents          INTEGER     NULL,
    notional_value            DOUBLE      NULL,
    tick_size                 DOUBLE      NULL,
    last_price_dollars        DOUBLE      NULL,
    previous_yes_bid_dollars  DOUBLE      NULL,
    previous_yes_ask_dollars  DOUBLE      NULL,
    previous_price_dollars    DOUBLE      NULL,
    volume_24h                DOUBLE      NULL,
    volume                    DOUBLE      NULL,
    liquidity                 DOUBLE      NULL,
    open_interest             DOUBLE      NULL,
    created_at                TIMESTAMPTZ NULL,
    last_updated_at           TIMESTAMPTZ NULL,
    removed_at                TIMESTAMPTZ NULL
)
""".strip()


KALSHI_PRICE_SNAPSHOTS_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS kalshi_price_snapshots (
    {_PROVENANCE_COLUMNS},
    ticker                    VARCHAR     NOT NULL,
    snapshot_ts               TIMESTAMPTZ NOT NULL,
    yes_bid_dollars           DOUBLE      NULL,
    yes_ask_dollars           DOUBLE      NULL,
    mid_price_dollars         DOUBLE      NULL,
    last_trade_price_dollars  DOUBLE      NULL,
    last_trade_ts             TIMESTAMPTZ NULL,
    volume_24h                DOUBLE      NULL,
    volume_total              DOUBLE      NULL,
    open_interest             DOUBLE      NULL,
    liquidity                 DOUBLE      NULL,
    liquidity_warning         BOOLEAN     NOT NULL DEFAULT FALSE,
    spread_bps                INTEGER     NULL,
    snapshot_source           VARCHAR     NOT NULL,
    PRIMARY KEY (ticker, snapshot_ts)
)
""".strip()


KALSHI_ORDERBOOK_SNAPSHOTS_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS kalshi_orderbook_snapshots (
    {_PROVENANCE_COLUMNS},
    ticker                    VARCHAR     NOT NULL,
    snapshot_ts               TIMESTAMPTZ NOT NULL,
    side                      VARCHAR     NOT NULL,
    level                     INTEGER     NOT NULL,
    price_dollars             DOUBLE      NOT NULL,
    count_fp                  DOUBLE      NOT NULL,
    PRIMARY KEY (ticker, snapshot_ts, side, level)
)
""".strip()


KALSHI_TRADES_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS kalshi_trades (
    {_PROVENANCE_COLUMNS},
    trade_id                  VARCHAR     NOT NULL,
    ticker                    VARCHAR     NOT NULL,
    created_time              TIMESTAMPTZ NOT NULL,
    yes_price_dollars         DOUBLE      NOT NULL,
    no_price_dollars          DOUBLE      NOT NULL,
    count                     DOUBLE      NOT NULL,
    taker_side                VARCHAR     NULL,
    PRIMARY KEY (trade_id)
)
""".strip()


KALSHI_SETTLEMENTS_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS kalshi_settlements (
    {_PROVENANCE_COLUMNS},
    ticker                    VARCHAR     NOT NULL,
    event_ticker              VARCHAR     NOT NULL,
    series_ticker             VARCHAR     NOT NULL,
    result                    VARCHAR     NOT NULL,
    settled_value             DOUBLE      NULL,
    settlement_ts             TIMESTAMPTZ NOT NULL,
    settlement_source         TEXT        NULL,
    final_yes_price           DOUBLE      NULL,
    final_no_price            DOUBLE      NULL,
    total_volume_at_settlement DOUBLE     NULL,
    voided                    BOOLEAN     NOT NULL DEFAULT FALSE
)
""".strip()


# Single-row state table tracking the most recent /historical/cutoff
# response. Snapshotted at the start of every cycle (REQ-KSI-SETTLE-004).
# Routing decisions for settlement and trade backfills consult this
# row rather than re-fetching mid-cycle (OQ-KSI-004 resolution).
KALSHI_HISTORICAL_CUTOFF_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS kalshi_historical_cutoff (
    market_settled_ts         TIMESTAMPTZ NOT NULL,
    trades_created_ts         TIMESTAMPTZ NOT NULL,
    orders_updated_ts         TIMESTAMPTZ NOT NULL,
    fetched_at                TIMESTAMPTZ NOT NULL
)
""".strip()


KALSHI_SECTOR_MAPPING_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS kalshi_sector_mapping (
    ticker                    VARCHAR     PRIMARY KEY,
    razor_sector              VARCHAR     NULL,
    secondary_sectors         JSON        NULL,
    confidence                VARCHAR     NOT NULL,
    mapped_at                 TIMESTAMPTZ NOT NULL,
    mapped_by                 VARCHAR     NOT NULL
)
""".strip()


# ToS hash history, parallel to polymarket_tos_version_history. Lets
# the gate fall back to the last-known-good hash if Kalshi's ToS URL is
# briefly unreachable (T-KSI-021 / design §3.8).
KALSHI_TOS_VERSION_HISTORY_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS kalshi_tos_version_history (
    tos_version_hash          VARCHAR     PRIMARY KEY,
    tos_url                   VARCHAR     NOT NULL,
    first_seen_at             TIMESTAMPTZ NOT NULL,
    last_seen_at              TIMESTAMPTZ NOT NULL,
    notes                     TEXT        NULL
)
""".strip()


# Indexes kept separate so callers can apply them selectively. v1
# index set per design §3.3.
KALSHI_INDEXES_DDL: Final[tuple[str, ...]] = (
    # series
    "CREATE INDEX IF NOT EXISTS idx_kalshi_series_category ON kalshi_series (category)",
    "CREATE INDEX IF NOT EXISTS idx_kalshi_series_dedup "
    "ON kalshi_series (source_id, source_record_id, superseded_at)",
    # events
    "CREATE INDEX IF NOT EXISTS idx_kalshi_events_series ON kalshi_events (series_ticker)",
    "CREATE INDEX IF NOT EXISTS idx_kalshi_events_status ON kalshi_events (status)",
    "CREATE INDEX IF NOT EXISTS idx_kalshi_events_expected_expiration "
    "ON kalshi_events (expected_expiration_time)",
    "CREATE INDEX IF NOT EXISTS idx_kalshi_events_dedup "
    "ON kalshi_events (source_id, source_record_id, superseded_at)",
    # markets
    "CREATE INDEX IF NOT EXISTS idx_kalshi_markets_event ON kalshi_markets (event_ticker)",
    "CREATE INDEX IF NOT EXISTS idx_kalshi_markets_series ON kalshi_markets (series_ticker)",
    "CREATE INDEX IF NOT EXISTS idx_kalshi_markets_type_status "
    "ON kalshi_markets (market_type, status)",
    "CREATE INDEX IF NOT EXISTS idx_kalshi_markets_expiration ON kalshi_markets (expiration_time)",
    "CREATE INDEX IF NOT EXISTS idx_kalshi_markets_dedup "
    "ON kalshi_markets (source_id, source_record_id, superseded_at)",
    # price snapshots
    "CREATE INDEX IF NOT EXISTS idx_kalshi_price_ticker_ts "
    "ON kalshi_price_snapshots (ticker, snapshot_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_kalshi_price_ts ON kalshi_price_snapshots (snapshot_ts)",
    # orderbook snapshots
    "CREATE INDEX IF NOT EXISTS idx_kalshi_orderbook_ticker_ts "
    "ON kalshi_orderbook_snapshots (ticker, snapshot_ts DESC)",
    # trades
    "CREATE INDEX IF NOT EXISTS idx_kalshi_trades_ticker_time "
    "ON kalshi_trades (ticker, created_time DESC)",
    "CREATE INDEX IF NOT EXISTS idx_kalshi_trades_time ON kalshi_trades (created_time)",
    # settlements
    "CREATE INDEX IF NOT EXISTS idx_kalshi_settlements_ts ON kalshi_settlements (settlement_ts)",
    "CREATE INDEX IF NOT EXISTS idx_kalshi_settlements_series_ts "
    "ON kalshi_settlements (series_ticker, settlement_ts)",
    "CREATE INDEX IF NOT EXISTS idx_kalshi_settlements_dedup "
    "ON kalshi_settlements (source_id, source_record_id, superseded_at)",
    # sector mapping
    "CREATE INDEX IF NOT EXISTS idx_kalshi_sector_mapping_sector "
    "ON kalshi_sector_mapping (razor_sector)",
)


ALL_KALSHI_DDL: Final[tuple[str, ...]] = (
    KALSHI_SERIES_DDL,
    KALSHI_EVENTS_DDL,
    KALSHI_MARKETS_DDL,
    KALSHI_PRICE_SNAPSHOTS_DDL,
    KALSHI_ORDERBOOK_SNAPSHOTS_DDL,
    KALSHI_TRADES_DDL,
    KALSHI_SETTLEMENTS_DDL,
    KALSHI_HISTORICAL_CUTOFF_DDL,
    KALSHI_SECTOR_MAPPING_DDL,
    KALSHI_TOS_VERSION_HISTORY_DDL,
)


# Drop order is the inverse of create order. State tables drop first.
KALSHI_TABLE_NAMES: Final[tuple[str, ...]] = (
    "kalshi_tos_version_history",
    "kalshi_sector_mapping",
    "kalshi_historical_cutoff",
    "kalshi_settlements",
    "kalshi_trades",
    "kalshi_orderbook_snapshots",
    "kalshi_price_snapshots",
    "kalshi_markets",
    "kalshi_events",
    "kalshi_series",
)
