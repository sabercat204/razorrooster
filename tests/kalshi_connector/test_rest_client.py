"""T-KSI-033 — public REST client acceptance tests.

Uses httpx.MockTransport so we don't touch the real Kalshi API. Each
test sets up a transport that responds to specific paths with
recorded payloads and verifies the dataclass projection.

The tests cover:
- All endpoint methods (list/get for series / events / markets, plus
  orderbook, trades, historical cutoff, historical markets, historical
  trades).
- Multi-type market parsing (binary / scalar / categorical).
- Strike-variant round-trip (above / below / between / unstructured).
- NO-side derivation from YES asks.
- Cursor-based pagination with paginate=True.
- Rate-limiter charges per request.
- Error response surfaces a typed KalshiAPIError.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from razor_rooster.kalshi_connector.client.models import (
    KalshiEvent,
    KalshiMarket,
    KalshiOrderbook,
    KalshiSeries,
    KalshiTrade,
)
from razor_rooster.kalshi_connector.client.rate_limit import TokenBucket
from razor_rooster.kalshi_connector.client.rest import (
    KalshiAPIError,
    KalshiRESTClient,
)

# -- helpers --------------------------------------------------------------


_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


def _make_response(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def _build_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    bucket: TokenBucket | None = None,
) -> KalshiRESTClient:
    transport = httpx.MockTransport(handler)
    httpx_client = httpx.Client(transport=transport, follow_redirects=True)
    return KalshiRESTClient(
        base_url=_BASE_URL,
        bucket=bucket,
        max_retries=0,  # tests don't need retries against mocks
        client=httpx_client,
    )


# -- series ---------------------------------------------------------------


def test_list_series_returns_typed_dataclasses() -> None:
    payload = {
        "series": [
            {
                "ticker": "INX",
                "title": "S&P 500 close",
                "category": "Markets",
                "frequency": "daily",
                "tags": ["macro", "stocks"],
                "settlement_source": "Bloomberg",
                "contract_url": "https://kalshi.com/markets/inx",
            }
        ],
        "cursor": "next-page-token",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/series")
        return _make_response(payload)

    with _build_client(handler) as client:
        result = client.list_series()
    assert len(result.items) == 1
    series = result.items[0]
    assert isinstance(series, KalshiSeries)
    assert series.series_ticker == "INX"
    assert series.title == "S&P 500 close"
    assert series.tags == ("macro", "stocks")
    assert result.cursor == "next-page-token"


def test_get_series_fetches_single_record() -> None:
    payload = {"series": {"ticker": "INX", "title": "S&P 500 close"}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/series/INX")
        return _make_response(payload)

    with _build_client(handler) as client:
        series = client.get_series("INX")
    assert series.series_ticker == "INX"
    assert series.title == "S&P 500 close"


# -- events ---------------------------------------------------------------


def test_list_events_filters_by_status_and_series() -> None:
    payload = {
        "events": [
            {
                "event_ticker": "EVENT-1",
                "series_ticker": "INX",
                "title": "Event 1",
                "status": "open",
                "mutually_exclusive": True,
                "expected_expiration_time": "2026-12-31T23:59:00Z",
            }
        ]
    }
    captured_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        for k, v in request.url.params.items():
            captured_params[k] = v
        return _make_response(payload)

    with _build_client(handler) as client:
        client.list_events(series_ticker="INX", status="open", limit=50)
    assert captured_params["series_ticker"] == "INX"
    assert captured_params["status"] == "open"
    assert captured_params["limit"] == "50"


def test_get_event_returns_typed_dataclass() -> None:
    payload = {
        "event": {
            "event_ticker": "EVENT-1",
            "series_ticker": "INX",
            "title": "Event 1",
            "status": "open",
            "mutually_exclusive": False,
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler) as client:
        event = client.get_event("EVENT-1")
    assert isinstance(event, KalshiEvent)
    assert event.event_ticker == "EVENT-1"
    assert event.series_ticker == "INX"


# -- markets -------------------------------------------------------------


def test_list_markets_open_status_default() -> None:
    payload = {
        "markets": [
            {
                "ticker": "KXFOO",
                "event_ticker": "EVENT-1",
                "series_ticker": "INX",
                "title": "S&P > 5000",
                "market_type": "binary",
                "strike_type": "above",
                "floor_strike": 5000.0,
                "status": "open",
            }
        ]
    }
    captured_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        for k, v in request.url.params.items():
            captured_params[k] = v
        return _make_response(payload)

    with _build_client(handler) as client:
        result = client.list_markets()
    assert captured_params.get("status") == "open"
    market = result.items[0]
    assert isinstance(market, KalshiMarket)
    assert market.market_type == "binary"
    assert market.strike_type == "above"
    assert market.floor_strike == 5000.0


def test_market_type_round_trips_for_all_three() -> None:
    """Binary, scalar, and categorical markets all parse correctly."""
    payload = {
        "markets": [
            {
                "ticker": "KXBIN",
                "event_ticker": "EVT",
                "series_ticker": "S",
                "title": "Binary",
                "market_type": "binary",
                "status": "open",
            },
            {
                "ticker": "KXSCAL",
                "event_ticker": "EVT",
                "series_ticker": "S",
                "title": "Scalar",
                "market_type": "scalar",
                "status": "open",
            },
            {
                "ticker": "KXCAT",
                "event_ticker": "EVT",
                "series_ticker": "S",
                "title": "Categorical",
                "market_type": "categorical",
                "status": "open",
            },
        ]
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler) as client:
        result = client.list_markets(status=None)
    types = {m.market_type for m in result.items}
    assert types == {"binary", "scalar", "categorical"}


def test_strike_variant_round_trip() -> None:
    """All four strike-variant values round-trip through the dataclass."""
    payload = {
        "markets": [
            {
                "ticker": "KX-A",
                "event_ticker": "E",
                "series_ticker": "S",
                "title": "above",
                "market_type": "binary",
                "strike_type": "above",
                "status": "open",
            },
            {
                "ticker": "KX-B",
                "event_ticker": "E",
                "series_ticker": "S",
                "title": "below",
                "market_type": "binary",
                "strike_type": "below",
                "status": "open",
            },
            {
                "ticker": "KX-C",
                "event_ticker": "E",
                "series_ticker": "S",
                "title": "between",
                "market_type": "binary",
                "strike_type": "between",
                "status": "open",
            },
            {
                "ticker": "KX-D",
                "event_ticker": "E",
                "series_ticker": "S",
                "title": "unstructured",
                "market_type": "binary",
                "strike_type": "unstructured",
                "status": "open",
            },
        ]
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler) as client:
        result = client.list_markets(status=None)
    assert {m.strike_type for m in result.items} == {
        "above",
        "below",
        "between",
        "unstructured",
    }


def test_get_market_returns_typed_record() -> None:
    payload = {
        "market": {
            "ticker": "KXFOO",
            "event_ticker": "EVENT-1",
            "series_ticker": "INX",
            "title": "Foo",
            "market_type": "binary",
            "status": "open",
            "last_price": 0.42,
            "volume_24h": 12345.6,
        }
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler) as client:
        m = client.get_market("KXFOO")
    assert m.ticker == "KXFOO"
    assert m.last_price_dollars == 0.42
    assert m.volume_24h == 12345.6
    # Raw payload preserved for sync-time persistence.
    assert m.raw["last_price"] == 0.42


# -- orderbook -----------------------------------------------------------


def test_orderbook_no_side_derived_from_yes() -> None:
    payload = {
        "orderbook": {
            "yes": [
                [0.42, 100],
                [0.45, 50],
            ]
        },
        "snapshot_ts": "2026-05-16T12:00:00Z",
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler) as client:
        ob = client.get_orderbook("KXFOO")
    assert isinstance(ob, KalshiOrderbook)
    assert ob.ticker == "KXFOO"
    assert len(ob.yes_levels) == 2
    assert len(ob.no_levels) == 2
    # YES @ 0.42 → NO @ 1 - 0.42 = 0.58
    assert ob.no_levels[0].price_dollars == pytest.approx(0.58)
    assert ob.no_levels[1].price_dollars == pytest.approx(0.55)


def test_orderbook_handles_dict_levels() -> None:
    """Some Kalshi responses use {price, count} dicts rather than [p, c] tuples."""
    payload = {
        "orderbook": {
            "yes": [
                {"price": 0.42, "count": 100},
                {"price": 0.45, "count": 50},
            ]
        }
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler) as client:
        ob = client.get_orderbook("KXFOO")
    assert ob.yes_levels[0].price_dollars == 0.42
    assert ob.yes_levels[0].count == 100.0


# -- trades -------------------------------------------------------------


def test_get_market_trades_returns_typed_records() -> None:
    payload = {
        "trades": [
            {
                "trade_id": "T-1",
                "ticker": "KXFOO",
                "created_time": "2026-05-16T12:00:00Z",
                "yes_price": 0.42,
                "no_price": 0.58,
                "count": 10,
                "taker_side": "yes",
            }
        ]
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler) as client:
        result = client.get_market_trades("KXFOO")
    trade = result.items[0]
    assert isinstance(trade, KalshiTrade)
    assert trade.trade_id == "T-1"
    assert trade.yes_price_dollars == 0.42
    assert trade.no_price_dollars == 0.58
    assert trade.taker_side == "yes"


def test_trade_no_price_derived_when_missing() -> None:
    payload = {
        "trades": [
            {
                "trade_id": "T-1",
                "ticker": "KX",
                "created_time": "2026-05-16T12:00:00Z",
                "yes_price": 0.42,
                "count": 10,
            }
        ]
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler) as client:
        result = client.get_market_trades()
    assert result.items[0].no_price_dollars == pytest.approx(0.58)


# -- historical -------------------------------------------------------


def test_get_historical_cutoff_round_trip() -> None:
    payload = {
        "market_settled_ts": "2026-02-15T00:00:00Z",
        "trades_created_ts": "2026-02-15T00:00:00Z",
        "orders_updated_ts": "2026-02-15T00:00:00Z",
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler) as client:
        cutoff = client.get_historical_cutoff()
    assert cutoff.market_settled_ts == datetime(2026, 2, 15, tzinfo=UTC)
    assert cutoff.trades_created_ts == datetime(2026, 2, 15, tzinfo=UTC)
    assert cutoff.orders_updated_ts == datetime(2026, 2, 15, tzinfo=UTC)


def test_get_historical_cutoff_missing_field_raises() -> None:
    payload = {"market_settled_ts": "2026-02-15T00:00:00Z"}

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler) as client, pytest.raises(KalshiAPIError):
        client.get_historical_cutoff()


def test_get_historical_markets_paginates() -> None:
    page1 = {
        "markets": [
            {
                "ticker": "OLD-A",
                "event_ticker": "E",
                "series_ticker": "S",
                "title": "Old A",
                "market_type": "binary",
                "status": "settled",
            }
        ],
        "cursor": "page2",
    }
    page2 = {
        "markets": [
            {
                "ticker": "OLD-B",
                "event_ticker": "E",
                "series_ticker": "S",
                "title": "Old B",
                "market_type": "binary",
                "status": "settled",
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "cursor=page2" in str(request.url):
            return _make_response(page2)
        return _make_response(page1)

    with _build_client(handler) as client:
        result = client.get_historical_markets(paginate=True)
    assert len(result.items) == 2
    assert {m.ticker for m in result.items} == {"OLD-A", "OLD-B"}


def test_get_historical_market_single() -> None:
    payload = {
        "market": {
            "ticker": "OLD-A",
            "event_ticker": "E",
            "series_ticker": "S",
            "title": "Old A",
            "market_type": "binary",
            "status": "settled",
        }
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler) as client:
        m = client.get_historical_market("OLD-A")
    assert m.ticker == "OLD-A"


def test_get_historical_trades_paginates() -> None:
    page1 = {
        "trades": [
            {
                "trade_id": "T1",
                "ticker": "K",
                "created_time": "2026-01-01T00:00:00Z",
                "yes_price": 0.5,
                "no_price": 0.5,
                "count": 1,
            }
        ],
        "cursor": "page2",
    }
    page2 = {
        "trades": [
            {
                "trade_id": "T2",
                "ticker": "K",
                "created_time": "2026-01-02T00:00:00Z",
                "yes_price": 0.6,
                "no_price": 0.4,
                "count": 1,
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "cursor=page2" in str(request.url):
            return _make_response(page2)
        return _make_response(page1)

    with _build_client(handler) as client:
        result = client.get_historical_trades(paginate=True)
    assert {t.trade_id for t in result.items} == {"T1", "T2"}


# -- error path + rate limiter ---------------------------------------------


def test_4xx_error_raises_typed_exception() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response({"error": "bad request"}, status=400)

    with _build_client(handler) as client, pytest.raises(KalshiAPIError) as exc_info:
        client.list_series()
    assert exc_info.value.status_code == 400


def test_rate_limiter_charges_per_request() -> None:
    """A list call drains 10 tokens (cost for /series)."""
    bucket = TokenBucket(capacity=100.0, refill_per_second=0.01)
    payload = {"series": []}

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler, bucket=bucket) as client:
        client.list_series()
    stats = bucket.stats()
    # 100 - 10 = 90, allow tiny floating-point tolerance for elapsed-time refill.
    assert stats.tokens_available == pytest.approx(90.0, abs=1.0)


def test_pagination_stops_when_cursor_is_none() -> None:
    """A response without 'cursor' ends the pagination loop."""
    payload = {"series": [{"ticker": "A", "title": "A"}]}

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response(payload)

    with _build_client(handler) as client:
        result = client.list_series(paginate=True)
    assert len(result.items) == 1
    assert result.cursor is None


def test_invalid_base_url_rejected() -> None:
    with pytest.raises(ValueError, match="must be http"):
        KalshiRESTClient(base_url="ws://example.com")


def test_client_is_not_iterable() -> None:
    """Defense against accidental iteration."""

    def handler(_: httpx.Request) -> httpx.Response:
        return _make_response({})

    with _build_client(handler) as client, pytest.raises(TypeError):
        for _ in client:
            pass
