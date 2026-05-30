"""T-PMC-044 — watched-markets trade pull tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from razor_rooster.polymarket_connector.client.gamma import (
    GAMMA_BASE_URL,
    GammaClient,
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
from razor_rooster.polymarket_connector.sync.markets import sync_markets
from razor_rooster.polymarket_connector.sync.trades import (
    TradePullReport,
    pull_watched_trades,
)


@pytest.fixture(autouse=True)
def _isolated_bucket() -> Iterator[None]:
    reset_shared_bucket()
    yield
    reset_shared_bucket()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "polymarket_trades.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        register_polymarket_sources(conn)
    try:
        yield s
    finally:
        s.close()


def _build_clob_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> ClobPublicClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=CLOB_BASE_URL)
    bucket = TokenBucket(capacity=1000.0, refill_per_second=1000.0)
    return ClobPublicClient(http_client=http, bucket=bucket, max_retries=0)


def _build_gamma_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> GammaClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=GAMMA_BASE_URL)
    bucket = TokenBucket(capacity=1000.0, refill_per_second=1000.0)
    return GammaClient(http_client=http, bucket=bucket, max_retries=0)


def _seed_market(store: DuckDBStore, condition_id: str, *, resolved: bool = False) -> None:
    payload: dict[str, Any] = {
        "conditionId": condition_id,
        "slug": f"market-{condition_id[2:6]}",
        "question": f"Will {condition_id[2:6]} happen?",
        "active": not resolved,
        "closed": resolved,
        "resolved": resolved,
        "outcomes": ["Yes", "No"],
        "clobTokenIds": [f"{condition_id}-yes", f"{condition_id}-no"],
        "category": "Politics",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-05-14T08:00:00Z",
    }

    def gamma_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/markets":
            return httpx.Response(404)
        active_param = request.url.params.get("active")
        closed_param = request.url.params.get("closed")
        if resolved and closed_param == "true":
            return httpx.Response(200, json=[payload])
        if (not resolved) and active_param == "true" and closed_param == "false":
            return httpx.Response(200, json=[payload])
        return httpx.Response(200, json=[])

    with _build_gamma_client(gamma_handler) as client:
        sync_markets(store, client=client, now=datetime(2026, 5, 14, tzinfo=UTC))


def _trade_payload(
    *,
    tx_hash: str,
    asset_id: str,
    market: str,
    price: float = 0.5,
    size: float = 100.0,
    side: str = "BUY",
    ts: int = 1_700_000_000,
) -> dict[str, Any]:
    return {
        "tx_hash": tx_hash,
        "market": market,
        "asset_id": asset_id,
        "price": str(price),
        "size": str(size),
        "side": side,
        "trade_ts": ts,
    }


def _trade_handler(
    market_to_trades: dict[str, list[dict[str, Any]]],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/trades":
            return httpx.Response(404)
        market = request.url.params.get("market", "")
        trades = market_to_trades.get(market, [])
        return httpx.Response(200, json=trades)

    return handler


# -------------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------------
def test_pull_watched_trades_no_watched_markets_is_clean_noop(store: DuckDBStore) -> None:
    handler = _trade_handler({})
    with _build_clob_client(handler) as client:
        report = pull_watched_trades(store, client=client, watched_markets=[])

    assert isinstance(report, TradePullReport)
    assert report.markets_evaluated == 0
    assert report.trades_inserted == 0
    assert report.errors == []


def test_pull_watched_trades_unknown_market_skipped(store: DuckDBStore) -> None:
    """A watched market that doesn't exist locally is skipped with a warning."""
    handler = _trade_handler({})
    with _build_clob_client(handler) as client:
        report = pull_watched_trades(store, client=client, watched_markets=["0xnotseen"])

    assert report.markets_evaluated == 1
    assert report.markets_skipped_unknown == 1
    assert report.trades_inserted == 0


def test_pull_watched_trades_inserts_records(store: DuckDBStore) -> None:
    _seed_market(store, "0xabc")
    trades_payload = [
        _trade_payload(tx_hash="0xtx1", asset_id="0xabc-yes", market="0xabc"),
        _trade_payload(tx_hash="0xtx2", asset_id="0xabc-no", market="0xabc"),
    ]
    handler = _trade_handler({"0xabc": trades_payload})

    with _build_clob_client(handler) as client:
        report = pull_watched_trades(store, client=client, watched_markets=["0xabc"])

    assert report.trades_inserted == 2
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT condition_id, tx_hash, outcome_token_id, price "
            "FROM polymarket_trades ORDER BY tx_hash"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == "0xtx1"
    assert rows[0][2] == "0xabc-yes"
    assert rows[0][3] == 0.5


def test_pull_watched_trades_dedup_on_re_pull(store: DuckDBStore) -> None:
    """Re-pulling the same trades does not insert duplicates."""
    _seed_market(store, "0xabc")
    trade = _trade_payload(tx_hash="0xtx1", asset_id="0xabc-yes", market="0xabc")
    handler = _trade_handler({"0xabc": [trade]})

    with _build_clob_client(handler) as client:
        first = pull_watched_trades(store, client=client, watched_markets=["0xabc"])
        second = pull_watched_trades(store, client=client, watched_markets=["0xabc"])

    assert first.trades_inserted == 1
    assert second.trades_inserted == 0
    assert second.trades_unchanged == 1
    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM polymarket_trades").fetchone()
    assert row is not None
    assert row[0] == 1


def test_pull_watched_trades_skips_resolved_markets_count(store: DuckDBStore) -> None:
    """A resolved market is still pulled but counted in markets_skipped_resolved."""
    _seed_market(store, "0xabc", resolved=True)
    handler = _trade_handler({"0xabc": []})

    with _build_clob_client(handler) as client:
        report = pull_watched_trades(store, client=client, watched_markets=["0xabc"])

    assert report.markets_skipped_resolved == 1
    assert report.markets_evaluated == 1


def test_pull_watched_trades_per_market_failure_isolated(store: DuckDBStore) -> None:
    _seed_market(store, "0xok")
    _seed_market(store, "0xbad")

    def handler(request: httpx.Request) -> httpx.Response:
        market = request.url.params.get("market", "")
        if market == "0xbad":
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200,
            json=[_trade_payload(tx_hash="0xtxok", asset_id="0xok-yes", market="0xok")],
        )

    with _build_clob_client(handler) as client:
        report = pull_watched_trades(store, client=client, watched_markets=["0xok", "0xbad"])

    assert report.markets_evaluated == 2
    assert len(report.market_errors) == 1
    failed_id, _ = report.market_errors[0]
    assert failed_id == "0xbad"
    assert report.trades_inserted == 1


def test_pull_watched_trades_handles_envelope_with_cursor(store: DuckDBStore) -> None:
    """The CLOB sometimes returns trades in an envelope with a next_cursor."""
    _seed_market(store, "0xabc")
    page1 = [
        _trade_payload(tx_hash=f"0xtx{i}", asset_id="0xabc-yes", market="0xabc") for i in range(3)
    ]
    page2 = [
        _trade_payload(tx_hash=f"0xtx{i}", asset_id="0xabc-yes", market="0xabc")
        for i in range(3, 5)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("next_cursor", "")
        if cursor == "":
            return httpx.Response(200, json={"data": page1, "next_cursor": "p2"})
        if cursor == "p2":
            return httpx.Response(200, json={"data": page2, "next_cursor": None})
        return httpx.Response(200, json={"data": []})

    with _build_clob_client(handler) as client:
        report = pull_watched_trades(store, client=client, watched_markets=["0xabc"])

    assert report.trades_inserted == 5


def test_pull_watched_trades_per_market_cap_flag(store: DuckDBStore) -> None:
    """When the per-market trade cap is hit, the report flags it."""
    _seed_market(store, "0xabc")
    trades = [
        _trade_payload(tx_hash=f"0xtx{i}", asset_id="0xabc-yes", market="0xabc") for i in range(20)
    ]
    handler = _trade_handler({"0xabc": trades})

    with _build_clob_client(handler) as client:
        report = pull_watched_trades(
            store,
            client=client,
            watched_markets=["0xabc"],
            trades_per_market=5,
        )

    assert report.trades_capped_at_limit == 1
    assert report.trades_inserted == 5


def test_pull_watched_trades_records_timing(store: DuckDBStore) -> None:
    handler = _trade_handler({})
    when = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    with _build_clob_client(handler) as client:
        report = pull_watched_trades(store, client=client, watched_markets=[], now=when)

    assert report.started_at == when
    assert report.completed_at is not None
    assert report.duration_seconds is not None


def test_pull_watched_trades_skips_trades_missing_dedup_key(store: DuckDBStore) -> None:
    """Trades missing tx_hash or asset_id are dropped silently."""
    _seed_market(store, "0xabc")
    bad = {"market": "0xabc", "price": "0.5", "size": "1"}  # no tx_hash, no asset_id
    good = _trade_payload(tx_hash="0xgood", asset_id="0xabc-yes", market="0xabc")
    handler = _trade_handler({"0xabc": [bad, good]})

    with _build_clob_client(handler) as client:
        report = pull_watched_trades(store, client=client, watched_markets=["0xabc"])

    assert report.trades_inserted == 1
    with store.connection() as conn:
        row = conn.execute("SELECT tx_hash FROM polymarket_trades").fetchone()
    assert row is not None
    assert row[0] == "0xgood"


def test_pull_watched_trades_preserves_raw_payload(store: DuckDBStore) -> None:
    _seed_market(store, "0xabc")
    payload = _trade_payload(tx_hash="0xtx1", asset_id="0xabc-yes", market="0xabc")
    payload["custom_field"] = "operator-tag"
    handler = _trade_handler({"0xabc": [payload]})

    with _build_clob_client(handler) as client:
        pull_watched_trades(store, client=client, watched_markets=["0xabc"])

    with store.connection() as conn:
        row = conn.execute(
            "SELECT source_payload_json FROM polymarket_trades WHERE tx_hash = ?",
            ["0xtx1"],
        ).fetchone()
    assert row is not None
    import json

    raw = json.loads(row[0])
    assert raw["custom_field"] == "operator-tag"
