"""Probability and delta math (T-MD-030; design §3.4).

Pure functions over numeric inputs. Polarity-aware: when a mapping
is ``inverted``, the YES outcome of the Polymarket market means the
event does NOT happen, so we flip the market probability before
deltaing against the model probability.

Edge cases:

- NULL prices return None (not zero).
- Boundary prices (0 or 1) are clipped to ``(eps, 1-eps)`` for
  log-odds math so the result is well-defined.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from razor_rooster.mispricing_detector.models import Polarity

_PROB_EPS: Final[float] = 1e-6


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Polymarket price-snapshot projection used by the delta engine."""

    best_bid: float | None
    best_ask: float | None
    last_trade_price: float | None
    volume_24h: float | None = None
    spread_bps: int | None = None


def market_probability_from(
    snapshot: MarketSnapshot, polarity: Polarity = "aligned"
) -> tuple[float | None, list[str]]:
    """Derive the market-implied probability for a binary YES outcome.

    Returns ``(prob, warnings)``. ``prob`` is None when no usable price
    is available; warnings is a list of strings (``degenerate_orderbook``,
    ``no_market_price``) describing why a fallback was applied.

    Polarity is applied here so callers downstream don't have to think
    about it: aligned → YES probability, inverted → 1-YES.
    """
    warnings: list[str] = []
    bid = snapshot.best_bid
    ask = snapshot.best_ask
    last = snapshot.last_trade_price

    if bid is not None and ask is not None:
        prob = (bid + ask) / 2.0
    elif last is not None:
        prob = last
        warnings.append("degenerate_orderbook")
    else:
        return None, ["no_market_price"]

    # Clip to [0, 1] in case of weird inputs.
    prob = max(0.0, min(1.0, prob))
    if polarity == "inverted":
        prob = 1.0 - prob
    return prob, warnings


def compute_delta(model_p: float, market_p: float | None) -> float | None:
    """``model_p - market_p`` in probability units. None when market_p is None."""
    if market_p is None:
        return None
    return float(model_p - market_p)


def log_odds_delta(model_p: float, market_p: float | None) -> float | None:
    """Log-odds delta with eps-clipping for boundary safety."""
    if market_p is None:
        return None
    return _log_odds(model_p) - _log_odds(market_p)


def expected_value(model_p: float, market_p: float | None) -> float | None:
    """Per-share expected value of buying the YES side at the market price.

    EV = model_p * (1 - market_p) - (1 - model_p) * market_p
       = model_p - market_p

    For binary YES markets the EV simplifies to the delta. We expose it
    as a separate function because future v1.1 work (multi-outcome
    markets, position-cost adjustments) will diverge from a simple delta.
    """
    if market_p is None or market_p in (0.0, 1.0):
        return None
    return compute_delta(model_p, market_p)


def _log_odds(p: float) -> float:
    """Log-odds with eps clipping so 0 and 1 produce finite values."""
    p_clip = max(_PROB_EPS, min(1.0 - _PROB_EPS, p))
    return math.log(p_clip / (1.0 - p_clip))
