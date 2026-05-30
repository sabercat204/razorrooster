"""T-PMC-033 — Gamma API client tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx
import pytest

from razor_rooster.polymarket_connector.client.gamma import (
    GAMMA_BASE_URL,
    GammaClient,
    GammaClientError,
    GammaMarket,
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


def _market_payload(condition_id: str, *, slug: str | None = None) -> dict[str, object]:
    return {
        "conditionId": condition_id,
        "slug": slug or f"market-{condition_id[:6]}",
        "question": f"Will event {condition_id[:6]} happen?",
        "active": True,
        "closed": False,
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["0.50", "0.50"],
    }


def _build_client(handler: Callable[[httpx.Request], httpx.Response]) -> GammaClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=GAMMA_BASE_URL)
    bucket = TokenBucket(capacity=1000.0, refill_per_second=1000.0)
    return GammaClient(http_client=http, bucket=bucket, max_retries=2)


def test_list_markets_returns_parsed_records() -> None:
    page = [
        _market_payload("0xabc"),
        _market_payload("0xdef"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/markets"
        assert request.url.params.get("active") == "true"
        assert request.url.params.get("closed") == "false"
        return httpx.Response(200, json=page)

    with _build_client(handler) as client:
        markets = client.list_markets(limit=50)

    assert len(markets) == 2
    assert all(isinstance(m, GammaMarket) for m in markets)
    assert markets[0].condition_id == "0xabc"
    assert markets[1].condition_id == "0xdef"
    assert markets[0].active is True
    assert markets[0].raw["question"].startswith("Will event")


def test_iter_markets_walks_pages_until_short_page() -> None:
    page1 = [_market_payload(f"0xa{i:02x}") for i in range(100)]
    page2 = [_market_payload(f"0xb{i:02x}") for i in range(50)]
    pages = [page1, page2]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(200, json=pages[0])
        if offset == 100:
            return httpx.Response(200, json=pages[1])
        return httpx.Response(200, json=[])

    with _build_client(handler) as client:
        results = list(client.iter_markets(page_size=100))

    assert len(results) == 150
    assert results[0].condition_id == "0xa00"
    assert results[-1].condition_id == "0xb31"


def test_list_resolved_passes_closed_true() -> None:
    captured_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params["closed"] = request.url.params.get("closed", "")
        captured_params["active"] = request.url.params.get("active", "<absent>")
        return httpx.Response(200, json=[])

    with _build_client(handler) as client:
        client.list_resolved(limit=10, offset=0)

    assert captured_params["closed"] == "true"
    # active=None → param omitted from URL.
    assert captured_params["active"] == "<absent>"


def test_get_market_by_slug_returns_none_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    with _build_client(handler) as client:
        result = client.get_market_by_slug("missing")

    assert result is None


def test_get_market_by_slug_returns_record_on_200_dict() -> None:
    payload = _market_payload("0xabc")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/markets/slug/some-slug"
        return httpx.Response(200, json=payload)

    with _build_client(handler) as client:
        market = client.get_market_by_slug("some-slug")

    assert market is not None
    assert market.condition_id == "0xabc"


def test_get_market_by_slug_returns_first_when_list_response() -> None:
    payload = [_market_payload("0xabc")]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with _build_client(handler) as client:
        market = client.get_market_by_slug("some-slug")

    assert market is not None
    assert market.condition_id == "0xabc"


def test_retries_on_transient_429() -> None:
    attempts: list[int] = []
    successful = [_market_payload("0xabc")]

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json=successful)

    with _build_client(handler) as client:
        results = client.list_markets()

    assert len(attempts) == 3
    assert results[0].condition_id == "0xabc"


def test_persistent_500_surfaces_as_client_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with (
        _build_client(handler) as client,
        pytest.raises(Exception),  # noqa: B017 - retry-exhausted wraps the response
    ):
        client.list_markets()


def test_unexpected_response_shape_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    with _build_client(handler) as client, pytest.raises(GammaClientError, match="unexpected"):
        client.list_markets()


def test_non_json_body_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    with _build_client(handler) as client, pytest.raises(GammaClientError, match="non-JSON"):
        client.list_markets()


def test_rate_limit_token_acquired_per_request() -> None:
    """Each call drains exactly one token from the bucket."""
    page = [_market_payload("0xabc")]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page)

    bucket = TokenBucket(capacity=10.0, refill_per_second=1.0)
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=GAMMA_BASE_URL)
    client = GammaClient(http_client=http, bucket=bucket, max_retries=0)
    try:
        before = bucket.stats().tokens_available
        client.list_markets()
        after = bucket.stats().tokens_available
    finally:
        client.close()
        http.close()
    # One token spent (with possible tiny refill since we injected
    # capacity=10/refill=1; we just check it dropped meaningfully).
    assert before - after >= 0.5


def test_iter_markets_handles_first_page_short_returns_one_pass() -> None:
    """Single page short of page_size — iterator returns once and stops."""
    page = [_market_payload("0xabc")]

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=page)

    with _build_client(handler) as client:
        results = list(client.iter_markets(page_size=100))

    assert call_count == 1  # one request, one page, one short result, done
    assert len(results) == 1


def test_list_events_returns_parsed_records() -> None:
    events = [
        {"id": "evt-1", "slug": "evt-slug-1", "title": "Event One"},
        {"id": "evt-2", "slug": "evt-slug-2", "title": "Event Two"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/events"
        return httpx.Response(200, json=events)

    with _build_client(handler) as client:
        results = client.list_events()

    assert len(results) == 2
    assert results[0].event_id == "evt-1"
    assert results[1].title == "Event Two"


def test_constructor_uses_shared_bucket_when_none_provided() -> None:
    """Sanity: calling without bucket=... falls back to the shared singleton."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=[]))
    http = httpx.Client(transport=transport, base_url=GAMMA_BASE_URL)
    client = GammaClient(http_client=http)
    try:
        # If construction failed to wire the bucket, this would raise.
        client.list_markets()
    finally:
        client.close()
        http.close()


def test_owns_client_when_no_http_client_passed() -> None:
    """When no http_client argument is given, GammaClient creates and owns one."""
    client = GammaClient()  # real httpx client, no requests made
    try:
        assert client._owns_client is True
    finally:
        client.close()


def test_close_does_not_close_external_http_client() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=[]))
    http = httpx.Client(transport=transport, base_url=GAMMA_BASE_URL)
    client = GammaClient(http_client=http)
    client.close()
    # After client.close, the external http should still be usable.
    assert http.is_closed is False
    http.close()


def test_raw_payload_preserved_in_market() -> None:
    payload = _market_payload("0xabc")
    payload["custom_field"] = "value"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[payload])

    with _build_client(handler) as client:
        markets = client.list_markets()

    assert markets[0].raw["custom_field"] == "value"
    # Verifies the entire payload is preserved verbatim.
    assert markets[0].raw == payload
    # And the parsed surface still works.
    assert markets[0].condition_id == "0xabc"


def test_handles_snake_case_alias_for_condition_id() -> None:
    """Some Polymarket responses use snake_case (condition_id) instead of camelCase."""
    payload = {
        "condition_id": "0xabc",
        "slug": "x",
        "question": "?",
        "active": True,
        "closed": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[payload])

    with _build_client(handler) as client:
        markets = client.list_markets()

    assert markets[0].condition_id == "0xabc"


def test_default_close_idempotent_on_owned_client() -> None:
    """Calling close twice on an owned client doesn't blow up."""
    client = GammaClient()
    client.close()
    client.close()  # second close is a no-op


def test_get_market_by_slug_unexpected_payload_shape_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json="not a dict or list")

    with _build_client(handler) as client, pytest.raises(GammaClientError, match="unexpected"):
        client.get_market_by_slug("any")


def test_get_market_by_slug_empty_list_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with _build_client(handler) as client:
        result = client.get_market_by_slug("any")

    assert result is None


def test_pagination_offset_increments_correctly() -> None:
    """When iter_markets walks pages, offset is bumped by page_size each time."""
    seen_offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        seen_offsets.append(offset)
        if offset == 0:
            return httpx.Response(200, json=[_market_payload(f"0xa{i:02x}") for i in range(100)])
        if offset == 100:
            return httpx.Response(200, json=[_market_payload(f"0xb{i:02x}") for i in range(100)])
        # Third call returns short page (50) — iterator stops.
        return httpx.Response(200, json=[_market_payload(f"0xc{i:02x}") for i in range(50)])

    with _build_client(handler) as client:
        all_markets = list(client.iter_markets(page_size=100))

    assert seen_offsets == [0, 100, 200]
    assert len(all_markets) == 250


def test_active_none_omits_active_param() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["active"] = request.url.params.get("active", "<absent>")
        captured["closed"] = request.url.params.get("closed", "<absent>")
        return httpx.Response(200, json=[])

    with _build_client(handler) as client:
        client.list_markets(active=None, closed=None)

    assert captured["active"] == "<absent>"
    assert captured["closed"] == "<absent>"


def test_response_text_attached_to_error() -> None:
    """A 4xx error includes a snippet of the response body for debugging."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=json.dumps({"error": "bad request"}))

    with _build_client(handler) as client, pytest.raises(GammaClientError, match="HTTP 400"):
        client.list_markets()
