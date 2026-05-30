"""T-PMC-034 — CLOB public client tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest

from razor_rooster.polymarket_connector.client.clob_public import (
    CLOB_BASE_URL,
    ClobClientError,
    ClobPublicClient,
    Orderbook,
    OrderbookLevel,
    PriceQuote,
)
from razor_rooster.polymarket_connector.client.rate_limit import (
    TokenBucket,
    reset_shared_bucket,
)


@pytest.fixture(autouse=True)
def _isolated_bucket() -> Iterator[None]:
    reset_shared_bucket()
    yield
    reset_shared_bucket()


def _build_client(handler: Callable[[httpx.Request], httpx.Response]) -> ClobPublicClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=CLOB_BASE_URL)
    bucket = TokenBucket(capacity=1000.0, refill_per_second=1000.0)
    return ClobPublicClient(http_client=http, bucket=bucket, max_retries=2)


_BOOK_RESPONSE = {
    "market": "0xmarket",
    "asset_id": "0xasset",
    "timestamp": "1700000000",
    "hash": "abcdef",
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


def test_get_orderbook_returns_typed_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/book"
        assert request.url.params.get("token_id") == "0xasset"
        return httpx.Response(200, json=_BOOK_RESPONSE)

    with _build_client(handler) as client:
        ob = client.get_orderbook("0xasset")

    assert isinstance(ob, Orderbook)
    assert ob.market == "0xmarket"
    assert ob.asset_id == "0xasset"
    assert ob.tick_size == 0.01
    assert ob.last_trade_price == 0.45
    assert ob.neg_risk is False
    assert len(ob.bids) == 2
    assert ob.bids[0].price == 0.45
    assert ob.bids[0].size == 100.0


def test_orderbook_best_bid_and_ask_accessors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_BOOK_RESPONSE)

    with _build_client(handler) as client:
        ob = client.get_orderbook("0xasset")

    assert ob is not None
    assert ob.best_bid == OrderbookLevel(price=0.45, size=100.0)
    assert ob.best_ask == OrderbookLevel(price=0.46, size=150.0)


def test_orderbook_thin_book_returns_none_for_missing_sides() -> None:
    payload = {
        "market": "0xmarket",
        "asset_id": "0xasset",
        "timestamp": "1700000000",
        "bids": [],
        "asks": [],
        "neg_risk": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with _build_client(handler) as client:
        ob = client.get_orderbook("0xasset")

    assert ob is not None
    assert ob.bids == ()
    assert ob.asks == ()
    assert ob.best_bid is None
    assert ob.best_ask is None
    assert ob.last_trade_price is None
    assert ob.tick_size is None


def test_get_orderbook_returns_none_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no such token"})

    with _build_client(handler) as client:
        ob = client.get_orderbook("missing")

    assert ob is None


def test_get_price_returns_typed_quote() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("token_id") == "0xasset"
        assert request.url.params.get("side") == "buy"
        return httpx.Response(200, json={"price": "0.45"})

    with _build_client(handler) as client:
        quote = client.get_price("0xasset", side="buy")

    assert isinstance(quote, PriceQuote)
    assert quote.token_id == "0xasset"
    assert quote.side == "buy"
    assert quote.price == 0.45


def test_get_price_handles_null_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"price": None})

    with _build_client(handler) as client:
        quote = client.get_price("0xasset", side="sell")

    assert quote.price is None


def test_get_midpoint_returns_typed_quote() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/midpoint"
        return httpx.Response(200, json={"mid": "0.455"})

    with _build_client(handler) as client:
        mid = client.get_midpoint("0xasset")

    assert mid.token_id == "0xasset"
    assert mid.midpoint == 0.455


def test_get_midpoint_handles_alternate_field_name() -> None:
    """Some endpoints return ``midpoint`` instead of ``mid``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"midpoint": "0.50"})

    with _build_client(handler) as client:
        mid = client.get_midpoint("0xasset")

    assert mid.midpoint == 0.50


def test_get_last_trade_price_returns_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/last-trade-price"
        return httpx.Response(200, json={"price": "0.45"})

    with _build_client(handler) as client:
        price = client.get_last_trade_price("0xasset")

    assert price == 0.45


def test_get_last_trade_price_returns_none_when_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _build_client(handler) as client:
        price = client.get_last_trade_price("0xasset")

    assert price is None


_TRADE_PAYLOAD_1 = {
    "tx_hash": "0xdeadbeef",
    "market": "0xmarket",
    "asset_id": "0xasset",
    "price": "0.45",
    "size": "100",
    "side": "BUY",
    "trade_ts": 1700000000,
}
_TRADE_PAYLOAD_2 = {
    "tx_hash": "0xcafebabe",
    "market": "0xmarket",
    "asset_id": "0xasset",
    "price": "0.46",
    "size": "50",
    "side": "SELL",
    "trade_ts": 1700000060,
}


def test_list_trades_returns_typed_records() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_TRADE_PAYLOAD_1, _TRADE_PAYLOAD_2])

    with _build_client(handler) as client:
        trades, cursor = client.list_trades(market="0xmarket")

    assert len(trades) == 2
    assert trades[0].tx_hash == "0xdeadbeef"
    assert trades[0].price == 0.45
    assert trades[0].size == 100.0
    assert trades[0].side == "BUY"
    assert trades[0].trade_ts_seconds == 1700000000
    assert cursor is None


def test_list_trades_handles_envelope_with_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [_TRADE_PAYLOAD_1], "next_cursor": "page2"},
        )

    with _build_client(handler) as client:
        trades, cursor = client.list_trades(market="0xmarket")

    assert len(trades) == 1
    assert cursor == "page2"


def test_iter_trades_walks_pages_via_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("next_cursor", "")
        if cursor == "":
            return httpx.Response(
                200,
                json={"data": [_TRADE_PAYLOAD_1], "next_cursor": "p2"},
            )
        if cursor == "p2":
            return httpx.Response(
                200,
                json={"data": [_TRADE_PAYLOAD_2], "next_cursor": None},
            )
        return httpx.Response(200, json={"data": []})

    with _build_client(handler) as client:
        results = list(client.iter_trades(market="0xmarket"))

    assert len(results) == 2
    assert results[0].tx_hash == "0xdeadbeef"
    assert results[1].tx_hash == "0xcafebabe"


def test_unexpected_response_shape_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json="not a dict or list")

    with _build_client(handler) as client, pytest.raises(ClobClientError, match="unexpected"):
        client.get_orderbook("0xasset")


def test_persistent_500_surfaces_after_retry_budget() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with (
        _build_client(handler) as client,
        pytest.raises(Exception),  # noqa: B017 - retry-exhausted
    ):
        client.get_orderbook("0xasset")


def test_safe_float_handles_string_numbers() -> None:
    """The orderbook parser uses _safe_float to coerce string prices."""
    payload = {**_BOOK_RESPONSE, "tick_size": "0.005"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with _build_client(handler) as client:
        ob = client.get_orderbook("0xasset")

    assert ob is not None
    assert ob.tick_size == 0.005


def test_safe_float_returns_none_for_garbage() -> None:
    """A non-numeric string in a price field becomes None, not a crash."""
    payload = {**_BOOK_RESPONSE, "last_trade_price": "not-a-number"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with _build_client(handler) as client:
        ob = client.get_orderbook("0xasset")

    assert ob is not None
    assert ob.last_trade_price is None


def test_close_does_not_close_external_client() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"price": "0.5"}))
    http = httpx.Client(transport=transport, base_url=CLOB_BASE_URL)
    client = ClobPublicClient(http_client=http)
    client.close()
    assert http.is_closed is False
    http.close()


def test_owns_client_when_no_http_client_passed() -> None:
    client = ClobPublicClient()
    try:
        assert client._owns_client is True
    finally:
        client.close()


def test_raw_payload_preserved_on_orderbook() -> None:
    payload = {**_BOOK_RESPONSE, "extra_field": "value"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with _build_client(handler) as client:
        ob = client.get_orderbook("0xasset")

    assert ob is not None
    assert ob.raw["extra_field"] == "value"


def test_trade_with_missing_optional_fields_parses() -> None:
    """A trade payload missing side / timestamp still parses without error."""
    minimal = {
        "tx_hash": "0xabc",
        "market": "0xmarket",
        "asset_id": "0xasset",
        "price": "0.5",
        "size": "1",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[minimal])

    with _build_client(handler) as client:
        trades, _ = client.list_trades(market="0xmarket")

    assert len(trades) == 1
    assert trades[0].side is None
    assert trades[0].trade_ts_seconds is None


def test_retry_on_429_then_succeed() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 2:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json=_BOOK_RESPONSE)

    with _build_client(handler) as client:
        ob = client.get_orderbook("0xasset")

    assert ob is not None
    assert len(attempts) == 2
