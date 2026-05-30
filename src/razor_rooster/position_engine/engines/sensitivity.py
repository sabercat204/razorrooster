"""Model-error sensitivity analysis (T-PE-034; OQ-PE-004 resolution).

Computes how the suggested Kelly fraction changes when ``model_p`` is
perturbed by ±x percentage points. Used by the renderer's --verbose
mode and persisted on every analysis.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from razor_rooster.position_engine.engines.kelly import apply_pipeline


def compute_sensitivity(
    *,
    model_probability: float,
    market_probability: float | None,
    kelly_fraction_default: float,
    max_single_position_pct: float,
    perturbations: Sequence[float] = (0.10, 0.20),
) -> dict[str, Any]:
    """Return a JSON-serializable structure with model_p variants and the
    resulting suggested fractions.

    Each perturbation produces two rows: model_p + delta and
    model_p - delta. Probability values are clipped to (0, 1).
    """
    rows: list[dict[str, Any]] = []
    for delta in perturbations:
        for sign in (+1.0, -1.0):
            perturbed = max(0.001, min(0.999, model_probability + sign * delta))
            result = apply_pipeline(
                model_p=perturbed,
                market_p=market_probability,
                kelly_fraction_default=kelly_fraction_default,
                max_single_position_pct=max_single_position_pct,
            )
            rows.append(
                {
                    "delta_pct": float(sign * delta),
                    "model_p_perturbed": float(perturbed),
                    "kelly_unclamped": result.kelly_unclamped,
                    "suggested_fraction": result.suggested_after_max_cap,
                }
            )
    return {
        "perturbations": rows,
        "method": "kelly_recompute_with_perturbed_model_p",
    }
