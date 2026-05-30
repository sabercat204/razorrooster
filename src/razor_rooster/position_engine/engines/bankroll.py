"""Bankroll-survival scenarios (T-PE-031; REQ-PE-CMP-005).

Computes the resulting bankroll fraction after N consecutive adverse
outcomes, assuming the suggested fraction is reapplied each time.
"""

from __future__ import annotations

from collections.abc import Sequence


def compute_survival(
    suggested_fraction: float,
    *,
    scenarios: Sequence[int] = (1, 3, 5),
) -> dict[int, float]:
    """Return ``{n_losses: bankroll_fraction_remaining}`` for each scenario.

    Model: each round the operator stakes ``suggested_fraction`` of
    current bankroll and loses it. After N losses, bankroll is
    ``(1 - f)^N`` of the starting balance.
    """
    f = max(0.0, min(1.0, suggested_fraction))
    return {n: float((1.0 - f) ** n) for n in scenarios}
