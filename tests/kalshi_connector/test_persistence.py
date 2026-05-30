"""T-KSI-010 / T-KSI-011 / T-KSI-012 — schema + migration + source-registration tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    applied_versions,
)
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.data_ingest.persistence.provenance import query_freshness
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)
from razor_rooster.kalshi_connector.persistence.schemas import (
    KALSHI_TABLE_NAMES,
)
from razor_rooster.kalshi_connector.persistence.source import (
    KALSHI_LIVE_SOURCE_ID,
    KALSHI_SETTLEMENTS_SOURCE_ID,
    register_kalshi_sources,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "kalshi.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_kalshi_migrations(conn)
    yield s
    s.close()


# --- T-KSI-010 / T-KSI-011: schema + migration ---------------------------


def test_kalshi_migration_creates_all_tables(store: DuckDBStore) -> None:
    with store.connection() as conn:
        rows = conn.execute("SELECT name FROM (SHOW TABLES) WHERE name LIKE 'kalshi_%'").fetchall()
    actual = {r[0] for r in rows}
    assert actual == set(KALSHI_TABLE_NAMES)


def test_kalshi_migration_records_version_8001(store: DuckDBStore) -> None:
    with store.connection() as conn:
        versions = applied_versions(conn)
    assert 8001 in versions


def test_kalshi_migration_is_idempotent(store: DuckDBStore) -> None:
    with store.connection() as conn:
        before = applied_versions(conn)
        run_pending_kalshi_migrations(conn)
        after = applied_versions(conn)
    assert before == after


def test_kalshi_indexes_exist(store: DuckDBStore) -> None:
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name LIKE 'kalshi_%'"
        ).fetchall()
    names = {r[0] for r in rows}
    expected_subset = {
        "idx_kalshi_series_category",
        "idx_kalshi_events_series",
        "idx_kalshi_markets_event",
        "idx_kalshi_markets_type_status",
        "idx_kalshi_price_ticker_ts",
        "idx_kalshi_orderbook_ticker_ts",
        "idx_kalshi_trades_ticker_time",
        "idx_kalshi_settlements_series_ts",
        "idx_kalshi_sector_mapping_sector",
    }
    assert expected_subset.issubset(names)


# --- round-trip tests for each table -------------------------------------


def test_kalshi_series_round_trip(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_series ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "series_ticker, title, category, frequency, tags, "
            "settlement_source, contract_url, created_at, last_updated_at, "
            "removed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            [
                "kalshi",
                "KXCPI",
                now,
                now,
                "0.1",
                json.dumps({"raw": "synthetic"}),
                "KXCPI",
                "Highest CPI print this month",
                "Economics",
                "monthly",
                json.dumps(["macro", "inflation"]),
                "BLS CPI release",
                "https://kalshi.com/markets/kxcpi",
                now,
                now,
            ],
        )
        row = conn.execute(
            "SELECT series_ticker, title, frequency, settlement_source FROM kalshi_series"
        ).fetchone()
    assert row is not None
    assert row[0] == "KXCPI"
    assert row[1] == "Highest CPI print this month"
    assert row[2] == "monthly"
    assert row[3] == "BLS CPI release"


def test_kalshi_events_round_trip(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_events ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "event_ticker, series_ticker, title, sub_title, category, "
            "mutually_exclusive, expected_expiration_time, strike_period, "
            "status, created_at, last_updated_at, removed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            [
                "kalshi",
                "KXCPI-26AUG",
                now,
                now,
                "0.1",
                json.dumps({}),
                "KXCPI-26AUG",
                "KXCPI",
                "August 2026 CPI",
                "monthly print",
                "Economics",
                True,
                datetime(2026, 9, 1, tzinfo=UTC),
                "monthly",
                "open",
                now,
                now,
            ],
        )
        row = conn.execute(
            "SELECT event_ticker, series_ticker, mutually_exclusive, status FROM kalshi_events"
        ).fetchone()
    assert row is not None
    assert row[0] == "KXCPI-26AUG"
    assert row[1] == "KXCPI"
    assert row[2] is True
    assert row[3] == "open"


def test_kalshi_markets_round_trip_binary(store: DuckDBStore) -> None:
    """A binary 'above' strike-variant market round-trips with all type fields."""
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        # Use a focused INSERT that exercises the type/strike fields and
        # a representative subset of price/volume fields. Other columns
        # accept NULL, so omitting them keeps the test readable.
        conn.execute(
            "INSERT INTO kalshi_markets ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, "
            "ticker, event_ticker, series_ticker, title, "
            "market_type, strike_type, floor_strike, "
            "open_time, close_time, expiration_time, "
            "status, last_price_dollars, volume_24h, "
            "created_at, last_updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "kalshi",
                "KXCPI-26AUG-T2.5",
                now,
                now,
                "0.1",
                json.dumps({}),
                "KXCPI-26AUG-T2.5",
                "KXCPI-26AUG",
                "KXCPI",
                "August CPI above 2.5%",
                "binary",
                "above",
                2.5,
                datetime(2026, 8, 1, tzinfo=UTC),
                datetime(2026, 9, 1, tzinfo=UTC),
                datetime(2026, 9, 1, tzinfo=UTC),
                "open",
                0.42,
                25000.0,
                now,
                now,
            ],
        )
        row = conn.execute(
            "SELECT ticker, market_type, strike_type, floor_strike FROM kalshi_markets"
        ).fetchone()
    assert row is not None
    assert row[0] == "KXCPI-26AUG-T2.5"
    assert row[1] == "binary"
    assert row[2] == "above"
    assert row[3] == pytest.approx(2.5)


def test_kalshi_markets_round_trip_scalar(store: DuckDBStore) -> None:
    """OQ-KSI-003: scalar markets round-trip faithfully even though v1
    doesn't surface them downstream."""
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_markets ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "ticker, event_ticker, series_ticker, title, market_type, "
            "strike_type, floor_strike, cap_strike, status, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            [
                "kalshi",
                "KXSCALAR-1",
                now,
                now,
                "0.1",
                json.dumps({}),
                "KXSCALAR-1",
                "KXSCALAR-EVT",
                "KXSCALAR",
                "synthetic scalar",
                "scalar",
                0.0,
                100.0,
                "open",
                now,
            ],
        )
        row = conn.execute(
            "SELECT market_type, floor_strike, cap_strike FROM kalshi_markets"
        ).fetchone()
    assert row is not None
    assert row[0] == "scalar"
    assert row[1] == pytest.approx(0.0)
    assert row[2] == pytest.approx(100.0)


def test_kalshi_price_snapshot_round_trip_preserves_nulls(store: DuckDBStore) -> None:
    """REQ-KSI-PRICE-004: thin orderbooks preserve NULLs and set liquidity_warning."""
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_price_snapshots ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "ticker, snapshot_ts, yes_bid_dollars, yes_ask_dollars, "
            "mid_price_dollars, last_trade_price_dollars, last_trade_ts, "
            "volume_24h, volume_total, open_interest, liquidity, "
            "liquidity_warning, spread_bps, snapshot_source"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "kalshi",
                "snap-thin-1",
                now,
                now,
                "0.1",
                json.dumps({}),
                "KXTHIN-1",
                now,
                None,  # yes_bid missing
                None,  # yes_ask missing
                None,  # mid_price unobservable
                None,
                None,
                None,
                None,
                None,
                None,
                True,  # liquidity_warning
                None,
                "rest",
            ],
        )
        row = conn.execute(
            "SELECT yes_bid_dollars, yes_ask_dollars, mid_price_dollars, "
            "liquidity_warning, snapshot_source FROM kalshi_price_snapshots"
        ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None
    assert row[3] is True
    assert row[4] == "rest"


def test_kalshi_orderbook_round_trip_yes_only(store: DuckDBStore) -> None:
    """Design §3.3: orderbook stores YES side only; NO derived by callers."""
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_orderbook_snapshots ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "ticker, snapshot_ts, side, level, price_dollars, count_fp"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
            [
                "kalshi",
                "ob-1",
                now,
                now,
                "0.1",
                json.dumps({}),
                "KXTEST",
                now,
                "yes_bid",
                0,
                0.42,
                100.0,
            ],
        )
        row = conn.execute(
            "SELECT side, level, price_dollars FROM kalshi_orderbook_snapshots"
        ).fetchone()
    assert row is not None
    assert row[0] == "yes_bid"
    assert row[1] == 0
    assert row[2] == pytest.approx(0.42)


def test_kalshi_trades_round_trip_with_no_derivation(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_trades ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "trade_id, ticker, created_time, yes_price_dollars, "
            "no_price_dollars, count, taker_side"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
            [
                "kalshi",
                "trade-1",
                now,
                now,
                "0.1",
                json.dumps({}),
                "trade-1",
                "KXTEST",
                now,
                0.42,
                0.58,
                10.0,
                "yes",
            ],
        )
        row = conn.execute(
            "SELECT trade_id, yes_price_dollars, no_price_dollars, taker_side FROM kalshi_trades"
        ).fetchone()
    assert row is not None
    assert row[0] == "trade-1"
    assert row[1] == pytest.approx(0.42)
    assert row[2] == pytest.approx(0.58)
    # Sanity: yes + no should sum to ~1.0 for this contract.
    assert row[1] + row[2] == pytest.approx(1.0)
    assert row[3] == "yes"


def test_kalshi_settlements_round_trip(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_settlements ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "ticker, event_ticker, series_ticker, result, settled_value, "
            "settlement_ts, settlement_source, final_yes_price, final_no_price, "
            "total_volume_at_settlement, voided"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "kalshi_settlements",
                "settle-1",
                now,
                now,
                "0.1",
                json.dumps({}),
                "KXCPI-26AUG-T2.5",
                "KXCPI-26AUG",
                "KXCPI",
                "yes",
                None,
                now,
                "BLS CPI release Aug-2026",
                1.0,
                0.0,
                500000.0,
                False,
            ],
        )
        row = conn.execute(
            "SELECT ticker, result, settlement_source, voided FROM kalshi_settlements"
        ).fetchone()
    assert row is not None
    assert row[0] == "KXCPI-26AUG-T2.5"
    assert row[1] == "yes"
    assert row[2] == "BLS CPI release Aug-2026"
    assert row[3] is False


def test_kalshi_historical_cutoff_single_row_replace(store: DuckDBStore) -> None:
    """OQ-KSI-004: cutoff snapshot is single-row state, upserted per cycle."""
    t1 = datetime(2026, 5, 15, 8, tzinfo=UTC)
    t2 = datetime(2026, 5, 16, 8, tzinfo=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_historical_cutoff "
            "(market_settled_ts, trades_created_ts, orders_updated_ts, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            [t1, t1, t1, t1],
        )
        # Subsequent cycles replace by deleting first.
        conn.execute("DELETE FROM kalshi_historical_cutoff")
        conn.execute(
            "INSERT INTO kalshi_historical_cutoff "
            "(market_settled_ts, trades_created_ts, orders_updated_ts, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            [t2, t2, t2, t2],
        )
        rows = conn.execute(
            "SELECT market_settled_ts, fetched_at FROM kalshi_historical_cutoff"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == t2
    assert rows[0][1] == t2


def test_kalshi_sector_mapping_round_trip(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_sector_mapping "
            "(ticker, razor_sector, secondary_sectors, confidence, mapped_at, mapped_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                "KXCPI-26AUG-T2.5",
                "macroeconomic",
                json.dumps([]),
                "inferred",
                now,
                "heuristic_v1",
            ],
        )
        row = conn.execute(
            "SELECT ticker, razor_sector, confidence FROM kalshi_sector_mapping"
        ).fetchone()
    assert row is not None
    assert row[0] == "KXCPI-26AUG-T2.5"
    assert row[1] == "macroeconomic"
    assert row[2] == "inferred"


def test_kalshi_sector_mapping_supports_out_of_scope(store: DuckDBStore) -> None:
    """OQ-KSI-001: out_of_scope is a valid razor_sector value."""
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_sector_mapping "
            "(ticker, razor_sector, confidence, mapped_at, mapped_by) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                "KXNFLSB-26",
                "out_of_scope",
                "inferred",
                now,
                "heuristic_v1",
            ],
        )
        row = conn.execute(
            "SELECT razor_sector FROM kalshi_sector_mapping WHERE ticker = ?",
            ["KXNFLSB-26"],
        ).fetchone()
    assert row is not None
    assert row[0] == "out_of_scope"


def test_kalshi_tos_version_history_round_trip(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_tos_version_history "
            "(tos_version_hash, tos_url, first_seen_at, last_seen_at, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                "abc123",
                "https://kalshi.com/docs/kalshi-terms-of-service",
                now,
                now,
                "initial seed",
            ],
        )
        row = conn.execute(
            "SELECT tos_version_hash, tos_url FROM kalshi_tos_version_history"
        ).fetchone()
    assert row is not None
    assert row[0] == "abc123"
    assert row[1].endswith("kalshi-terms-of-service")


# --- T-KSI-012: source registration + freshness participation ------------


def test_register_kalshi_sources_creates_two_rows(store: DuckDBStore) -> None:
    with store.connection() as conn:
        register_kalshi_sources(conn)
        ids = conn.execute("SELECT source_id FROM sources ORDER BY source_id").fetchall()
    actual = {r[0] for r in ids}
    assert KALSHI_LIVE_SOURCE_ID in actual
    assert KALSHI_SETTLEMENTS_SOURCE_ID in actual


def test_register_kalshi_sources_is_idempotent(store: DuckDBStore) -> None:
    with store.connection() as conn:
        register_kalshi_sources(conn)
        register_kalshi_sources(conn)
        rows = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE source_id LIKE 'kalshi%'"
        ).fetchone()
    assert rows is not None
    assert rows[0] == 2


def test_register_kalshi_sources_thresholds_match_design(store: DuckDBStore) -> None:
    """Design §4: 3h prices, 48h settlements."""
    with store.connection() as conn:
        register_kalshi_sources(conn)
        live = conn.execute(
            "SELECT freshness_threshold_seconds FROM sources WHERE source_id = ?",
            [KALSHI_LIVE_SOURCE_ID],
        ).fetchone()
        settle = conn.execute(
            "SELECT freshness_threshold_seconds FROM sources WHERE source_id = ?",
            [KALSHI_SETTLEMENTS_SOURCE_ID],
        ).fetchone()
    assert live is not None and live[0] == 10_800
    assert settle is not None and settle[0] == 172_800


def test_freshness_view_picks_up_kalshi_sources(store: DuckDBStore) -> None:
    with store.connection() as conn:
        register_kalshi_sources(conn)
        rows = query_freshness(conn)
    source_ids = {r.source_id for r in rows}
    assert KALSHI_LIVE_SOURCE_ID in source_ids
    assert KALSHI_SETTLEMENTS_SOURCE_ID in source_ids
    # Both are stale on first registration (no last_successful_fetch yet).
    for r in rows:
        if r.source_id in (KALSHI_LIVE_SOURCE_ID, KALSHI_SETTLEMENTS_SOURCE_ID):
            assert r.is_stale is True
