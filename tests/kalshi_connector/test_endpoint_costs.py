"""T-KSI-030 — endpoint cost map acceptance tests."""

from __future__ import annotations

import pytest

from razor_rooster.kalshi_connector.client.endpoint_costs import (
    DEFAULT_TOKEN_COST,
    ENDPOINT_COSTS,
    cost_for,
    template_for_path,
)


def test_default_cost_matches_kalshi_documented() -> None:
    """All current v1 endpoints share the documented default."""
    assert DEFAULT_TOKEN_COST == 10
    for cost in ENDPOINT_COSTS.values():
        assert cost == DEFAULT_TOKEN_COST


@pytest.mark.parametrize(
    ("path", "expected_template"),
    [
        ("/series", "/series"),
        ("/series/INX", "/series/{series_ticker}"),
        ("/events", "/events"),
        ("/events/MYEVENT-26AUG", "/events/{event_ticker}"),
        ("/markets", "/markets"),
        ("/markets/KXFOO", "/markets/{ticker}"),
        ("/markets/KXFOO/orderbook", "/markets/{ticker}/orderbook"),
        ("/markets/KXFOO/candlesticks", "/markets/{ticker}/candlesticks"),
        ("/markets/trades", "/markets/trades"),
        ("/historical/cutoff", "/historical/cutoff"),
        ("/historical/markets", "/historical/markets"),
        ("/historical/markets/OLDMKT", "/historical/markets/{ticker}"),
        (
            "/historical/markets/OLDMKT/candlesticks",
            "/historical/markets/{ticker}/candlesticks",
        ),
        ("/historical/trades", "/historical/trades"),
    ],
)
def test_template_matching(path: str, expected_template: str) -> None:
    assert template_for_path(path) == expected_template


def test_unknown_path_falls_back_to_default() -> None:
    assert template_for_path("/something/new") is None
    assert cost_for("/something/new") == DEFAULT_TOKEN_COST


def test_full_url_matches_template() -> None:
    """Full URLs strip host correctly to match templates."""
    full = "https://external-api.kalshi.com/trade-api/v2/markets/KXFOO/orderbook"
    assert template_for_path(full) == "/markets/{ticker}/orderbook"


def test_path_with_query_string_strips_correctly() -> None:
    assert template_for_path("/markets?status=open&limit=100") == "/markets"


def test_path_with_v2_prefix_normalizes() -> None:
    assert template_for_path("/v2/markets/KXFOO") == "/markets/{ticker}"


def test_orderbook_template_takes_priority_over_ticker() -> None:
    """Longest template wins so /orderbook isn't swallowed by /{ticker}."""
    assert template_for_path("/markets/KXFOO/orderbook") == "/markets/{ticker}/orderbook"


def test_cost_for_known_endpoint() -> None:
    assert cost_for("/markets") == 10
    assert cost_for("/markets/KXFOO/orderbook") == 10


def test_template_endings_normalize_trailing_slashes() -> None:
    assert template_for_path("/markets/") == "/markets"


def test_path_normalization_is_idempotent() -> None:
    assert template_for_path("/markets//KXFOO") == "/markets/{ticker}"
