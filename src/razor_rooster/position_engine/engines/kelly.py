"""Kelly fraction math (T-PE-030; design §3.4).

Pure functions. Edge cases (market_p in {0, 1}, model_p in {0, 1},
NULL inputs) handled deliberately. The unclamped value is always
returned alongside the clamped/applied form so the analysis can
be transparent about how aggressive the math wanted to go.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

_PROB_EPS: Final[float] = 1e-6


@dataclass(frozen=True, slots=True)
class KellyResult:
    """Output of Kelly + clamping pipeline."""

    kelly_unclamped: float
    kelly_clamped_zero: float
    suggested_after_default: float
    suggested_after_max_cap: float
    kelly_negative: bool
    clamped_by_max_cap: bool


def kelly_fraction(model_p: float, market_p: float | None) -> float:
    """Theoretical Kelly fraction for buying the YES side at ``market_p``.

    For a binary YES market priced at ``market_p`` (cost per share),
    a YES win pays $1 (so net profit per share is ``1 - market_p``)
    and a YES loss pays $0 (so loss is ``market_p``).

    Kelly: ``f* = (p * b - q) / b`` where ``b = (1 - market_p) / market_p``,
    ``p = model_p``, ``q = 1 - p``. Simplifies to:

        f* = (p - market_p) / (1 - market_p)

    Returns the unclamped Kelly value (can be negative).

    Edge cases:
    - ``market_p`` is None: return 0.0 (no signal to size against).
    - ``market_p`` is 0: would-be infinite Kelly; clipped to its
      eps-clipped equivalent for finite math.
    - ``market_p`` is 1: would-be undefined; clipped likewise.
    - ``model_p`` outside (0, 1): clipped likewise.
    """
    if market_p is None:
        return 0.0
    p = max(_PROB_EPS, min(1.0 - _PROB_EPS, model_p))
    m = max(_PROB_EPS, min(1.0 - _PROB_EPS, market_p))
    return float((p - m) / (1.0 - m))


def apply_pipeline(
    *,
    model_p: float,
    market_p: float | None,
    kelly_fraction_default: float,
    max_single_position_pct: float,
) -> KellyResult:
    """Run the full Kelly clamping pipeline.

    Order: unclamped → clip-to-zero → multiply by half-Kelly default
    → clamp by max_single_position_pct.
    """
    unclamped = kelly_fraction(model_p, market_p)
    kelly_negative = unclamped < 0.0
    clamped_zero = max(0.0, unclamped)
    after_default = float(kelly_fraction_default * clamped_zero)
    clamped_by_cap = after_default > max_single_position_pct
    after_cap = min(after_default, max_single_position_pct)
    return KellyResult(
        kelly_unclamped=unclamped,
        kelly_clamped_zero=clamped_zero,
        suggested_after_default=after_default,
        suggested_after_max_cap=after_cap,
        kelly_negative=kelly_negative,
        clamped_by_max_cap=clamped_by_cap,
    )
