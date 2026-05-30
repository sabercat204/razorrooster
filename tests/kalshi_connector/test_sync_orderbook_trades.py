"""T-KSI-044 + T-KSI-045 — orderbook + trades sync acceptance tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.kalshi_connector.client.models import (
    KalshiHistoricalCutoff,
    KalshiOrderbook,
    KalshiOrderbookLevel,
    KalshiPaginatedResponse,
    KalshiTrade,
)
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)
from razor_rooster.kalshi_connector.persistence.source import (
    register_kalshi_sources,
)
from razor_rooster.kalshi_connector.sync.cutoff import snapshot_cutoff
from razor_rooster.kalshi_connector.sync.orderbook import fetch_orderbook
from razor_rooster.kalshi_connector.sync.trades import sync_trades


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "kalshi_sync_ob.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_kalshi_migrations(conn)
        register_kalshi_sources(conn)
    yield s
    s.close()


# -- orderbook ----------------------------------------------------------


class _FakeOrderbookClient:
    def __init__(self, orderbook: KalshiOrderbook) -> None:
        self._orderbook = orderbook
        self.calls = 0

    def get_orderbook(self, ticker: str, *, depth: int = 10) -> KalshiOrderbook:
        self.calls += 1
        return self._orderbook


def test_orderbook_fetch_persists_yes_and_no_levels(store: DuckDBStore) -> None:
    snapshot_ts = datetime(2026, 5, 16, 12, tzinfo=UTC)
    orderbook = KalshiOrderbook(
        ticker="KXFOO",
        snapshot_ts=snapshot_ts,
        yes_levels=(
            KalshiOrderbookLevel(price_dollars=0.42, count=100.0),
            KalshiOrderbookLevel(price_dollars=0.45, count=50.0),
        ),
        no_levels=(
            KalshiOrderbookLevel(price_dollars=0.58, count=100.0),
            KalshiOrderbookLevel(price_dollars=0.55, count=50.0),
        ),
    )
    client = _FakeOrderbookClient(orderbook)
    report = fetch_orderbook(store, client=client, ticker="KXFOO")  # type: ignore[arg-type]
    assert report.yes_levels == 2
    assert report.no_levels == 2
    assert report.rows_inserted == 4
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT side, level, price_dollars, count_fp "
            "FROM kalshi_orderbook_snapshots WHERE ticker = 'KXFOO' "
            "ORDER BY side, level"
        ).fetchall()
    assert len(rows) == 4
    sides = {r[0] for r in rows}
    assert sides == {"yes", "no"}


def test_orderbook_fetch_handles_empty_levels(store: DuckDBStore) -> None:
    snapshot_ts = datetime(2026, 5, 16, 12, tzinfo=UTC)
    orderbook = KalshiOrderbook(
        ticker="KXEMPTY",
        snapshot_ts=snapshot_ts,
        yes_levels=(),
        no_levels=(),
    )
    client = _FakeOrderbookClient(orderbook)
    report = fetch_orderbook(store, client=client, ticker="KXEMPTY")  # type: ignore[arg-type]
    assert report.yes_levels == 0
    assert report.no_levels == 0
    assert report.rows_inserted == 0


def test_orderbook_fetch_idempotent(store: DuckDBStore) -> None:
    snapshot_ts = datetime(2026, 5, 16, 12, tzinfo=UTC)
    orderbook = KalshiOrderbook(
        ticker="KXFOO",
        snapshot_ts=snapshot_ts,
        yes_levels=(KalshiOrderbookLevel(price_dollars=0.42, count=100.0),),
        no_levels=(KalshiOrderbookLevel(price_dollars=0.58, count=100.0),),
    )
    fetch_orderbook(store, client=_FakeOrderbookClient(orderbook), ticker="KXFOO")  # type: ignore[arg-type]
    second = fetch_orderbook(  # type: ignore[arg-type]
        store, client=_FakeOrderbookClient(orderbook), ticker="KXFOO"
    )
    assert second.rows_inserted == 0
    assert second.rows_unchanged == 2


# -- trades --------------------------------------------------------------


class _FakeTradesClient:
    """Routes get_market_trades and get_historical_trades to fixed lists."""

    def __init__(
        self,
        *,
        live_by_ticker: dict[str, list[KalshiTrade]],
        historical_by_ticker: dict[str, list[KalshiTrade]],
        cutoff: KalshiHistoricalCutoff,
    ) -> None:
        self._live = live_by_ticker
        self._historical = historical_by_ticker
        self._cutoff = cutoff
        self.live_calls: list[str] = []
        self.historical_calls: list[str] = []

    def get_historical_cutoff(self) -> KalshiHistoricalCutoff:
        return self._cutoff

    def get_market_trades(
        self,
        ticker: str | None = None,
        *,
        cursor: str | None = None,
        limit: int = 100,
        **_: object,
    ) -> KalshiPaginatedResponse:
        if ticker is not None:
            self.live_calls.append(ticker)
        items = self._live.get(ticker or "", []) if ticker else []
        return KalshiPaginatedResponse(items=list(items), cursor=None)

    def get_historical_trades(
        self,
        *,
        ticker: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
        **_: object,
    ) -> KalshiPaginatedResponse:
        if ticker is not None:
            self.historical_calls.append(ticker)
        items = self._historical.get(ticker or "", []) if ticker else []
        return KalshiPaginatedResponse(items=list(items), cursor=None)


def _trade(
    *, trade_id: str, ticker: str, created_time: datetime, yes_price: float = 0.5
) -> KalshiTrade:
    return KalshiTrade(
        trade_id=trade_id,
        ticker=ticker,
        created_time=created_time,
        yes_price_dollars=yes_price,
        no_price_dollars=1.0 - yes_price,
        count=1.0,
        taker_side="yes",
    )


def _seed_cutoff(
    store: DuckDBStore,
    *,
    market_settled_ts: datetime,
    trades_created_ts: datetime,
) -> KalshiHistoricalCutoff:
    cutoff = KalshiHistoricalCutoff(
        market_settled_ts=market_settled_ts,
        trades_created_ts=trades_created_ts,
        orders_updated_ts=trades_created_ts,
        fetched_at=datetime(2026, 5, 16, tzinfo=UTC),
    )

    class _C:
        def get_historical_cutoff(self) -> KalshiHistoricalCutoff:
            return cutoff

    snapshot_cutoff(store, client=_C())  # type: ignore[arg-type]
    return cutoff


def test_trades_sync_no_cutoff_returns_error(store: DuckDBStore) -> None:
    """Trades sync requires the cutoff snapshot first."""

    class _NoCallClient:
        def get_market_trades(self, *_: object, **__: object) -> KalshiPaginatedResponse:
            raise AssertionError("should not be called without cutoff")

        def get_historical_trades(self, *_: object, **__: object) -> KalshiPaginatedResponse:
            raise AssertionError("should not be called without cutoff")

    report = sync_trades(  # type: ignore[arg-type]
        store, client=_NoCallClient(), watched_markets=["KXFOO"]
    )
    assert report.errors
    assert any("kalshi_historical_cutoff" in e for e in report.errors)


def test_trades_sync_routes_to_live_when_no_prior(store: DuckDBStore) -> None:
    """No prior trade for a ticker → routes to /markets/trades (live).

    With latest=None, the routing condition ``latest is None or latest >=
    cutoff`` evaluates to True, sending us to live.
    """
    cutoff = _seed_cutoff(
        store,
        market_settled_ts=datetime(2026, 2, 15, tzinfo=UTC),
        trades_created_ts=datetime(2026, 2, 15, tzinfo=UTC),
    )
    trade_a = _trade(
        trade_id="T-A",
        ticker="KXFOO",
        created_time=datetime(2026, 5, 1, tzinfo=UTC),
    )
    client = _FakeTradesClient(
        live_by_ticker={"KXFOO": [trade_a]},
        historical_by_ticker={},
        cutoff=cutoff,
    )
    report = sync_trades(store, client=client, watched_markets=["KXFOO"])  # type: ignore[arg-type]
    assert report.routed_live == 1
    assert report.trades_inserted == 1
    assert "KXFOO" in client.live_calls
    assert client.historical_calls == []


def test_trades_sync_routes_to_historical_when_old_watermark(store: DuckDBStore) -> None:
    """latest < cutoff → routes to /historical/trades."""
    cutoff = _seed_cutoff(
        store,
        market_settled_ts=datetime(2026, 4, 1, tzinfo=UTC),
        trades_created_ts=datetime(2026, 4, 1, tzinfo=UTC),
    )
    # Pre-seed a stale trade so latest < cutoff.
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
                "T-OLD",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 1, tzinfo=UTC),
                "kalshi@0.1.0",
                '{"k":"v"}',
                "T-OLD",
                "KXFOO",
                datetime(2026, 1, 1, tzinfo=UTC),
                0.5,
                0.5,
                1.0,
                "yes",
            ],
        )
    trade_h = _trade(
        trade_id="T-H",
        ticker="KXFOO",
        created_time=datetime(2026, 2, 1, tzinfo=UTC),
    )
    client = _FakeTradesClient(
        live_by_ticker={},
        historical_by_ticker={"KXFOO": [trade_h]},
        cutoff=cutoff,
    )
    report = sync_trades(store, client=client, watched_markets=["KXFOO"])  # type: ignore[arg-type]
    assert report.routed_historical == 1
    assert client.historical_calls == ["KXFOO"]
    assert client.live_calls == []


def test_trades_sync_idempotent_via_watermark(store: DuckDBStore) -> None:
    """Re-running with the same input is a no-op (watermark filters)."""
    cutoff = _seed_cutoff(
        store,
        market_settled_ts=datetime(2026, 2, 15, tzinfo=UTC),
        trades_created_ts=datetime(2026, 2, 15, tzinfo=UTC),
    )
    trade_a = _trade(
        trade_id="T-A",
        ticker="KXFOO",
        created_time=datetime(2026, 5, 1, tzinfo=UTC),
    )
    client_a = _FakeTradesClient(
        live_by_ticker={"KXFOO": [trade_a]},
        historical_by_ticker={},
        cutoff=cutoff,
    )
    sync_trades(store, client=client_a, watched_markets=["KXFOO"])  # type: ignore[arg-type]
    # Second run sees the same trade — but watermark filters it.
    client_b = _FakeTradesClient(
        live_by_ticker={"KXFOO": [trade_a]},
        historical_by_ticker={},
        cutoff=cutoff,
    )
    report = sync_trades(store, client=client_b, watched_markets=["KXFOO"])  # type: ignore[arg-type]
    assert report.trades_seen == 0
    assert report.trades_inserted == 0


def test_trades_sync_empty_watched_list_returns_empty(store: DuckDBStore) -> None:
    cutoff = _seed_cutoff(
        store,
        market_settled_ts=datetime(2026, 2, 15, tzinfo=UTC),
        trades_created_ts=datetime(2026, 2, 15, tzinfo=UTC),
    )

    class _C:
        def get_market_trades(self, *_: object, **__: object) -> KalshiPaginatedResponse:
            raise AssertionError("should not be called")

        def get_historical_trades(self, *_: object, **__: object) -> KalshiPaginatedResponse:
            raise AssertionError("should not be called")

        def get_historical_cutoff(self) -> KalshiHistoricalCutoff:
            return cutoff

    report = sync_trades(store, client=_C(), watched_markets=[])  # type: ignore[arg-type]
    assert report.tickers_evaluated == 0
    assert report.trades_seen == 0
