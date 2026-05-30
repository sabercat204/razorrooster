"""T-PMC-045 — on-demand orderbook fetch tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.polymarket_connector.client.clob_public import (
    CLOB_BASE_URL,
    ClobPublicClient,
)
from razor_rooster.polymarket_connector.client.rate_limit import (
    TokenBucket,
    reset_shared_bucket,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.source import (
    register_polymarket_sources,
)
from razor_rooster.polymarket_connector.sync.orderbook import (
    OrderbookFetchReport,
    fetch_orderbook,
)


@pytest.fixture(autouse=True)
def _isolated_bucket() -> Iterator[None]:
    reset_shared_bucket()
    yield
    reset_shared_bucket()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "polymarket_ob.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        register_polymarket_sources(conn)
    try:
        yield s
    finally:
        s.close()


def _build_client(handler: Callable[[httpx.Request], httpx.Response]) -> ClobPublicClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=CLOB_BASE_URL)
    bucket = TokenBucket(capacity=1000.0, refill_per_second=1000.0)
    return ClobPublicClient(http_client=http, bucket=bucket, max_retries=0)


_BOOK_RESPONSE = {
    "market": "0xmarket",
    "asset_id": "0xasset",
    "timestamp": "1700000000",
    "hash": "abc",
    "bids": [
        {"price": "0.45", "size": "100"},
        {"price": "0.44", "size": "200"},
    ],
    "asks": [
        {"price": "0.46", "size": "150"},
        {"price": "0.47", "size": "250"},
    ],
    "min_order_size": "1",
    "tick_size": "0.01",
    "neg_risk": False,
    "last_trade_price": "0.45",
}


def test_fetch_orderbook_default_does_not_persist(store: DuckDBStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_BOOK_RESPONSE)

    with _build_client(handler) as client:
        report = fetch_orderbook(
            client=client,
            condition_id="0xabc",
            outcome_token_id="0xabc-yes",
        )

    assert isinstance(report, OrderbookFetchReport)
    assert report.persisted is False
    assert report.persisted_levels == 0
    assert report.orderbook is not None

    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM polymarket_orderbook_snapshots").fetchone()
    assert row is not None
    assert row[0] == 0


def test_fetch_orderbook_persist_writes_one_row_per_level(store: DuckDBStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_BOOK_RESPONSE)

    with _build_client(handler) as client:
        report = fetch_orderbook(
            client=client,
            condition_id="0xabc",
            outcome_token_id="0xabc-yes",
            persist=True,
            store=store,
        )

    assert report.persisted is True
    assert report.persisted_levels == 4  # 2 bids + 2 asks

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT side, level, price, size FROM polymarket_orderbook_snapshots "
            "ORDER BY side, level"
        ).fetchall()

    assert len(rows) == 4
    asks = [r for r in rows if r[0] == "ask"]
    bids = [r for r in rows if r[0] == "bid"]
    assert len(bids) == 2
    assert len(asks) == 2
    # Best bid (level 0) should be 0.45.
    assert bids[0][1] == 0
    assert bids[0][2] == 0.45
    # Best ask (level 0) should be 0.46.
    assert asks[0][1] == 0
    assert asks[0][2] == 0.46


def test_fetch_orderbook_persist_idempotent(store: DuckDBStore) -> None:
    """Persisting the same orderbook at the same timestamp does not duplicate rows."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_BOOK_RESPONSE)

    when = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    with _build_client(handler) as client:
        fetch_orderbook(
            client=client,
            condition_id="0xabc",
            outcome_token_id="0xabc-yes",
            persist=True,
            store=store,
            now=when,
        )
        fetch_orderbook(
            client=client,
            condition_id="0xabc",
            outcome_token_id="0xabc-yes",
            persist=True,
            store=store,
            now=when,
        )

    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM polymarket_orderbook_snapshots").fetchone()
    assert row is not None
    assert row[0] == 4  # No duplicates.


def test_fetch_orderbook_404_returns_none_orderbook(store: DuckDBStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no such token"})

    with _build_client(handler) as client:
        report = fetch_orderbook(
            client=client,
            condition_id="0xabc",
            outcome_token_id="missing",
            persist=True,
            store=store,
        )

    assert report.orderbook is None
    assert report.persisted is False


def test_fetch_orderbook_persist_requires_store() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_BOOK_RESPONSE)

    with (
        _build_client(handler) as client,
        pytest.raises(ValueError, match="requires a DuckDBStore"),
    ):
        fetch_orderbook(
            client=client,
            condition_id="0xabc",
            outcome_token_id="0xabc-yes",
            persist=True,
            store=None,
        )


def test_fetch_orderbook_handles_network_error(store: DuckDBStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with _build_client(handler) as client:
        report = fetch_orderbook(
            client=client,
            condition_id="0xabc",
            outcome_token_id="0xabc-yes",
        )

    assert report.errors
    assert report.orderbook is None


def test_fetch_orderbook_records_timing(store: DuckDBStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_BOOK_RESPONSE)

    when = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    with _build_client(handler) as client:
        report = fetch_orderbook(
            client=client,
            condition_id="0xabc",
            outcome_token_id="0xabc-yes",
            now=when,
        )

    assert report.started_at == when
    assert report.completed_at is not None
    assert report.duration_seconds is not None


def test_fetch_orderbook_returns_typed_orderbook(store: DuckDBStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_BOOK_RESPONSE)

    with _build_client(handler) as client:
        report = fetch_orderbook(
            client=client,
            condition_id="0xabc",
            outcome_token_id="0xabc-yes",
        )

    ob = report.orderbook
    assert ob is not None
    assert ob.market == "0xmarket"
    assert ob.tick_size == 0.01
    assert ob.best_bid is not None
    assert ob.best_bid.price == 0.45


def test_fetch_orderbook_persist_thin_book_writes_what_exists(store: DuckDBStore) -> None:
    """A thin book (one side only) writes only the side that has levels."""
    payload = {
        "market": "0xmarket",
        "asset_id": "0xasset",
        "timestamp": "1700000000",
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [],
        "neg_risk": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with _build_client(handler) as client:
        report = fetch_orderbook(
            client=client,
            condition_id="0xabc",
            outcome_token_id="0xabc-yes",
            persist=True,
            store=store,
        )

    assert report.persisted is True
    assert report.persisted_levels == 1

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT side, COUNT(*) FROM polymarket_orderbook_snapshots GROUP BY side"
        ).fetchall()
    by_side = {r[0]: r[1] for r in rows}
    assert by_side == {"bid": 1}


def test_fetch_orderbook_persist_two_timestamps_writes_separate_rows(
    store: DuckDBStore,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_BOOK_RESPONSE)

    t1 = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    t2 = datetime(2026, 5, 14, 10, 0, tzinfo=UTC)
    with _build_client(handler) as client:
        fetch_orderbook(
            client=client,
            condition_id="0xabc",
            outcome_token_id="0xabc-yes",
            persist=True,
            store=store,
            now=t1,
        )
        fetch_orderbook(
            client=client,
            condition_id="0xabc",
            outcome_token_id="0xabc-yes",
            persist=True,
            store=store,
            now=t2,
        )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT snapshot_ts) FROM polymarket_orderbook_snapshots"
        ).fetchone()
    assert row is not None
    assert row[0] == 2
