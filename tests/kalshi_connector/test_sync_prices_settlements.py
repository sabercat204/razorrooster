"""T-KSI-042 + T-KSI-043 — prices and settlements sync acceptance tests."""

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
    KalshiMarket,
    KalshiPaginatedResponse,
)
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)
from razor_rooster.kalshi_connector.persistence.source import (
    register_kalshi_sources,
)
from razor_rooster.kalshi_connector.sync.cutoff import snapshot_cutoff
from razor_rooster.kalshi_connector.sync.events import sync_events
from razor_rooster.kalshi_connector.sync.markets import sync_markets
from razor_rooster.kalshi_connector.sync.prices import snapshot_prices
from razor_rooster.kalshi_connector.sync.series import sync_series
from razor_rooster.kalshi_connector.sync.settlements import sync_settlements


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "kalshi_sync_prices.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_kalshi_migrations(conn)
        register_kalshi_sources(conn)
    yield s
    s.close()


# -- builders ------------------------------------------------------------


def _make_market(
    *,
    ticker: str = "KXFOO",
    market_type: str = "binary",
    yes_bid: float | None = 0.42,
    yes_ask: float | None = 0.45,
    last: float | None = 0.43,
    volume_24h: float | None = 10_000.0,
    liquidity: float | None = 5000.0,
    status: str = "open",
    result: str | None = None,
    expiration_time: datetime | None = None,
) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        event_ticker="EVT-1",
        series_ticker="INX",
        title=ticker,
        sub_title=None,
        market_type=market_type,  # type: ignore[arg-type]
        strike_type=None,
        floor_strike=None,
        cap_strike=None,
        open_time=None,
        close_time=None,
        expiration_time=expiration_time,
        expected_expiration_time=None,
        latest_expiration_time=None,
        settlement_timer_seconds=None,
        status=status,
        yes_sub_title=None,
        no_sub_title=None,
        result=result,
        can_close_early=None,
        expiration_value=None,
        category=None,
        risk_limit_cents=None,
        notional_value=None,
        tick_size=None,
        last_price_dollars=last,
        previous_yes_bid_dollars=yes_bid,
        previous_yes_ask_dollars=yes_ask,
        previous_price_dollars=None,
        volume_24h=volume_24h,
        volume=volume_24h,
        liquidity=liquidity,
        open_interest=None,
    )


# -- fakes ---------------------------------------------------------------


class _FakePerTickerClient:
    """Returns a fixed market per ticker for `get_market`."""

    def __init__(self, by_ticker: dict[str, KalshiMarket]) -> None:
        self._by_ticker = by_ticker
        self.calls: list[str] = []

    def get_market(self, ticker: str) -> KalshiMarket:
        self.calls.append(ticker)
        if ticker not in self._by_ticker:
            raise RuntimeError(f"unexpected ticker {ticker}")
        return self._by_ticker[ticker]


class _FakeSettlementsClient:
    """Returns settled markets for the live and historical paths."""

    def __init__(
        self,
        *,
        live_settled: list[KalshiMarket],
        historical: list[KalshiMarket],
        cutoff: KalshiHistoricalCutoff,
    ) -> None:
        self._live = live_settled
        self._historical = historical
        self._cutoff = cutoff

    def get_historical_cutoff(self) -> KalshiHistoricalCutoff:
        return self._cutoff

    def list_markets(
        self,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
        **_: object,
    ) -> KalshiPaginatedResponse:
        if status == "settled":
            return KalshiPaginatedResponse(items=list(self._live), cursor=None)
        return KalshiPaginatedResponse(items=[], cursor=None)

    def get_historical_markets(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        **_: object,
    ) -> KalshiPaginatedResponse:
        return KalshiPaginatedResponse(items=list(self._historical), cursor=None)


def _seed_active_market(
    store: DuckDBStore, *, ticker: str = "KXFOO", market_type: str = "binary"
) -> None:
    """Run series + events + markets sync to seed an active market."""
    from razor_rooster.kalshi_connector.client.models import (
        KalshiEvent,
        KalshiPaginatedResponse,
        KalshiSeries,
    )

    class _Series:
        def list_series(self, **_: object) -> KalshiPaginatedResponse:
            return KalshiPaginatedResponse(
                items=[KalshiSeries(series_ticker="INX", title="S&P")],
                cursor=None,
            )

    class _Events:
        def list_events(
            self,
            *,
            series_ticker: str | None = None,
            status: str | None = None,
            **_: object,
        ) -> KalshiPaginatedResponse:
            if series_ticker == "INX" and status == "open":
                return KalshiPaginatedResponse(
                    items=[
                        KalshiEvent(
                            event_ticker="EVT-1",
                            series_ticker="INX",
                            title="Test",
                            sub_title=None,
                            category=None,
                            mutually_exclusive=False,
                            expected_expiration_time=None,
                            strike_period=None,
                            status="open",
                        )
                    ],
                    cursor=None,
                )
            return KalshiPaginatedResponse(items=[], cursor=None)

    class _Markets:
        def list_markets(
            self,
            *,
            event_ticker: str | None = None,
            status: str | None = None,
            **_: object,
        ) -> KalshiPaginatedResponse:
            if event_ticker == "EVT-1" and status == "open":
                return KalshiPaginatedResponse(
                    items=[_make_market(ticker=ticker, market_type=market_type)],
                    cursor=None,
                )
            return KalshiPaginatedResponse(items=[], cursor=None)

    sync_series(store, client=_Series())  # type: ignore[arg-type]
    sync_events(store, client=_Events())  # type: ignore[arg-type]
    sync_markets(store, client=_Markets())  # type: ignore[arg-type]


# -- prices --------------------------------------------------------------


def test_prices_sync_inserts_snapshot_for_binary_market(store: DuckDBStore) -> None:
    _seed_active_market(store)
    fake = _FakePerTickerClient(
        # Tight spread (0.420 / 0.422 → 47 bps, under the 200 default).
        {"KXFOO": _make_market(ticker="KXFOO", yes_bid=0.420, yes_ask=0.422)}
    )
    report = snapshot_prices(store, client=fake)  # type: ignore[arg-type]
    assert report.markets_evaluated == 1
    assert report.snapshots_inserted == 1
    assert report.snapshots_thin_book == 0


def test_prices_sync_skips_non_binary_markets(store: DuckDBStore) -> None:
    _seed_active_market(store, ticker="KXSCAL", market_type="scalar")
    fake = _FakePerTickerClient({})  # no calls expected
    report = snapshot_prices(store, client=fake)  # type: ignore[arg-type]
    assert report.markets_skipped_non_binary == 1
    assert report.snapshots_inserted == 0
    assert fake.calls == []


def test_prices_sync_thin_book_flag(store: DuckDBStore) -> None:
    _seed_active_market(store)
    # Wide spread → thin-book warning.
    fake = _FakePerTickerClient({"KXFOO": _make_market(ticker="KXFOO", yes_bid=0.10, yes_ask=0.50)})
    report = snapshot_prices(store, client=fake)  # type: ignore[arg-type]
    assert report.snapshots_thin_book == 1


def test_prices_sync_null_preservation(store: DuckDBStore) -> None:
    """Missing yes_bid/ask leave fields NULL and trigger liquidity_warning."""
    _seed_active_market(store)
    fake = _FakePerTickerClient({"KXFOO": _make_market(ticker="KXFOO", yes_bid=None, yes_ask=None)})
    report = snapshot_prices(store, client=fake)  # type: ignore[arg-type]
    assert report.snapshots_thin_book == 1
    with store.connection() as conn:
        row = conn.execute(
            "SELECT yes_bid_dollars, yes_ask_dollars, mid_price_dollars, "
            "liquidity_warning FROM kalshi_price_snapshots WHERE ticker = 'KXFOO'"
        ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None
    assert row[3] is True


def test_prices_market_filter_restricts_evaluation(store: DuckDBStore) -> None:
    """When market_filter is supplied, only the listed tickers are evaluated."""
    # Seed both KXFOO and KXBAR via a single markets sync so neither
    # is marked removed.
    from razor_rooster.kalshi_connector.client.models import (
        KalshiEvent,
        KalshiPaginatedResponse,
        KalshiSeries,
    )

    class _Series:
        def list_series(self, **_: object) -> KalshiPaginatedResponse:
            return KalshiPaginatedResponse(
                items=[KalshiSeries(series_ticker="INX", title="S&P")],
                cursor=None,
            )

    class _Events:
        def list_events(
            self,
            *,
            series_ticker: str | None = None,
            status: str | None = None,
            **_: object,
        ) -> KalshiPaginatedResponse:
            if series_ticker == "INX" and status == "open":
                return KalshiPaginatedResponse(
                    items=[
                        KalshiEvent(
                            event_ticker="EVT-1",
                            series_ticker="INX",
                            title="Test",
                            sub_title=None,
                            category=None,
                            mutually_exclusive=False,
                            expected_expiration_time=None,
                            strike_period=None,
                            status="open",
                        )
                    ],
                    cursor=None,
                )
            return KalshiPaginatedResponse(items=[], cursor=None)

    class _Markets:
        def list_markets(
            self,
            *,
            event_ticker: str | None = None,
            status: str | None = None,
            **_: object,
        ) -> KalshiPaginatedResponse:
            if event_ticker == "EVT-1" and status == "open":
                return KalshiPaginatedResponse(
                    items=[
                        _make_market(ticker="KXFOO"),
                        _make_market(ticker="KXBAR"),
                    ],
                    cursor=None,
                )
            return KalshiPaginatedResponse(items=[], cursor=None)

    sync_series(store, client=_Series())  # type: ignore[arg-type]
    sync_events(store, client=_Events())  # type: ignore[arg-type]
    sync_markets(store, client=_Markets())  # type: ignore[arg-type]

    fake = _FakePerTickerClient(
        {
            "KXFOO": _make_market(ticker="KXFOO", yes_bid=0.42, yes_ask=0.422),
            "KXBAR": _make_market(ticker="KXBAR", yes_bid=0.51, yes_ask=0.512),
        }
    )
    report = snapshot_prices(store, client=fake, market_filter=["KXFOO"])  # type: ignore[arg-type]
    assert report.markets_evaluated == 1


# -- settlements --------------------------------------------------------


def test_settlements_sync_requires_cutoff_snapshot(store: DuckDBStore) -> None:
    """Without a snapshot, settlements sync returns an error and no calls are made."""

    class _NoCallClient:
        def list_markets(self, **_: object) -> KalshiPaginatedResponse:
            raise AssertionError("should not be called without cutoff snapshot")

        def get_historical_markets(self, **_: object) -> KalshiPaginatedResponse:
            raise AssertionError("should not be called without cutoff snapshot")

    report = sync_settlements(store, client=_NoCallClient())  # type: ignore[arg-type]
    assert report.errors
    assert any("kalshi_historical_cutoff" in e for e in report.errors)


def test_settlements_sync_writes_live_and_historical(store: DuckDBStore) -> None:
    cutoff = KalshiHistoricalCutoff(
        market_settled_ts=datetime(2026, 2, 15, tzinfo=UTC),
        trades_created_ts=datetime(2026, 2, 15, tzinfo=UTC),
        orders_updated_ts=datetime(2026, 2, 15, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 16, tzinfo=UTC),
    )
    settled_live = _make_market(
        ticker="KXLIVE",
        status="settled",
        result="yes",
        expiration_time=datetime(2026, 5, 1, tzinfo=UTC),
    )
    settled_old = _make_market(
        ticker="KXOLD",
        status="settled",
        result="yes",
        expiration_time=datetime(2025, 12, 1, tzinfo=UTC),
    )
    client = _FakeSettlementsClient(
        live_settled=[settled_live],
        historical=[settled_old],
        cutoff=cutoff,
    )
    snapshot_cutoff(store, client=client)  # type: ignore[arg-type]
    report = sync_settlements(store, client=client)  # type: ignore[arg-type]
    assert report.settlements_seen == 2
    assert report.settlements_inserted == 2
    assert report.routed_to_live == 1
    assert report.routed_to_historical == 1


def test_settlements_sync_voided_market_marked(store: DuckDBStore) -> None:
    cutoff = KalshiHistoricalCutoff(
        market_settled_ts=datetime(2026, 2, 15, tzinfo=UTC),
        trades_created_ts=datetime(2026, 2, 15, tzinfo=UTC),
        orders_updated_ts=datetime(2026, 2, 15, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 16, tzinfo=UTC),
    )
    settled_voided = _make_market(
        ticker="KXVOID",
        status="settled",
        result="void",
        expiration_time=datetime(2026, 5, 1, tzinfo=UTC),
    )
    client = _FakeSettlementsClient(
        live_settled=[settled_voided],
        historical=[],
        cutoff=cutoff,
    )
    snapshot_cutoff(store, client=client)  # type: ignore[arg-type]
    sync_settlements(store, client=client)  # type: ignore[arg-type]
    with store.connection() as conn:
        row = conn.execute(
            "SELECT voided FROM kalshi_settlements WHERE ticker = 'KXVOID'"
        ).fetchone()
    assert row is not None
    assert row[0] is True


def test_settlements_sync_idempotent(store: DuckDBStore) -> None:
    cutoff = KalshiHistoricalCutoff(
        market_settled_ts=datetime(2026, 2, 15, tzinfo=UTC),
        trades_created_ts=datetime(2026, 2, 15, tzinfo=UTC),
        orders_updated_ts=datetime(2026, 2, 15, tzinfo=UTC),
        fetched_at=datetime(2026, 5, 16, tzinfo=UTC),
    )
    settled = _make_market(
        ticker="KXLIVE",
        status="settled",
        result="yes",
        expiration_time=datetime(2026, 5, 1, tzinfo=UTC),
    )
    client = _FakeSettlementsClient(live_settled=[settled], historical=[], cutoff=cutoff)
    snapshot_cutoff(store, client=client)  # type: ignore[arg-type]
    sync_settlements(store, client=client)  # type: ignore[arg-type]
    second = sync_settlements(store, client=client)  # type: ignore[arg-type]
    assert second.settlements_inserted == 0
    assert second.settlements_unchanged == 1
