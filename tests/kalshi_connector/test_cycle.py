"""T-KSI-062 — Kalshi cycle integration acceptance tests.

Verifies:
- Successful end-to-end cycle reports per-stage counts.
- Failure isolation: one stage's exception doesn't block subsequent
  stages.
- ``cycle_report_to_connector_outcome`` produces the right status
  values (``'ok'`` / ``'partial'`` / ``'failed'``).
- ``skip_trades=True`` skips the trades stage cleanly.
- Empty watched_markets skips trades cleanly without an error.
- ``stage_summary_lines`` renders lines in the expected order.
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
    KalshiHistoricalCutoff,
    KalshiMarket,
    KalshiOrderbook,
    KalshiPaginatedResponse,
    KalshiSeries,
)
from razor_rooster.kalshi_connector.config.loader import (
    KalshiConfig,
    KalshiSectorKeywordsConfig,
)
from razor_rooster.kalshi_connector.cycle import (
    cycle_report_to_connector_outcome,
    run_kalshi_cycle,
    stage_summary_lines,
)
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)
from razor_rooster.kalshi_connector.persistence.source import (
    register_kalshi_sources,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "kalshi_cycle.duckdb"
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
        sectors={"macroeconomic": ["CPI"], "out_of_scope": ["NFL"]},
    )


@pytest.fixture
def config() -> KalshiConfig:
    return KalshiConfig(version=1)


# -- fakes ----------------------------------------------------------------


def _cutoff_2026() -> KalshiHistoricalCutoff:
    from datetime import UTC, datetime

    ts = datetime(2026, 2, 15, tzinfo=UTC)
    return KalshiHistoricalCutoff(
        market_settled_ts=ts,
        trades_created_ts=ts,
        orders_updated_ts=ts,
        fetched_at=ts,
    )


def _series(ticker: str = "INX", title: str = "S&P") -> KalshiSeries:
    return KalshiSeries(series_ticker=ticker, title=title)


def _event(ticker: str = "EVT-1", series_ticker: str = "INX") -> KalshiEvent:
    return KalshiEvent(
        event_ticker=ticker,
        series_ticker=series_ticker,
        title="Event",
        sub_title=None,
        category=None,
        mutually_exclusive=False,
        expected_expiration_time=None,
        strike_period=None,
        status="open",
    )


def _market(
    ticker: str = "KX-CPI",
    *,
    market_type: str = "binary",
    status: str = "open",
    yes_bid: float | None = 0.42,
    yes_ask: float | None = 0.43,
) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        event_ticker="EVT-1",
        series_ticker="INX",
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
        last_price_dollars=yes_bid,
        previous_yes_bid_dollars=yes_bid,
        previous_yes_ask_dollars=yes_ask,
        previous_price_dollars=None,
        volume_24h=10000.0,
        volume=10000.0,
        liquidity=5000.0,
        open_interest=None,
    )


class _FakeRESTClient:
    """A minimal in-process fake covering all REST methods the cycle calls."""

    def __init__(
        self,
        *,
        series_items: list[KalshiSeries] | None = None,
        events_by_key: dict[str, list[KalshiEvent]] | None = None,
        markets_by_event: dict[str, list[KalshiMarket]] | None = None,
        markets_by_ticker: dict[str, KalshiMarket] | None = None,
        cutoff: KalshiHistoricalCutoff | None = None,
        live_settled: list[KalshiMarket] | None = None,
        historical_settled: list[KalshiMarket] | None = None,
    ) -> None:
        self._series = series_items or []
        self._events_by_key = events_by_key or {}
        self._markets_by_event = markets_by_event or {}
        self._markets_by_ticker = markets_by_ticker or {}
        self._cutoff = cutoff or _cutoff_2026()
        self._live_settled = live_settled or []
        self._historical_settled = historical_settled or []

    def get_historical_cutoff(self) -> KalshiHistoricalCutoff:
        return self._cutoff

    def list_series(self, **_: object) -> KalshiPaginatedResponse:
        return KalshiPaginatedResponse(items=list(self._series), cursor=None)

    def list_events(
        self,
        *,
        series_ticker: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> KalshiPaginatedResponse:
        key = f"{series_ticker}|{status}"
        return KalshiPaginatedResponse(items=list(self._events_by_key.get(key, [])), cursor=None)

    def list_markets(
        self,
        *,
        event_ticker: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
        **_: object,
    ) -> KalshiPaginatedResponse:
        if status == "settled":
            return KalshiPaginatedResponse(items=list(self._live_settled), cursor=None)
        if event_ticker is not None:
            return KalshiPaginatedResponse(
                items=list(self._markets_by_event.get(event_ticker, [])),
                cursor=None,
            )
        return KalshiPaginatedResponse(items=[], cursor=None)

    def get_market(self, ticker: str) -> KalshiMarket:
        if ticker in self._markets_by_ticker:
            return self._markets_by_ticker[ticker]
        if ticker in self._markets_by_event.get("EVT-1", []):
            return self._markets_by_ticker.get(ticker, _market(ticker=ticker))
        return _market(ticker=ticker)

    def get_orderbook(self, ticker: str, *, depth: int = 10) -> KalshiOrderbook:
        from datetime import UTC, datetime

        return KalshiOrderbook(
            ticker=ticker,
            snapshot_ts=datetime.now(tz=UTC),
            yes_levels=(),
            no_levels=(),
        )

    def get_market_trades(self, ticker: str | None = None, **_: object) -> KalshiPaginatedResponse:
        return KalshiPaginatedResponse(items=[], cursor=None)

    def get_historical_markets(self, **_: object) -> KalshiPaginatedResponse:
        return KalshiPaginatedResponse(items=list(self._historical_settled), cursor=None)

    def get_historical_trades(self, **_: object) -> KalshiPaginatedResponse:
        return KalshiPaginatedResponse(items=[], cursor=None)

    def close(self) -> None:
        return


# -- happy path ---------------------------------------------------------


def test_cycle_runs_all_stages_successfully(
    store: DuckDBStore,
    config: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    cpi = _market(ticker="KX-CPI")
    client = _FakeRESTClient(
        series_items=[_series()],
        events_by_key={
            "INX|open": [_event()],
            "INX|closed": [],
            "INX|settled": [],
        },
        markets_by_event={"EVT-1": [cpi]},
        markets_by_ticker={"KX-CPI": cpi},
    )
    report = run_kalshi_cycle(store, config=config, sector_keywords=keywords, rest_client=client)
    assert not report.errors
    assert report.series is not None and report.series.series_inserted == 1
    assert report.events is not None and report.events.events_inserted == 1
    assert report.markets is not None and report.markets.markets_inserted == 1
    assert report.markets.mappings_upserted == 1  # heuristic side-effect
    assert report.prices is not None and report.prices.snapshots_inserted == 1
    assert report.settlements is not None
    # No watched markets in default config → trades skipped silently.
    assert report.trades is None


def test_cycle_outcome_status_ok_on_clean_run(
    store: DuckDBStore,
    config: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    cpi = _market(ticker="KX-CPI")
    client = _FakeRESTClient(
        series_items=[_series()],
        events_by_key={
            "INX|open": [_event()],
            "INX|closed": [],
            "INX|settled": [],
        },
        markets_by_event={"EVT-1": [cpi]},
        markets_by_ticker={"KX-CPI": cpi},
    )
    report = run_kalshi_cycle(store, config=config, sector_keywords=keywords, rest_client=client)
    outcome = cycle_report_to_connector_outcome(report)
    assert outcome.source_id == "kalshi"
    assert outcome.status == "ok"
    assert outcome.records_ingested >= 4  # series + event + market + price


# -- failure isolation -------------------------------------------------


class _FailingPriceClient(_FakeRESTClient):
    """Like the base but every get_market call raises."""

    def get_market(self, ticker: str) -> KalshiMarket:
        raise RuntimeError("simulated network failure for prices stage")


def test_cycle_isolates_per_stage_failures(
    store: DuckDBStore,
    config: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """A prices-stage exception is captured but later stages still run."""
    cpi = _market(ticker="KX-CPI")
    client = _FailingPriceClient(
        series_items=[_series()],
        events_by_key={
            "INX|open": [_event()],
            "INX|closed": [],
            "INX|settled": [],
        },
        markets_by_event={"EVT-1": [cpi]},
    )
    report = run_kalshi_cycle(store, config=config, sector_keywords=keywords, rest_client=client)
    # Earlier stages succeeded.
    assert report.series is not None
    assert report.events is not None
    assert report.markets is not None
    # Prices stage produced a per-market error in its report; no top-level
    # exception captured. The cycle continues to settlements.
    assert report.prices is not None
    assert report.prices.market_errors  # the failing get_market hit
    assert report.settlements is not None


def test_cycle_outcome_status_partial_on_per_stage_warnings(
    store: DuckDBStore,
    config: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """Catastrophic failure in one stage → status is 'partial' or 'failed'."""

    class _AllRaiseClient:
        def get_historical_cutoff(self) -> KalshiHistoricalCutoff:
            raise RuntimeError("boom")

        def list_series(self, **_: object) -> KalshiPaginatedResponse:
            raise RuntimeError("boom")

        def list_events(self, **_: object) -> KalshiPaginatedResponse:
            raise RuntimeError("boom")

        def list_markets(self, **_: object) -> KalshiPaginatedResponse:
            raise RuntimeError("boom")

        def get_market(self, ticker: str) -> KalshiMarket:
            raise RuntimeError("boom")

        def get_orderbook(self, *_: object, **__: object) -> KalshiOrderbook:
            raise RuntimeError("boom")

        def get_market_trades(self, *_: object, **__: object) -> KalshiPaginatedResponse:
            raise RuntimeError("boom")

        def get_historical_markets(self, **_: object) -> KalshiPaginatedResponse:
            raise RuntimeError("boom")

        def get_historical_trades(self, **_: object) -> KalshiPaginatedResponse:
            raise RuntimeError("boom")

        def close(self) -> None:
            return

    report = run_kalshi_cycle(
        store,
        config=config,
        sector_keywords=keywords,
        rest_client=_AllRaiseClient(),  # type: ignore[arg-type]
    )
    outcome = cycle_report_to_connector_outcome(report)
    # Cutoff failure is logged via report.errors. Series/events/markets
    # still tried and produce their own per-stage errors. Outcome status
    # should not be "ok".
    assert outcome.status in ("partial", "failed")


# -- skip_trades and empty watched_markets ----------------------------


def test_skip_trades_flag_skips_trades_stage(
    store: DuckDBStore,
    config: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    config_with_watched = KalshiConfig.model_validate(
        {
            **config.model_dump(mode="python"),
            "sync": {
                **config.sync.model_dump(mode="python"),
                "prices": {
                    **config.sync.prices.model_dump(mode="python"),
                    "watched_markets": ["KX-CPI"],
                },
            },
        }
    )
    cpi = _market(ticker="KX-CPI")
    client = _FakeRESTClient(
        series_items=[_series()],
        events_by_key={
            "INX|open": [_event()],
            "INX|closed": [],
            "INX|settled": [],
        },
        markets_by_event={"EVT-1": [cpi]},
        markets_by_ticker={"KX-CPI": cpi},
    )
    report = run_kalshi_cycle(
        store,
        config=config_with_watched,
        sector_keywords=keywords,
        rest_client=client,
        skip_trades=True,
    )
    assert report.trades is None


def test_empty_watched_skips_trades_silently(
    store: DuckDBStore,
    config: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """No watched markets → trades skipped without an error."""
    cpi = _market(ticker="KX-CPI")
    client = _FakeRESTClient(
        series_items=[_series()],
        events_by_key={
            "INX|open": [_event()],
            "INX|closed": [],
            "INX|settled": [],
        },
        markets_by_event={"EVT-1": [cpi]},
        markets_by_ticker={"KX-CPI": cpi},
    )
    report = run_kalshi_cycle(store, config=config, sector_keywords=keywords, rest_client=client)
    assert report.trades is None
    assert "trades" not in " ".join(report.errors)


# -- summary lines ---------------------------------------------------


def test_stage_summary_lines_order(
    store: DuckDBStore,
    config: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    cpi = _market(ticker="KX-CPI")
    client = _FakeRESTClient(
        series_items=[_series()],
        events_by_key={
            "INX|open": [_event()],
            "INX|closed": [],
            "INX|settled": [],
        },
        markets_by_event={"EVT-1": [cpi]},
        markets_by_ticker={"KX-CPI": cpi},
    )
    report = run_kalshi_cycle(store, config=config, sector_keywords=keywords, rest_client=client)
    lines = list(stage_summary_lines(report))
    expected_order = ["series:", "events:", "markets:", "prices:", "settlements:"]
    seen_indexes = [
        next(i for i, line in enumerate(lines) if marker in line) for marker in expected_order
    ]
    assert seen_indexes == sorted(seen_indexes), f"summary lines out of order: {lines}"


# -- ConnectorOutcome shape ---------------------------------------


def test_outcome_records_ingested_aggregates_all_stages(
    store: DuckDBStore,
    config: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    cpi = _market(ticker="KX-CPI")
    client = _FakeRESTClient(
        series_items=[_series()],
        events_by_key={
            "INX|open": [_event()],
            "INX|closed": [],
            "INX|settled": [],
        },
        markets_by_event={"EVT-1": [cpi]},
        markets_by_ticker={"KX-CPI": cpi},
    )
    report = run_kalshi_cycle(store, config=config, sector_keywords=keywords, rest_client=client)
    outcome = cycle_report_to_connector_outcome(report)
    # 1 series + 1 event + 1 market + 1 price snapshot = 4 baseline.
    assert outcome.records_ingested >= 4
