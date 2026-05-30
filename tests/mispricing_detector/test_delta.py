"""T-MD-030 — probability and delta math tests."""

from __future__ import annotations

import math

import pytest

from razor_rooster.mispricing_detector.engines.delta import (
    MarketSnapshot,
    compute_delta,
    expected_value,
    log_odds_delta,
    market_probability_from,
)


def test_market_probability_from_midprice() -> None:
    prob, warnings = market_probability_from(
        MarketSnapshot(best_bid=0.40, best_ask=0.50, last_trade_price=0.45)
    )
    assert prob == pytest.approx(0.45)
    assert warnings == []


def test_market_probability_from_polarity_inverted() -> None:
    prob, _ = market_probability_from(
        MarketSnapshot(best_bid=0.40, best_ask=0.50, last_trade_price=0.45),
        polarity="inverted",
    )
    assert prob == pytest.approx(0.55)


def test_market_probability_from_falls_back_to_last_trade() -> None:
    prob, warnings = market_probability_from(
        MarketSnapshot(best_bid=None, best_ask=None, last_trade_price=0.30)
    )
    assert prob == pytest.approx(0.30)
    assert "degenerate_orderbook" in warnings


def test_market_probability_from_returns_none_when_all_missing() -> None:
    prob, warnings = market_probability_from(
        MarketSnapshot(best_bid=None, best_ask=None, last_trade_price=None)
    )
    assert prob is None
    assert "no_market_price" in warnings


def test_market_probability_clipped_to_unit_interval() -> None:
    prob, _ = market_probability_from(
        MarketSnapshot(best_bid=1.2, best_ask=1.5, last_trade_price=None)
    )
    assert prob == pytest.approx(1.0)
    prob, _ = market_probability_from(
        MarketSnapshot(best_bid=-0.1, best_ask=0.0, last_trade_price=None)
    )
    assert prob == pytest.approx(0.0)


def test_compute_delta_basic() -> None:
    assert compute_delta(0.30, 0.10) == pytest.approx(0.20)
    assert compute_delta(0.10, 0.30) == pytest.approx(-0.20)


def test_compute_delta_market_none() -> None:
    assert compute_delta(0.30, None) is None


def test_log_odds_delta_basic() -> None:
    delta = log_odds_delta(0.30, 0.10)
    expected = math.log(0.30 / 0.70) - math.log(0.10 / 0.90)
    assert delta == pytest.approx(expected)


def test_log_odds_delta_handles_boundary_prices() -> None:
    """Values of 0 and 1 produce finite log-odds via eps clipping."""
    a = log_odds_delta(0.5, 0.0)
    b = log_odds_delta(0.5, 1.0)
    assert a is not None and math.isfinite(a)
    assert b is not None and math.isfinite(b)
    assert a > 0  # market_p clipped near 0 -> very negative log-odds -> positive delta
    assert b < 0


def test_log_odds_delta_market_none() -> None:
    assert log_odds_delta(0.30, None) is None


def test_expected_value_returns_delta_for_binary() -> None:
    assert expected_value(0.20, 0.10) == pytest.approx(0.10)


def test_expected_value_none_at_boundaries() -> None:
    assert expected_value(0.20, 0.0) is None
    assert expected_value(0.20, 1.0) is None


def test_expected_value_none_when_market_missing() -> None:
    assert expected_value(0.20, None) is None
