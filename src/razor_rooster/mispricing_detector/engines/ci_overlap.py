"""Credible-interval overlap analysis (T-MD-031; design §3.4).

Decides whether the model's credible interval and the market's
bid-ask range overlap. When NULL inputs would make the answer
indeterminate, the function returns False — the safer default in
the comparison-surfacing pipeline (an unknown overlap doesn't
suppress a comparison from surfacing).
"""

from __future__ import annotations


def check_ci_overlap(
    *,
    model_ci_lower: float,
    model_ci_upper: float,
    market_bid: float | None,
    market_ask: float | None,
) -> bool:
    """True when the intervals overlap or touch.

    Cases:
    - Both bid and ask present: overlap test against [bid, ask].
    - Only one of bid/ask: cannot compute a market range; return False.
    - Neither: False.
    - model_ci_lower > model_ci_upper (degenerate): False.
    """
    if model_ci_lower > model_ci_upper:
        return False
    if market_bid is None or market_ask is None:
        return False
    if market_bid > market_ask:
        market_bid, market_ask = market_ask, market_bid
    return not (model_ci_upper < market_bid or model_ci_lower > market_ask)
