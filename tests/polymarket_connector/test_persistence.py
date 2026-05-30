"""T-PMC-010, T-PMC-011, T-PMC-012 — Polymarket persistence layer tests.

Verifies:
- All seven Polymarket-namespace tables apply via the migration.
- The migration runner is idempotent (second open is a no-op).
- A round-trip insert/select works for each table preserving the
  provenance prefix.
- Source registration writes the two Polymarket source rows and the
  ``freshness`` view picks them up.
"""

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
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.schemas import (
    POLYMARKET_TABLE_NAMES,
)
from razor_rooster.polymarket_connector.persistence.source import (
    POLYMARKET_LIVE_SOURCE_ID,
    POLYMARKET_RESOLUTIONS_SOURCE_ID,
    register_polymarket_sources,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "polymarket_persistence.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        # data_ingest migrations first — they create schema_migrations
        # and the canonical schemas. Polymarket migrations run after.
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
    try:
        yield s
    finally:
        s.close()


def test_polymarket_migration_creates_all_tables(store: DuckDBStore) -> None:
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name LIKE 'polymarket_%'"
        ).fetchall()
    table_names = {r[0] for r in rows}
    for expected in POLYMARKET_TABLE_NAMES:
        assert expected in table_names, f"missing Polymarket table: {expected}"


def test_polymarket_migration_records_version_1001(store: DuckDBStore) -> None:
    with store.connection() as conn:
        versions = applied_versions(conn)
    assert 1001 in versions


def test_polymarket_migration_is_idempotent(store: DuckDBStore) -> None:
    with store.connection() as conn:
        before = applied_versions(conn)
        applied_again = run_pending_polymarket_migrations(conn)
        after = applied_versions(conn)
    assert applied_again == ()
    assert before == after


def test_polymarket_markets_round_trip(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            """
            INSERT INTO polymarket_markets (
                source_id, source_record_id, source_publication_ts, fetch_ts,
                connector_version, source_payload_json, superseded_at,
                condition_id, slug, question, description, category,
                subcategory, tags, event_id, market_type, outcome_tokens,
                end_date, active, closed, resolved, volume_lifetime,
                created_at_polymarket, last_updated_polymarket, removed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "polymarket",
                "0xabcdef",
                now,
                now,
                "polymarket@0.1.0",
                json.dumps({"raw": "verbatim"}),
                None,
                "0xabcdef",
                "will-x-happen-by-y",
                "Will X happen by Y?",
                None,
                "Politics",
                "Elections",
                json.dumps(["election", "us"]),
                "evt-1",
                "binary",
                json.dumps(
                    [
                        {"token_id": "token_yes", "outcome_label": "Yes"},
                        {"token_id": "token_no", "outcome_label": "No"},
                    ]
                ),
                None,
                True,
                False,
                False,
                None,
                None,
                now,
                None,
            ],
        )
        row = conn.execute(
            "SELECT condition_id, market_type, active FROM polymarket_markets "
            "WHERE source_record_id = ?",
            ["0xabcdef"],
        ).fetchone()

    assert row is not None
    assert row[0] == "0xabcdef"
    assert row[1] == "binary"
    assert row[2] is True


def test_polymarket_price_snapshot_round_trip_preserves_nulls(store: DuckDBStore) -> None:
    """REQ-PMC-PRICE-004 — thin orderbooks preserve NULLs and set liquidity_warning."""
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            """
            INSERT INTO polymarket_price_snapshots (
                source_id, source_record_id, source_publication_ts, fetch_ts,
                connector_version, source_payload_json, superseded_at,
                condition_id, outcome_token_id, snapshot_ts, mid_price,
                best_bid, best_ask, last_trade_price, last_trade_ts,
                volume_24h, liquidity_warning, spread_bps, snapshot_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "polymarket",
                "snap-1",
                now,
                now,
                "polymarket@0.1.0",
                json.dumps({"raw": "thin"}),
                None,
                "0xabcdef",
                "token_yes",
                now,
                None,
                None,
                None,
                None,
                None,
                None,
                True,
                None,
                "rest",
            ],
        )
        row = conn.execute(
            "SELECT mid_price, best_bid, best_ask, liquidity_warning, snapshot_source "
            "FROM polymarket_price_snapshots WHERE source_record_id = ?",
            ["snap-1"],
        ).fetchone()

    assert row is not None
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None
    assert row[3] is True
    assert row[4] == "rest"


def test_polymarket_resolutions_round_trip(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            """
            INSERT INTO polymarket_resolutions (
                source_id, source_record_id, source_publication_ts, fetch_ts,
                connector_version, source_payload_json, superseded_at,
                condition_id, winning_outcome_token_id, winning_outcome_label,
                resolution_ts, resolution_source, resolution_metadata,
                final_yes_price, final_no_price, total_volume_at_resolution,
                invalidated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "polymarket_resolutions",
                "res-1",
                now,
                now,
                "polymarket@0.1.0",
                json.dumps({"raw": "settled"}),
                None,
                "0xabcdef",
                "token_yes",
                "Yes",
                now,
                "uma_oracle",
                json.dumps({"oracle_request_hash": "0xfeed"}),
                0.97,
                0.03,
                12345.6,
                False,
            ],
        )
        row = conn.execute(
            "SELECT winning_outcome_label, final_yes_price, invalidated "
            "FROM polymarket_resolutions WHERE source_record_id = ?",
            ["res-1"],
        ).fetchone()

    assert row is not None
    assert row[0] == "Yes"
    assert row[1] == 0.97
    assert row[2] is False


def test_polymarket_sector_mapping_round_trip(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as conn:
        conn.execute(
            """
            INSERT INTO polymarket_sector_mapping (
                condition_id, razor_sector, secondary_sectors, confidence,
                mapped_at, mapped_by
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                "0xabcdef",
                "geopolitical",
                json.dumps(["regulatory"]),
                "manual",
                now,
                "operator",
            ],
        )
        row = conn.execute(
            "SELECT razor_sector, confidence, mapped_by FROM polymarket_sector_mapping "
            "WHERE condition_id = ?",
            ["0xabcdef"],
        ).fetchone()

    assert row is not None
    assert row[0] == "geopolitical"
    assert row[1] == "manual"
    assert row[2] == "operator"


def test_register_polymarket_sources_creates_two_rows(store: DuckDBStore) -> None:
    with store.connection() as conn:
        register_polymarket_sources(conn)
        rows = conn.execute(
            "SELECT source_id, license, freshness_threshold_seconds "
            "FROM sources WHERE source_id LIKE 'polymarket%' ORDER BY source_id"
        ).fetchall()
    by_id = {r[0]: r for r in rows}
    assert POLYMARKET_LIVE_SOURCE_ID in by_id
    assert POLYMARKET_RESOLUTIONS_SOURCE_ID in by_id
    # OQ-PMC-007 thresholds.
    assert by_id[POLYMARKET_LIVE_SOURCE_ID][2] == 21_600
    assert by_id[POLYMARKET_RESOLUTIONS_SOURCE_ID][2] == 172_800
    # Both carry the Polymarket Terms-versioned license.
    for row in rows:
        assert row[1] == "POLYMARKET_TERMS_VERSIONED"


def test_register_polymarket_sources_is_idempotent(store: DuckDBStore) -> None:
    with store.connection() as conn:
        register_polymarket_sources(conn)
        register_polymarket_sources(conn)
        rows = conn.execute(
            "SELECT source_id FROM sources WHERE source_id LIKE 'polymarket%'"
        ).fetchall()
    assert len(rows) == 2  # no duplicates


def test_freshness_view_picks_up_polymarket_sources(store: DuckDBStore) -> None:
    with store.connection() as conn:
        register_polymarket_sources(conn)
        freshness_rows = query_freshness(conn)
    by_id = {r.source_id: r for r in freshness_rows}
    assert POLYMARKET_LIVE_SOURCE_ID in by_id
    assert POLYMARKET_RESOLUTIONS_SOURCE_ID in by_id
    # Never-fetched sources are stale.
    assert by_id[POLYMARKET_LIVE_SOURCE_ID].is_stale is True
    assert by_id[POLYMARKET_RESOLUTIONS_SOURCE_ID].is_stale is True


def test_polymarket_indexes_exist(store: DuckDBStore) -> None:
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name LIKE 'polymarket_%'"
        ).fetchall()
    index_names = {r[0] for r in rows}
    # Spot-check a few index names; the full set is in
    # POLYMARKET_INDEXES_DDL but DuckDB sometimes synthesizes index names
    # for primary keys, so we only assert the named ones we created
    # explicitly.
    expected_named_indexes = {
        "idx_polymarket_markets_active_end_date",
        "idx_polymarket_markets_category",
        "idx_polymarket_markets_market_type",
        "idx_polymarket_markets_event_id",
        "idx_polymarket_price_condition_ts",
        "idx_polymarket_price_ts",
        "idx_polymarket_orderbook_condition_ts",
        "idx_polymarket_trades_condition_ts",
        "idx_polymarket_resolutions_ts",
        "idx_polymarket_sector_mapping_sector",
    }
    missing = expected_named_indexes - index_names
    assert not missing, f"missing indexes: {missing}"
