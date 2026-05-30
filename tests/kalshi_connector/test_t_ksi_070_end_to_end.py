"""T-KSI-070 — Kalshi end-to-end integration test.

Composes the full cycle (cutoff → series → events → markets → prices
→ settlements → trades + sector mapping + orderbook) against
synthetic in-process fakes, plus failure-injection scenarios and the
forbidden-imports acceptance check.

Sub-scenarios covered (one test each, all in this file so the cycle
shape is obvious):

- Eligibility gate refuses without configured jurisdiction.
- ToS gate refuses without recorded acknowledgement.
- Read-only posture acceptance round-trip.
- Daily-cycle happy path: every stage runs and persists rows.
- Cycle is idempotent: re-run produces no inserts, only unchanged.
- Settlement backfill across the cutoff routes live + historical.
- Watched-trade pull only fires for the configured tickers.
- On-demand orderbook persists YES + derived NO levels.
- Sector mapping fires the heuristic, including out_of_scope.
- Removed-market handling: market disappearing upstream gets removed_at.
- Cross-venue mapping: same class against both Polymarket and Kalshi
  produces two distinct comparisons.
- Failure-injection: Kalshi 5xx in one stage doesn't block others.
- Failure-injection: 429 with no Retry-After header is retried per
  the helper's own backoff.
- Failure-injection: ToS hash drift between runs forces re-ack.
- Failure-injection: posture-mismatch acknowledgement refused.
- Forbidden-imports: cryptography.hazmat asymmetric.padding,
  websockets, aiohttp.WSMsgType are not imported anywhere in
  kalshi_connector.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import random
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import razor_rooster.kalshi_connector as kalshi_connector_pkg
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.kalshi_connector.client.models import (
    KalshiEvent,
    KalshiHistoricalCutoff,
    KalshiMarket,
    KalshiOrderbook,
    KalshiOrderbookLevel,
    KalshiPaginatedResponse,
    KalshiSeries,
    KalshiTrade,
)
from razor_rooster.kalshi_connector.client.rate_limit import TokenBucket
from razor_rooster.kalshi_connector.client.retry import (
    RetryAttempt,
    RetryExhaustedError,
    retry_with_backoff,
)
from razor_rooster.kalshi_connector.config.loader import (
    KalshiAllowedJurisdictionsConfig,
    KalshiConfig,
    KalshiSectorKeywordsConfig,
)
from razor_rooster.kalshi_connector.cycle import (
    cycle_report_to_connector_outcome,
    run_kalshi_cycle,
)
from razor_rooster.kalshi_connector.gates.eligibility import (
    EligibilityRefusal,
    check_eligibility,
)
from razor_rooster.kalshi_connector.gates.tos import (
    ToSAcknowledgementRequired,
    ToSGateResult,
    ToSPostureMismatch,
    check_tos_acknowledged,
    hash_tos_text,
    record_acknowledgement,
)
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)
from razor_rooster.kalshi_connector.persistence.source import (
    register_kalshi_sources,
)
from razor_rooster.kalshi_connector.sync.orderbook import fetch_orderbook

# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "ksi_e2e.duckdb"
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
            "macroeconomic": ["CPI", "Fed"],
            "regulatory": ["FDA"],
            "commodity": ["oil"],
            "climate": ["hurricane"],
            "geopolitical": ["election"],
            "public_health": ["vaccine"],
            "infrastructure_energy": ["pipeline"],
            "cross_cutting": ["global"],
            "out_of_scope": ["NFL", "Oscar"],
        },
    )


@pytest.fixture
def config_default() -> KalshiConfig:
    return KalshiConfig(version=1)


@pytest.fixture
def allowed_us_only() -> KalshiAllowedJurisdictionsConfig:
    return KalshiAllowedJurisdictionsConfig(version=1, allowed=["US"])


# --------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------


def _cutoff_2026() -> KalshiHistoricalCutoff:
    ts = datetime(2026, 2, 15, tzinfo=UTC)
    return KalshiHistoricalCutoff(
        market_settled_ts=ts,
        trades_created_ts=ts,
        orders_updated_ts=ts,
        fetched_at=ts,
    )


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
    market_type: str = "binary",
    status: str = "open",
    yes_bid: float | None = 0.42,
    yes_ask: float | None = 0.43,
    title: str | None = None,
    result: str | None = None,
    expiration_time: datetime | None = None,
) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        event_ticker="EVT-1",
        series_ticker="INX",
        title=title or f"Market {ticker}",
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
        last_price_dollars=yes_bid,
        previous_yes_bid_dollars=yes_bid,
        previous_yes_ask_dollars=yes_ask,
        previous_price_dollars=None,
        volume_24h=10000.0,
        volume=10000.0,
        liquidity=5000.0,
        open_interest=None,
    )


# --------------------------------------------------------------------
# Fake REST client
# --------------------------------------------------------------------


class _FakeRESTClient:
    """A configurable stand-in for KalshiRESTClient."""

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
        trades_live: dict[str, list[KalshiTrade]] | None = None,
        trades_historical: dict[str, list[KalshiTrade]] | None = None,
        orderbook: KalshiOrderbook | None = None,
    ) -> None:
        self._series = series_items or []
        self._events_by_key = events_by_key or {}
        self._markets_by_event = markets_by_event or {}
        self._markets_by_ticker = markets_by_ticker or {}
        self._cutoff = cutoff or _cutoff_2026()
        self._live_settled = live_settled or []
        self._historical_settled = historical_settled or []
        self._trades_live = trades_live or {}
        self._trades_historical = trades_historical or {}
        self._orderbook = orderbook
        self.calls: list[str] = []

    def get_historical_cutoff(self) -> KalshiHistoricalCutoff:
        self.calls.append("cutoff")
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
        return KalshiPaginatedResponse(
            items=list(self._events_by_key.get(f"{series_ticker}|{status}", [])),
            cursor=None,
        )

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
        return KalshiPaginatedResponse(
            items=list(self._markets_by_event.get(event_ticker or "", [])),
            cursor=None,
        )

    def get_market(self, ticker: str) -> KalshiMarket:
        if ticker in self._markets_by_ticker:
            return self._markets_by_ticker[ticker]
        for items in self._markets_by_event.values():
            for m in items:
                if m.ticker == ticker:
                    return m
        return _market(ticker=ticker)

    def get_orderbook(self, ticker: str, *, depth: int = 10) -> KalshiOrderbook:
        if self._orderbook is not None:
            return self._orderbook
        return KalshiOrderbook(
            ticker=ticker,
            snapshot_ts=datetime.now(tz=UTC),
            yes_levels=(),
            no_levels=(),
        )

    def get_market_trades(self, ticker: str | None = None, **_: object) -> KalshiPaginatedResponse:
        items = self._trades_live.get(ticker or "", []) if ticker else []
        return KalshiPaginatedResponse(items=list(items), cursor=None)

    def get_historical_markets(self, **_: object) -> KalshiPaginatedResponse:
        return KalshiPaginatedResponse(items=list(self._historical_settled), cursor=None)

    def get_historical_trades(
        self, *, ticker: str | None = None, **_: object
    ) -> KalshiPaginatedResponse:
        items = self._trades_historical.get(ticker or "", []) if ticker else []
        return KalshiPaginatedResponse(items=list(items), cursor=None)

    def close(self) -> None:
        return


def _build_basic_universe() -> _FakeRESTClient:
    cpi = _market(
        ticker="KX-CPI",
        title="CPI above 2.5% in August",
    )
    nfl = _market(
        ticker="KX-NFL",
        title="NFL season-opener winner",
    )
    return _FakeRESTClient(
        series_items=[_series("INX")],
        events_by_key={
            "INX|open": [_event()],
            "INX|closed": [],
            "INX|settled": [],
        },
        markets_by_event={"EVT-1": [cpi, nfl]},
        markets_by_ticker={"KX-CPI": cpi, "KX-NFL": nfl},
    )


# --------------------------------------------------------------------
# Eligibility + ToS gate end-to-end
# --------------------------------------------------------------------


def test_eligibility_gate_refuses_without_jurisdiction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    with pytest.raises(EligibilityRefusal, match="OPERATOR_JURISDICTION"):
        check_eligibility(
            operator_config_path=tmp_path / "no.yaml",
            allowed=allowed_us_only,
        )


def test_eligibility_gate_passes_for_allowed_us(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    monkeypatch.setenv("OPERATOR_JURISDICTION", "US")
    accepted = check_eligibility(
        operator_config_path=tmp_path / "no.yaml",
        allowed=allowed_us_only,
    )
    assert accepted == "US"


class _StaticToSClient:
    def __init__(self, body: str) -> None:
        self._body = body

    def get(self, url: str, *, timeout: float | None = None) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            content=self._body.encode("utf-8"),
            request=httpx.Request("GET", url),
        )


def test_tos_gate_round_trip_with_read_only_posture(
    store: DuckDBStore,
) -> None:
    body = "Kalshi ToS v1"
    expected_hash = hash_tos_text(body)
    client = _StaticToSClient(body)
    with store.connection() as conn:
        record_acknowledgement(conn, tos_version_hash=expected_hash)
        result = check_tos_acknowledged(conn, client=client)  # type: ignore[arg-type]
    assert isinstance(result, ToSGateResult)
    assert result.acknowledged_posture == "read_only"


def test_tos_gate_refuses_when_no_ack(store: DuckDBStore) -> None:
    client = _StaticToSClient("Kalshi ToS v1")
    with store.connection() as conn, pytest.raises(ToSAcknowledgementRequired):
        check_tos_acknowledged(conn, client=client)  # type: ignore[arg-type]


def test_tos_gate_hash_drift_forces_reack(store: DuckDBStore) -> None:
    """Acknowledged hash A; live hash now B → re-ack required."""
    body_a = "Kalshi ToS v1"
    body_b = "Kalshi ToS v2"
    hash_a = hash_tos_text(body_a)
    hash_b = hash_tos_text(body_b)
    with store.connection() as conn:
        record_acknowledgement(conn, tos_version_hash=hash_a)
        with pytest.raises(ToSAcknowledgementRequired) as exc_info:
            check_tos_acknowledged(conn, client=_StaticToSClient(body_b))  # type: ignore[arg-type]
    assert exc_info.value.tos_version_hash == hash_b


def test_tos_gate_posture_mismatch_refuses(store: DuckDBStore) -> None:
    """An ack recorded for 'trading' is refused under v1 read-only."""
    body = "Kalshi ToS v1"
    expected_hash = hash_tos_text(body)
    with store.connection() as conn:
        record_acknowledgement(conn, tos_version_hash=expected_hash, posture="trading")
        with pytest.raises(ToSPostureMismatch) as exc_info:
            check_tos_acknowledged(conn, client=_StaticToSClient(body))  # type: ignore[arg-type]
    assert exc_info.value.recorded_posture == "trading"


# --------------------------------------------------------------------
# Daily cycle: happy path + idempotency
# --------------------------------------------------------------------


def test_daily_cycle_happy_path_persists_all_stages(
    store: DuckDBStore,
    config_default: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    client = _build_basic_universe()
    report = run_kalshi_cycle(
        store, config=config_default, sector_keywords=keywords, rest_client=client
    )
    assert not report.errors
    # Series + events + markets persisted.
    with store.connection() as conn:
        n_series = conn.execute("SELECT COUNT(*) FROM kalshi_series").fetchone()
        n_events = conn.execute("SELECT COUNT(*) FROM kalshi_events").fetchone()
        n_markets = conn.execute("SELECT COUNT(*) FROM kalshi_markets").fetchone()
        n_prices = conn.execute("SELECT COUNT(*) FROM kalshi_price_snapshots").fetchone()
        n_mappings = conn.execute("SELECT COUNT(*) FROM kalshi_sector_mapping").fetchone()
    assert n_series and n_series[0] == 1
    assert n_events and n_events[0] == 1
    assert n_markets and n_markets[0] == 2
    assert n_prices and n_prices[0] == 2
    assert n_mappings and n_mappings[0] == 2


def test_daily_cycle_is_idempotent(
    store: DuckDBStore,
    config_default: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """Re-running the same cycle produces no inserts on the second run."""
    client = _build_basic_universe()
    run_kalshi_cycle(store, config=config_default, sector_keywords=keywords, rest_client=client)
    second = run_kalshi_cycle(
        store, config=config_default, sector_keywords=keywords, rest_client=client
    )
    assert second.series is not None and second.series.series_inserted == 0
    assert second.events is not None and second.events.events_inserted == 0
    assert second.markets is not None and second.markets.markets_inserted == 0
    # Markets unchanged → mappings_skipped_manual stays 0 (heuristic
    # re-runs but the row's confidence is still 'inferred' so the
    # upsert overwrites with same content).
    assert second.markets.markets_unchanged == 2


# --------------------------------------------------------------------
# Settlement backfill: live + historical routing
# --------------------------------------------------------------------


def test_settlement_backfill_routes_live_and_historical(
    store: DuckDBStore,
    config_default: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    cutoff = _cutoff_2026()
    settled_live = _market(
        ticker="KX-LIVE-SETTLE",
        status="settled",
        result="yes",
        expiration_time=datetime(2026, 5, 1, tzinfo=UTC),
    )
    settled_old = _market(
        ticker="KX-OLD-SETTLE",
        status="settled",
        result="yes",
        expiration_time=datetime(2025, 12, 1, tzinfo=UTC),
    )
    client = _FakeRESTClient(
        series_items=[_series("INX")],
        events_by_key={
            "INX|open": [_event()],
            "INX|closed": [],
            "INX|settled": [],
        },
        markets_by_event={"EVT-1": [_market(ticker="KX-CPI")]},
        cutoff=cutoff,
        live_settled=[settled_live],
        historical_settled=[settled_old],
    )
    run_kalshi_cycle(store, config=config_default, sector_keywords=keywords, rest_client=client)
    with store.connection() as conn:
        rows = conn.execute("SELECT ticker FROM kalshi_settlements ORDER BY ticker").fetchall()
    tickers = {r[0] for r in rows}
    assert "KX-LIVE-SETTLE" in tickers
    assert "KX-OLD-SETTLE" in tickers


# --------------------------------------------------------------------
# Watched-trades pull
# --------------------------------------------------------------------


def test_watched_trades_pull_only_for_listed_tickers(
    store: DuckDBStore,
    config_default: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """sync_trades pulls only for tickers in watched_markets."""
    config_with_watched = KalshiConfig.model_validate(
        {
            **config_default.model_dump(mode="python"),
            "sync": {
                **config_default.sync.model_dump(mode="python"),
                "prices": {
                    **config_default.sync.prices.model_dump(mode="python"),
                    "watched_markets": ["KX-CPI"],
                },
            },
        }
    )
    trade = KalshiTrade(
        trade_id="T-1",
        ticker="KX-CPI",
        created_time=datetime(2026, 5, 16, tzinfo=UTC),
        yes_price_dollars=0.43,
        no_price_dollars=0.57,
        count=10.0,
        taker_side="yes",
    )
    client = _FakeRESTClient(
        series_items=[_series("INX")],
        events_by_key={
            "INX|open": [_event()],
            "INX|closed": [],
            "INX|settled": [],
        },
        markets_by_event={"EVT-1": [_market(ticker="KX-CPI"), _market(ticker="KX-OTHER")]},
        markets_by_ticker={
            "KX-CPI": _market(ticker="KX-CPI"),
            "KX-OTHER": _market(ticker="KX-OTHER"),
        },
        trades_live={"KX-CPI": [trade]},
    )
    run_kalshi_cycle(
        store,
        config=config_with_watched,
        sector_keywords=keywords,
        rest_client=client,
    )
    with store.connection() as conn:
        rows = conn.execute("SELECT ticker FROM kalshi_trades").fetchall()
    tickers = {r[0] for r in rows}
    # Only KX-CPI should have trades.
    assert tickers == {"KX-CPI"}


# --------------------------------------------------------------------
# Orderbook on-demand
# --------------------------------------------------------------------


def test_orderbook_persists_yes_and_derived_no_levels(
    store: DuckDBStore,
) -> None:
    """fetch_orderbook persists YES + derived NO levels symmetrically."""
    snapshot_ts = datetime(2026, 5, 16, 12, tzinfo=UTC)
    orderbook = KalshiOrderbook(
        ticker="KX-CPI",
        snapshot_ts=snapshot_ts,
        yes_levels=(KalshiOrderbookLevel(price_dollars=0.42, count=100.0),),
        no_levels=(KalshiOrderbookLevel(price_dollars=0.58, count=100.0),),
    )
    client = _FakeRESTClient(orderbook=orderbook)
    report = fetch_orderbook(store, client=client, ticker="KX-CPI")  # type: ignore[arg-type]
    assert report.yes_levels == 1
    assert report.no_levels == 1
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT side, price_dollars FROM kalshi_orderbook_snapshots "
            "WHERE ticker = 'KX-CPI' ORDER BY side"
        ).fetchall()
    by_side = {r[0]: r[1] for r in rows}
    assert by_side["yes"] == pytest.approx(0.42)
    assert by_side["no"] == pytest.approx(0.58)
    # Sum checks: yes + no = 1 (orderbook NO derivation invariant).
    assert by_side["yes"] + by_side["no"] == pytest.approx(1.0)


# --------------------------------------------------------------------
# Sector mapping including out_of_scope
# --------------------------------------------------------------------


def test_sector_mapping_includes_out_of_scope(
    store: DuckDBStore,
    config_default: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    client = _build_basic_universe()
    run_kalshi_cycle(store, config=config_default, sector_keywords=keywords, rest_client=client)
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT ticker, razor_sector FROM kalshi_sector_mapping ORDER BY ticker"
        ).fetchall()
    by_ticker = {r[0]: r[1] for r in rows}
    assert by_ticker["KX-CPI"] == "macroeconomic"
    assert by_ticker["KX-NFL"] == "out_of_scope"


# --------------------------------------------------------------------
# Removed-market handling
# --------------------------------------------------------------------


def test_removed_market_marked_with_removed_at(
    store: DuckDBStore,
    config_default: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """A market that disappears upstream gets removed_at set."""
    client_with_two = _build_basic_universe()
    run_kalshi_cycle(
        store,
        config=config_default,
        sector_keywords=keywords,
        rest_client=client_with_two,
    )
    # Second cycle: KX-NFL is gone.
    only_cpi = _market(ticker="KX-CPI", title="CPI above 2.5%")
    client_one_left = _FakeRESTClient(
        series_items=[_series("INX")],
        events_by_key={
            "INX|open": [_event()],
            "INX|closed": [],
            "INX|settled": [],
        },
        markets_by_event={"EVT-1": [only_cpi]},
        markets_by_ticker={"KX-CPI": only_cpi},
    )
    run_kalshi_cycle(
        store,
        config=config_default,
        sector_keywords=keywords,
        rest_client=client_one_left,
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT removed_at FROM kalshi_markets WHERE ticker = 'KX-NFL'"
        ).fetchone()
    assert row is not None
    assert row[0] is not None  # removed_at populated


# --------------------------------------------------------------------
# Cross-venue mapping
# --------------------------------------------------------------------


def test_cross_venue_mapping_two_distinct_comparisons(
    store: DuckDBStore,
    config_default: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """One class mapped to both venues yields two comparison rows."""
    from razor_rooster.mispricing_detector.engines.comparator import (
        compute_comparison,
    )
    from razor_rooster.mispricing_detector.mapping.operator_overrides import (
        register_operator_mapping,
    )
    from razor_rooster.mispricing_detector.persistence.migrations import (
        run_pending_mispricing_migrations,
    )
    from razor_rooster.pattern_library.persistence.migrations import (
        run_pending_pattern_library_migrations,
    )
    from razor_rooster.polymarket_connector.persistence.migrations import (
        run_pending_polymarket_migrations,
    )
    from razor_rooster.signal_scanner.persistence.migrations import (
        run_pending_signal_scanner_migrations,
    )

    # Set up the full migration stack.
    with store.connection() as conn:
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        run_pending_mispricing_migrations(conn)

    # Seed a polymarket market + price snapshot.
    snapshot_ts = datetime(2026, 5, 16, 12, tzinfo=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO polymarket_markets ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "condition_id, slug, question, market_type, outcome_tokens, "
            "active, closed, resolved"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "polymarket",
                "0xabc",
                snapshot_ts,
                snapshot_ts,
                "polymarket@0.1.0",
                "{}",
                "0xabc",
                "test",
                "test?",
                "binary",
                json.dumps(
                    [
                        {"id": "tok-yes", "outcome": "yes"},
                        {"id": "tok-no", "outcome": "no"},
                    ]
                ),
                True,
                False,
                False,
            ],
        )
        conn.execute(
            "INSERT INTO polymarket_price_snapshots ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "condition_id, outcome_token_id, snapshot_ts, mid_price, "
            "best_bid, best_ask, last_trade_price, last_trade_ts, "
            "volume_24h, liquidity_warning, spread_bps, snapshot_source"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "polymarket",
                f"0xabc:tok-yes:{snapshot_ts.isoformat()}",
                snapshot_ts,
                snapshot_ts,
                "polymarket@0.1.0",
                "{}",
                "0xabc",
                "tok-yes",
                snapshot_ts,
                0.42,
                0.41,
                0.43,
                0.42,
                snapshot_ts,
                10_000.0,
                False,
                50,
                "rest",
            ],
        )
        conn.execute(
            "INSERT INTO scan_summaries ("
            "scan_id, scan_started_at, scan_completed_at, "
            "pattern_library_version, classes_total, classes_succeeded, "
            "classes_failed, classes_skipped, candidates_count, "
            "library_stale_warning"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["scan-1", snapshot_ts, snapshot_ts, 1, 1, 1, 0, 0, 1, False],
        )
        conn.execute(
            "INSERT INTO scan_records ("
            "scan_id, class_id, class_definition_version, "
            "pattern_library_version, data_as_of, scan_started_at, "
            "scan_completed_at, base_rate, base_rate_ci_lower, "
            "base_rate_ci_upper, posterior, posterior_ci_lower, "
            "posterior_ci_upper, log_odds_shift, is_candidate, "
            "candidate_direction, signature_confidence, "
            "low_signature_confidence, source_stale_warning, "
            "library_stale_warning, definition_drift_warning, "
            "no_update_applied"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "scan-1",
                "cpi",
                1,
                1,
                snapshot_ts,
                snapshot_ts,
                snapshot_ts,
                0.20,
                0.10,
                0.30,
                0.65,
                0.55,
                0.75,
                1.5,
                True,
                "above",
                0.85,
                False,
                False,
                False,
                False,
                False,
            ],
        )
        conn.execute(
            "INSERT INTO scan_traces (scan_id, class_id, trace_json) VALUES (?, ?, ?)",
            ["scan-1", "cpi", json.dumps({"precursors": []})],
        )

    # Seed Kalshi market + snapshot via a real cycle.
    client = _build_basic_universe()
    run_kalshi_cycle(store, config=config_default, sector_keywords=keywords, rest_client=client)

    with store.connection() as conn:
        poly = register_operator_mapping(
            conn, class_id="cpi", condition_id="0xabc", venue="polymarket"
        )
        kalshi = register_operator_mapping(
            conn, class_id="cpi", condition_id="KX-CPI", venue="kalshi"
        )

    cycle_id = "cy-cross"
    poly_cmp, _ = compute_comparison(
        store=store,
        cycle_id=cycle_id,
        mapping=poly,
        scan_id="scan-1",
        library_version=1,
    )
    kalshi_cmp, _ = compute_comparison(
        store=store,
        cycle_id=cycle_id,
        mapping=kalshi,
        scan_id="scan-1",
        library_version=1,
    )

    assert poly_cmp.venue == "polymarket"
    assert kalshi_cmp.venue == "kalshi"
    assert poly_cmp.condition_id != kalshi_cmp.condition_id
    assert poly_cmp.delta != kalshi_cmp.delta


# --------------------------------------------------------------------
# Failure injection
# --------------------------------------------------------------------


def test_failure_injection_5xx_in_one_stage_does_not_block_others(
    store: DuckDBStore,
    config_default: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """A simulated 5xx in get_market still lets settlements stage run."""

    class _PriceFailingClient(_FakeRESTClient):
        def get_market(self, ticker: str) -> KalshiMarket:
            raise RuntimeError("simulated 503 Service Unavailable")

    client = _PriceFailingClient(
        series_items=[_series("INX")],
        events_by_key={
            "INX|open": [_event()],
            "INX|closed": [],
            "INX|settled": [],
        },
        markets_by_event={"EVT-1": [_market(ticker="KX-CPI")]},
    )
    report = run_kalshi_cycle(
        store, config=config_default, sector_keywords=keywords, rest_client=client
    )
    assert report.markets is not None  # earlier stage succeeded
    assert report.prices is not None
    assert report.prices.market_errors  # failure captured
    assert report.settlements is not None  # later stage still ran


def test_failure_injection_429_no_retry_after_uses_internal_backoff() -> None:
    """The retry helper does not honor any Retry-After header on 429.

    Synthetic test: the response declares Retry-After=60 but the helper
    must not adapt its backoff to that value.
    """

    class _Resp:
        def __init__(self, status: int, headers: dict[str, str]) -> None:
            self.status_code = status
            self.headers = headers

    sleep_durations: list[float] = []

    def custom_sleep(s: float) -> None:
        sleep_durations.append(s)

    counter = {"calls": 0}

    def callable_() -> _Resp:
        counter["calls"] += 1
        if counter["calls"] == 1:
            return _Resp(429, {"Retry-After": "60"})
        return _Resp(200, {})

    captured: list[RetryAttempt] = []
    retry_with_backoff(
        callable_,
        sleep=custom_sleep,
        rng=random.Random(0),
        base_seconds=1.0,
        max_seconds=5.0,
        on_retry=captured.append,
    )
    # Helper applied its own backoff (≤ 5s) — not 60s as Retry-After
    # would have suggested.
    assert all(s <= 5.0 for s in sleep_durations)
    assert captured and captured[0].status_code == 429


def test_failure_injection_429_drains_bucket_when_supplied() -> None:
    """A 429 with bucket=… drains the limiter so the next attempt
    doesn't race against a stale rate budget."""
    bucket = TokenBucket(capacity=100.0, refill_per_second=0.01)

    class _Resp:
        def __init__(self, status: int) -> None:
            self.status_code = status
            self.headers: dict[str, str] = {}

    counter = {"calls": 0}

    def callable_() -> _Resp:
        counter["calls"] += 1
        if counter["calls"] == 1:
            return _Resp(429)
        return _Resp(200)

    retry_with_backoff(
        callable_,
        sleep=lambda _: None,
        rng=random.Random(0),
        bucket=bucket,
    )
    stats = bucket.stats()
    assert stats.tokens_available < 1.0


def test_persistent_failure_exhausts_retry_budget() -> None:
    """Persistent 5xx exhausts retries and surfaces the original error."""

    class _Resp:
        def __init__(self) -> None:
            self.status_code = 503
            self.headers: dict[str, str] = {}

    def callable_() -> _Resp:
        return _Resp()

    with pytest.raises(RetryExhaustedError):
        retry_with_backoff(
            callable_,
            sleep=lambda _: None,
            rng=random.Random(0),
            max_retries=2,
        )


# --------------------------------------------------------------------
# Forbidden imports
# --------------------------------------------------------------------


def test_kalshi_connector_forbidden_imports_absent() -> None:
    """Walk the kalshi_connector package; assert no module imports
    cryptography.hazmat.primitives.asymmetric.padding,
    websockets, or aiohttp.WSMsgType.

    These imports are reserved for v2 trading work; v1 must stay
    read-only and unsigned. The check operates at runtime by
    importing every submodule and inspecting its loaded sys.modules
    plus reading source for the literal forbidden import patterns.
    """
    forbidden_patterns = [
        "from cryptography.hazmat.primitives.asymmetric import padding",
        "from cryptography.hazmat.primitives.asymmetric.padding",
        "import websockets",
        "from websockets",
        "from aiohttp import WSMsgType",
        "aiohttp.WSMsgType",
    ]

    package = kalshi_connector_pkg
    package_path = Path(package.__file__).resolve().parent  # type: ignore[arg-type]

    # Walk every .py file in kalshi_connector.
    failed: list[tuple[str, str]] = []
    for module_info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
        module = importlib.import_module(module_info.name)
        if module.__file__ is None:
            continue
        source = Path(module.__file__).read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            if pattern in source:
                failed.append((module.__name__, pattern))

    assert not failed, (
        f"forbidden imports found in kalshi_connector: {failed}. "
        "These imports are reserved for v2 trading work; v1 must "
        "remain read-only and unsigned."
    )

    # Also walk every file in the subdirectory tree that didn't get
    # imported (e.g. __pycache__ skipped, but config and data files
    # could carry forbidden patterns).
    for path in package_path.rglob("*.py"):
        if path.name == "__init__.py" and path.parent == package_path:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for pattern in forbidden_patterns:
            if pattern in source:
                failed.append((str(path.relative_to(package_path)), pattern))

    assert not failed, f"forbidden imports found in source files: {failed}"


# --------------------------------------------------------------------
# Cycle outcome aggregation
# --------------------------------------------------------------------


def test_cycle_outcome_aggregates_clean_run(
    store: DuckDBStore,
    config_default: KalshiConfig,
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    client = _build_basic_universe()
    report = run_kalshi_cycle(
        store, config=config_default, sector_keywords=keywords, rest_client=client
    )
    outcome = cycle_report_to_connector_outcome(report)
    assert outcome.source_id == "kalshi"
    assert outcome.status == "ok"
    assert outcome.records_ingested >= 5  # series + event + 2 markets + 2 prices
