"""Liquidity-feasibility computation (T-PE-032; REQ-PE-CMP-006).

Decides whether the suggested dollar size would be too large relative
to the market's recent volume, and clamps the suggested fraction down
to keep the dollar size at the threshold when needed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LiquidityResult:
    """Result of the liquidity-feasibility check."""

    pct_of_24h_volume: float
    suggested_fraction_after_clamp: float
    suggested_dollar_size_after_clamp: float
    clamped: bool
    low_liquidity_flag: bool


def compute_liquidity(
    *,
    suggested_fraction: float,
    bankroll_usd: float,
    volume_24h: float | None,
    threshold_pct_of_volume: float,
) -> LiquidityResult:
    """Compute liquidity metrics and clamp the fraction if needed.

    When ``volume_24h`` is None or zero, the position is infinitely
    illiquid for analytical purposes; the result flags
    ``low_liquidity`` and clamps the suggested fraction to zero.
    """
    suggested_dollars = bankroll_usd * suggested_fraction
    if volume_24h is None or volume_24h <= 0.0:
        return LiquidityResult(
            pct_of_24h_volume=math.inf,
            suggested_fraction_after_clamp=0.0,
            suggested_dollar_size_after_clamp=0.0,
            clamped=True,
            low_liquidity_flag=True,
        )
    pct = suggested_dollars / volume_24h
    if pct <= threshold_pct_of_volume:
        return LiquidityResult(
            pct_of_24h_volume=float(pct),
            suggested_fraction_after_clamp=float(suggested_fraction),
            suggested_dollar_size_after_clamp=float(suggested_dollars),
            clamped=False,
            low_liquidity_flag=False,
        )
    # Clamp dollars to threshold * volume; back-derive fraction.
    clamped_dollars = volume_24h * threshold_pct_of_volume
    clamped_fraction = clamped_dollars / bankroll_usd if bankroll_usd > 0 else 0.0
    return LiquidityResult(
        pct_of_24h_volume=float(pct),
        suggested_fraction_after_clamp=float(clamped_fraction),
        suggested_dollar_size_after_clamp=float(clamped_dollars),
        clamped=True,
        low_liquidity_flag=True,
    )
