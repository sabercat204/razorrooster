"""T-KSI-041 — series + events + markets sync acceptance tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.kalshi_connector.client.models import (
    KalshiEvent,
    KalshiMarket,
    KalshiPaginatedResponse,
    KalshiSeries,
)
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)
from razor_rooster.kalshi_connector.persistence.source import (
    register_kalshi_sources,
)
from razor_rooster.kalshi_connector.sync.events import sync_events
from razor_rooster.kalshi_connector.sync.markets import sync_markets
from razor_rooster.kalshi_connector.sync.series import sync_series


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "kalshi_sync.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_kalshi_migrations(conn)
        register_kalshi_sources(conn)
    yield s
    s.close()


# -- fakes ---------------------------------------------------------------


class _FakeSeriesClient:
    def __init__(self, items: list[KalshiSeries]) -> None:
        self._items = items

    def list_series(self, **_: object) -> KalshiPaginatedResponse:
        return KalshiPaginatedResponse(items=self._items, cursor=None)


class _FakeEventsClient:
    def __init__(self, items_per_request: dict[str, list[KalshiEvent]]) -> None:
        # Key the response on (series_ticker, status); the test loops
        # series_tickers x statuses and wires keys accordingly.
        self._by_key = items_per_request

    def list_events(
        self,
        *,
        series_ticker: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> KalshiPaginatedResponse:
        key = f"{series_ticker}|{status}"
        items = self._by_key.get(key, [])
        return KalshiPaginatedResponse(items=items, cursor=None)


class _FakeMarketsClient:
    def __init__(self, items_per_request: dict[str, list[KalshiMarket]]) -> None:
        self._by_key = items_per_request

    def list_markets(
        self,
        *,
        event_ticker: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
        **_: object,
    ) -> KalshiPaginatedResponse:
        key = f"{event_ticker}|{status}"
        items = self._by_key.get(key, [])
        return KalshiPaginatedResponse(items=items, cursor=None)


# -- builders ------------------------------------------------------------


def _series(ticker: str = "INX", title: str = "S&P 500") -> KalshiSeries:
    return KalshiSeries(series_ticker=ticker, title=title, category="Markets")


def _event(
    *,
    event_ticker: str = "EVT-1",
    series_ticker: str = "INX",
    status: str = "open",
) -> KalshiEvent:
    return KalshiEvent(
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        title="Test Event",
        sub_title=None,
        category=None,
        mutually_exclusive=False,
        expected_expiration_time=None,
        strike_period=None,
        status=status,
    )


def _market(
    *,
    ticker: str,
    event_ticker: str = "EVT-1",
    series_ticker: str = "INX",
    market_type: str = "binary",
    status: str = "open",
) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        title=f"Market {ticker}",
        sub_title=None,
        market_type=market_type,  # type: ignore[arg-type]
        strike_type=None,
        floor_strike=None,
        cap_strike=None,
        open_time=None,
        close_time=None,
        expiration_time=None,
        expected_expiration_time=None,
        latest_expiration_time=None,
        settlement_timer_seconds=None,
        status=status,
        yes_sub_title=None,
        no_sub_title=None,
        result=None,
        can_close_early=None,
        expiration_value=None,
        category=None,
        risk_limit_cents=None,
        notional_value=None,
        tick_size=None,
        last_price_dollars=None,
        previous_yes_bid_dollars=None,
        previous_yes_ask_dollars=None,
        previous_price_dollars=None,
        volume_24h=None,
        volume=None,
        liquidity=None,
        open_interest=None,
    )


# -- series sync --------------------------------------------------------


def test_series_sync_inserts_new_rows(store: DuckDBStore) -> None:
    client = _FakeSeriesClient([_series("INX"), _series("CPI", "CPI")])
    report = sync_series(store, client=client)  # type: ignore[arg-type]
    assert report.series_total_seen == 2
    assert report.series_inserted == 2
    assert report.series_removed == 0


def test_series_sync_marks_missing_as_removed(store: DuckDBStore) -> None:
    """Second cycle without an old series sets removed_at on the prior row."""
    sync_series(store, client=_FakeSeriesClient([_series("INX"), _series("CPI")]))  # type: ignore[arg-type]
    sync_series(store, client=_FakeSeriesClient([_series("INX")]))  # type: ignore[arg-type]
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT series_ticker, removed_at FROM kalshi_series WHERE series_ticker = 'CPI'"
        ).fetchall()
    assert any(r[1] is not None for r in rows)


def test_series_sync_idempotent(store: DuckDBStore) -> None:
    """Two consecutive identical syncs leave 'unchanged' counts on the second."""
    sync_series(store, client=_FakeSeriesClient([_series("INX")]))  # type: ignore[arg-type]
    report = sync_series(store, client=_FakeSeriesClient([_series("INX")]))  # type: ignore[arg-type]
    assert report.series_inserted == 0
    assert report.series_unchanged == 1
    assert report.series_removed == 0


# -- events sync --------------------------------------------------------


def test_events_sync_inserts_per_series(store: DuckDBStore) -> None:
    # Seed an active series so the events pull has something to scan.
    sync_series(store, client=_FakeSeriesClient([_series("INX")]))  # type: ignore[arg-type]
    client = _FakeEventsClient(
        {
            "INX|open": [_event(event_ticker="EVT-OPEN")],
            "INX|closed": [],
            "INX|settled": [_event(event_ticker="EVT-SETTLED", status="settled")],
        }
    )
    report = sync_events(store, client=client)  # type: ignore[arg-type]
    assert report.events_total_seen == 2
    assert report.events_inserted == 2


def test_events_sync_marks_missing_as_removed(store: DuckDBStore) -> None:
    sync_series(store, client=_FakeSeriesClient([_series("INX")]))  # type: ignore[arg-type]
    sync_events(
        store,
        client=_FakeEventsClient(  # type: ignore[arg-type]
            {
                "INX|open": [_event(event_ticker="EVT-1"), _event(event_ticker="EVT-2")],
                "INX|closed": [],
                "INX|settled": [],
            }
        ),
    )
    sync_events(
        store,
        client=_FakeEventsClient(  # type: ignore[arg-type]
            {
                "INX|open": [_event(event_ticker="EVT-1")],
                "INX|closed": [],
                "INX|settled": [],
            }
        ),
    )
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT event_ticker, removed_at FROM kalshi_events WHERE event_ticker = 'EVT-2'"
        ).fetchall()
    assert any(r[1] is not None for r in rows)


def test_events_sync_no_active_series_returns_empty(store: DuckDBStore) -> None:
    """With no active series, the sync does nothing and reports zero counts."""
    client = _FakeEventsClient({})
    report = sync_events(store, client=client)  # type: ignore[arg-type]
    assert report.events_total_seen == 0
    assert report.events_inserted == 0


# -- markets sync ------------------------------------------------------


def test_markets_sync_inserts_all_market_types(store: DuckDBStore) -> None:
    """All four market types round-trip through the sync."""
    sync_series(store, client=_FakeSeriesClient([_series("INX")]))  # type: ignore[arg-type]
    sync_events(
        store,
        client=_FakeEventsClient(  # type: ignore[arg-type]
            {
                "INX|open": [_event(event_ticker="EVT-1")],
                "INX|closed": [],
                "INX|settled": [],
            }
        ),
    )
    client = _FakeMarketsClient(
        {
            "EVT-1|open": [
                _market(ticker="KX-BIN", market_type="binary"),
                _market(ticker="KX-SCAL", market_type="scalar"),
                _market(ticker="KX-CAT", market_type="categorical"),
            ]
        }
    )
    report = sync_markets(store, client=client)  # type: ignore[arg-type]
    assert report.markets_total_seen == 3
    assert report.markets_inserted == 3
    assert report.market_type_counts == {"binary": 1, "scalar": 1, "categorical": 1}


def test_markets_sync_idempotent(store: DuckDBStore) -> None:
    sync_series(store, client=_FakeSeriesClient([_series("INX")]))  # type: ignore[arg-type]
    sync_events(
        store,
        client=_FakeEventsClient(  # type: ignore[arg-type]
            {
                "INX|open": [_event(event_ticker="EVT-1")],
                "INX|closed": [],
                "INX|settled": [],
            }
        ),
    )
    client = _FakeMarketsClient({"EVT-1|open": [_market(ticker="KXFOO")]})
    sync_markets(store, client=client)  # type: ignore[arg-type]
    report = sync_markets(store, client=client)  # type: ignore[arg-type]
    assert report.markets_inserted == 0
    assert report.markets_unchanged == 1


def test_markets_sync_marks_missing_as_removed(store: DuckDBStore) -> None:
    sync_series(store, client=_FakeSeriesClient([_series("INX")]))  # type: ignore[arg-type]
    sync_events(
        store,
        client=_FakeEventsClient(  # type: ignore[arg-type]
            {
                "INX|open": [_event(event_ticker="EVT-1")],
                "INX|closed": [],
                "INX|settled": [],
            }
        ),
    )
    sync_markets(
        store,
        client=_FakeMarketsClient(  # type: ignore[arg-type]
            {
                "EVT-1|open": [
                    _market(ticker="KXFOO"),
                    _market(ticker="KXBAR"),
                ]
            }
        ),
    )
    sync_markets(
        store,
        client=_FakeMarketsClient(  # type: ignore[arg-type]
            {"EVT-1|open": [_market(ticker="KXFOO")]}
        ),
    )
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT ticker, removed_at FROM kalshi_markets WHERE ticker = 'KXBAR'"
        ).fetchall()
    assert any(r[1] is not None for r in rows)


def test_markets_sync_no_events_returns_empty(store: DuckDBStore) -> None:
    """Without active events, the markets sync does nothing."""
    report = sync_markets(store, client=_FakeMarketsClient({}))  # type: ignore[arg-type]
    assert report.markets_total_seen == 0


def test_markets_sync_explicit_event_filter(store: DuckDBStore) -> None:
    """When event_tickers is supplied, the sync uses it directly."""
    client = _FakeMarketsClient({"EVT-X|open": [_market(ticker="KX-ONLY-X")]})
    report = sync_markets(store, client=client, event_tickers=["EVT-X"])  # type: ignore[arg-type]
    assert report.markets_total_seen == 1
    assert report.markets_inserted == 1
