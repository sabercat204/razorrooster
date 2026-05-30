"""Polymarket-namespaced table DDL (T-PMC-010; design §3.3).

Every Polymarket-sourced row carries the provenance prefix from
``data_ingest`` design §4 (source_id, source_record_id, fetch_ts,
connector_version, source_payload_json, superseded_at,
source_publication_ts). The table-specific columns layer on top of that
prefix.

These are not canonical schemas in the ``data_ingest`` sense — they live
under the ``polymarket_*`` namespace and are specific to this subsystem.
``data_ingest``'s four canonical schemas (event_stream, time_series,
document_docket, geospatial_indicator) are unchanged; the requirement
REQ-EXT-002 (no new canonical schema in v1) is honored.

All timestamp columns use ``TIMESTAMPTZ`` per ``data_ingest`` REQ-NORM-002.
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


POLYMARKET_MARKETS_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS polymarket_markets (
    {_PROVENANCE_COLUMNS},
    condition_id              VARCHAR     NOT NULL,
    slug                      VARCHAR     NOT NULL,
    question                  TEXT        NOT NULL,
    description               TEXT        NULL,
    category                  VARCHAR     NULL,
    subcategory               VARCHAR     NULL,
    tags                      JSON        NULL,
    event_id                  VARCHAR     NULL,
    market_type               VARCHAR     NOT NULL,
    outcome_tokens            JSON        NOT NULL,
    end_date                  TIMESTAMPTZ NULL,
    active                    BOOLEAN     NOT NULL,
    closed                    BOOLEAN     NOT NULL,
    resolved                  BOOLEAN     NOT NULL,
    volume_lifetime           DOUBLE      NULL,
    created_at_polymarket     TIMESTAMPTZ NULL,
    last_updated_polymarket   TIMESTAMPTZ NULL,
    removed_at                TIMESTAMPTZ NULL
)
""".strip()


POLYMARKET_PRICE_SNAPSHOTS_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS polymarket_price_snapshots (
    {_PROVENANCE_COLUMNS},
    condition_id              VARCHAR     NOT NULL,
    outcome_token_id          VARCHAR     NOT NULL,
    snapshot_ts               TIMESTAMPTZ NOT NULL,
    mid_price                 DOUBLE      NULL,
    best_bid                  DOUBLE      NULL,
    best_ask                  DOUBLE      NULL,
    last_trade_price          DOUBLE      NULL,
    last_trade_ts             TIMESTAMPTZ NULL,
    volume_24h                DOUBLE      NULL,
    liquidity_warning         BOOLEAN     NOT NULL DEFAULT FALSE,
    spread_bps                INTEGER     NULL,
    snapshot_source           VARCHAR     NOT NULL,
    PRIMARY KEY (condition_id, outcome_token_id, snapshot_ts)
)
""".strip()


POLYMARKET_ORDERBOOK_SNAPSHOTS_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS polymarket_orderbook_snapshots (
    {_PROVENANCE_COLUMNS},
    condition_id              VARCHAR     NOT NULL,
    outcome_token_id          VARCHAR     NOT NULL,
    snapshot_ts               TIMESTAMPTZ NOT NULL,
    side                      VARCHAR     NOT NULL,
    level                     INTEGER     NOT NULL,
    price                     DOUBLE      NOT NULL,
    size                      DOUBLE      NOT NULL,
    PRIMARY KEY (condition_id, outcome_token_id, snapshot_ts, side, level)
)
""".strip()


POLYMARKET_TRADES_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS polymarket_trades (
    {_PROVENANCE_COLUMNS},
    condition_id              VARCHAR     NOT NULL,
    outcome_token_id          VARCHAR     NOT NULL,
    trade_ts                  TIMESTAMPTZ NOT NULL,
    price                     DOUBLE      NOT NULL,
    size                      DOUBLE      NOT NULL,
    side                      VARCHAR     NULL,
    tx_hash                   VARCHAR     NOT NULL,
    PRIMARY KEY (tx_hash, outcome_token_id)
)
""".strip()


POLYMARKET_RESOLUTIONS_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS polymarket_resolutions (
    {_PROVENANCE_COLUMNS},
    condition_id              VARCHAR     NOT NULL,
    winning_outcome_token_id  VARCHAR     NULL,
    winning_outcome_label     VARCHAR     NULL,
    resolution_ts             TIMESTAMPTZ NOT NULL,
    resolution_source         VARCHAR     NOT NULL,
    resolution_metadata       JSON        NULL,
    final_yes_price           DOUBLE      NULL,
    final_no_price            DOUBLE      NULL,
    total_volume_at_resolution DOUBLE     NULL,
    invalidated               BOOLEAN     NOT NULL DEFAULT FALSE
)
""".strip()


POLYMARKET_SECTOR_MAPPING_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS polymarket_sector_mapping (
    condition_id              VARCHAR     PRIMARY KEY,
    razor_sector              VARCHAR     NULL,
    secondary_sectors         JSON        NULL,
    confidence                VARCHAR     NOT NULL,
    mapped_at                 TIMESTAMPTZ NOT NULL,
    mapped_by                 VARCHAR     NOT NULL
)
""".strip()


# T-PMC-021: ToS hash + acknowledgement is recorded on the existing
# data_ingest sources table (license_terms_hash, license_acknowledged_at).
# We additionally keep a small history table so the gate can fall back to
# the last-known-good hash if the canonical ToS URL is briefly unreachable.
POLYMARKET_TOS_VERSION_HISTORY_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS polymarket_tos_version_history (
    tos_version_hash          VARCHAR     PRIMARY KEY,
    tos_url                   VARCHAR     NOT NULL,
    first_seen_at             TIMESTAMPTZ NOT NULL,
    last_seen_at              TIMESTAMPTZ NOT NULL,
    notes                     TEXT        NULL
)
""".strip()


# Indexes are kept separate so callers can apply them selectively (e.g. a
# pure migration applies all DDL + all indexes; a test fixture might skip
# the indexes for speed).
POLYMARKET_INDEXES_DDL: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS idx_polymarket_markets_active_end_date "
    "ON polymarket_markets (active, end_date)",
    "CREATE INDEX IF NOT EXISTS idx_polymarket_markets_category ON polymarket_markets (category)",
    "CREATE INDEX IF NOT EXISTS idx_polymarket_markets_market_type "
    "ON polymarket_markets (market_type)",
    "CREATE INDEX IF NOT EXISTS idx_polymarket_markets_event_id ON polymarket_markets (event_id)",
    "CREATE INDEX IF NOT EXISTS idx_polymarket_markets_dedup "
    "ON polymarket_markets (source_id, source_record_id, superseded_at)",
    "CREATE INDEX IF NOT EXISTS idx_polymarket_price_condition_ts "
    "ON polymarket_price_snapshots (condition_id, snapshot_ts)",
    "CREATE INDEX IF NOT EXISTS idx_polymarket_price_ts "
    "ON polymarket_price_snapshots (snapshot_ts)",
    "CREATE INDEX IF NOT EXISTS idx_polymarket_orderbook_condition_ts "
    "ON polymarket_orderbook_snapshots (condition_id, snapshot_ts)",
    "CREATE INDEX IF NOT EXISTS idx_polymarket_trades_condition_ts "
    "ON polymarket_trades (condition_id, trade_ts)",
    "CREATE INDEX IF NOT EXISTS idx_polymarket_resolutions_dedup "
    "ON polymarket_resolutions (source_id, source_record_id, superseded_at)",
    "CREATE INDEX IF NOT EXISTS idx_polymarket_resolutions_ts "
    "ON polymarket_resolutions (resolution_ts)",
    "CREATE INDEX IF NOT EXISTS idx_polymarket_sector_mapping_sector "
    "ON polymarket_sector_mapping (razor_sector)",
)


ALL_POLYMARKET_DDL: Final[tuple[str, ...]] = (
    POLYMARKET_MARKETS_DDL,
    POLYMARKET_PRICE_SNAPSHOTS_DDL,
    POLYMARKET_ORDERBOOK_SNAPSHOTS_DDL,
    POLYMARKET_TRADES_DDL,
    POLYMARKET_RESOLUTIONS_DDL,
    POLYMARKET_SECTOR_MAPPING_DDL,
    POLYMARKET_TOS_VERSION_HISTORY_DDL,
)


# Drop order is the inverse of create order — and we drop indexes first
# to avoid orphaned-index complaints in DuckDB versions that surface them.
POLYMARKET_TABLE_NAMES: Final[tuple[str, ...]] = (
    "polymarket_tos_version_history",
    "polymarket_sector_mapping",
    "polymarket_resolutions",
    "polymarket_trades",
    "polymarket_orderbook_snapshots",
    "polymarket_price_snapshots",
    "polymarket_markets",
)
