"""T-KSI-051 — markets sync wires sector heuristic side-effect.

Verifies the integration between :func:`sync_markets` and the sector
mapper persistence:

- When ``sector_keywords`` is supplied, every upstream market gets a
  ``kalshi_sector_mapping`` row.
- Heuristic upsert preserves an existing manual override.
- ``MarketsSyncReport`` records ``mappings_upserted`` and
  ``mappings_skipped_manual`` counts.
- When ``sector_keywords`` is None (default), no mapping rows are
  written.
"""

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
from razor_rooster.kalshi_connector.config.loader import (
    KalshiSectorKeywordsConfig,
)
from razor_rooster.kalshi_connector.mapping.sector_overrides import (
    get_mapping,
    set_override,
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
    db_path = tmp_path / "kalshi_markets_with_mapping.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_kalshi_migrations(conn)
        register_kalshi_sources(conn)
    yield s
    s.close()


@pytest.fixture
def keywords() -> KalshiSectorKeywordsConfig:
    return KalshiSectorKeywordsConfig(
        version=1,
        sectors={
            "macroeconomic": ["CPI", "Fed", "GDP"],
            "commodity": ["oil", "wheat"],
            "regulatory": ["FDA", "Congress"],
            "climate": ["hurricane"],
            "geopolitical": ["election"],
            "public_health": ["vaccine"],
            "infrastructure_energy": ["pipeline"],
            "cross_cutting": ["global"],
            "out_of_scope": ["NFL"],
        },
    )


def _market(
    *,
    ticker: str,
    title: str = "",
    market_type: str = "binary",
    category: str | None = None,
) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        event_ticker="EVT-1",
        series_ticker="INX",
        title=title,
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
        status="open",
        yes_sub_title=None,
        no_sub_title=None,
        result=None,
        can_close_early=None,
        expiration_value=None,
        category=category,
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


class _FakeSeriesClient:
    def list_series(self, **_: object) -> KalshiPaginatedResponse:
        return KalshiPaginatedResponse(
            items=[KalshiSeries(series_ticker="INX", title="S&P 500")],
            cursor=None,
        )


class _FakeEventsClient:
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
                        title="Test event",
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


class _FakeMarketsClient:
    def __init__(self, markets: list[KalshiMarket]) -> None:
        self._markets = markets

    def list_markets(
        self,
        *,
        event_ticker: str | None = None,
        status: str | None = None,
        **_: object,
    ) -> KalshiPaginatedResponse:
        if event_ticker == "EVT-1" and status == "open":
            return KalshiPaginatedResponse(items=list(self._markets), cursor=None)
        return KalshiPaginatedResponse(items=[], cursor=None)


def _seed_series_and_events(store: DuckDBStore) -> None:
    sync_series(store, client=_FakeSeriesClient())  # type: ignore[arg-type]
    sync_events(store, client=_FakeEventsClient())  # type: ignore[arg-type]


def test_markets_sync_with_keywords_writes_sector_mappings(
    store: DuckDBStore,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    _seed_series_and_events(store)
    markets = [
        _market(ticker="KX-CPI", title="CPI above 2.5%"),
        _market(ticker="KX-OIL", title="WTI oil above $80"),
        _market(ticker="KX-NFL", title="NFL season opener"),
    ]
    report = sync_markets(  # type: ignore[arg-type]
        store,
        client=_FakeMarketsClient(markets),
        sector_keywords=keywords,
    )
    assert report.mappings_upserted == 3
    assert report.mappings_skipped_manual == 0
    with store.connection() as conn:
        cpi = get_mapping(conn, "KX-CPI")
        oil = get_mapping(conn, "KX-OIL")
        nfl = get_mapping(conn, "KX-NFL")
    assert cpi is not None and cpi.razor_sector == "macroeconomic"
    assert oil is not None and oil.razor_sector == "commodity"
    assert nfl is not None and nfl.razor_sector == "out_of_scope"


def test_markets_sync_preserves_manual_override(
    store: DuckDBStore,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    _seed_series_and_events(store)
    # Operator sets a manual override before the heuristic runs.
    with store.connection() as conn:
        set_override(conn, ticker="KX-CPI", razor_sector="regulatory")
    markets = [_market(ticker="KX-CPI", title="CPI above 2.5%")]
    report = sync_markets(  # type: ignore[arg-type]
        store,
        client=_FakeMarketsClient(markets),
        sector_keywords=keywords,
    )
    # The heuristic would have suggested macroeconomic; manual wins.
    assert report.mappings_upserted == 0
    assert report.mappings_skipped_manual == 1
    with store.connection() as conn:
        row = get_mapping(conn, "KX-CPI")
    assert row is not None
    assert row.razor_sector == "regulatory"
    assert row.confidence == "manual"


def test_markets_sync_no_keywords_skips_mapping(store: DuckDBStore) -> None:
    """Without ``sector_keywords``, no mapping rows are written."""
    _seed_series_and_events(store)
    markets = [_market(ticker="KX-CPI", title="CPI above 2.5%")]
    report = sync_markets(  # type: ignore[arg-type]
        store,
        client=_FakeMarketsClient(markets),
    )
    assert report.mappings_upserted == 0
    assert report.mappings_skipped_manual == 0
    with store.connection() as conn:
        row = get_mapping(conn, "KX-CPI")
    assert row is None
