"""Invalidation-criteria extraction (T-PE-033; REQ-PE-CMP-007).

Generates a structured list of "if observable X moves to Y, revisit
this analysis" criteria from the underlying scanner trace plus the
current comparison state.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def extract_criteria(
    *,
    scanner_trace: Mapping[str, Any] | None,
    model_probability: float,
    market_probability: float | None,
    surfacing_threshold_log_odds: float = 0.5,
) -> list[dict[str, Any]]:
    """Return a list of invalidation criteria for one comparison.

    Three categories of criteria:

    1. **Precursor-shift criteria** — for each precursor that fired in
       the scanner trace, "if the precursor drops below threshold X,
       the model signal weakens." For each non-fired precursor with a
       strong threshold, "if it climbs above threshold X, the signal
       changes direction."
    2. **Market-move criteria** — the market price at which the
       absolute log-odds delta would fall below the surfacing
       threshold. Two-sided.
    3. **General caveats** — when low-confidence or missing-data
       conditions hold, an explicit caveat criterion fires.
    """
    out: list[dict[str, Any]] = []

    if scanner_trace:
        precursors = scanner_trace.get("precursors") or []
        for entry in precursors:
            if not isinstance(entry, dict):
                continue
            variable_id = entry.get("variable_id") or "(unnamed)"
            title = entry.get("title") or variable_id
            threshold = entry.get("threshold")
            current = entry.get("current_value")
            fired = bool(entry.get("fired"))
            direction = entry.get("direction") or "high_signals_event"
            if threshold is None or current is None:
                continue
            if fired:
                out.append(
                    {
                        "type": "precursor_shift",
                        "variable_id": variable_id,
                        "title": title,
                        "direction": direction,
                        "threshold": float(threshold),
                        "current_value": float(current),
                        "description": (
                            f"if {variable_id!r} drops back below "
                            f"{float(threshold):.3f}, the model signal weakens"
                        ),
                    }
                )
            else:
                out.append(
                    {
                        "type": "precursor_shift",
                        "variable_id": variable_id,
                        "title": title,
                        "direction": direction,
                        "threshold": float(threshold),
                        "current_value": float(current),
                        "description": (
                            f"if {variable_id!r} crosses above "
                            f"{float(threshold):.3f}, the model signal would change"
                        ),
                    }
                )

    if market_probability is not None and market_probability not in (0.0, 1.0):
        # Solve for the market probability at which |log_odds_delta|
        # falls below the surfacing threshold.
        market_low = _market_p_for_log_odds(model_probability, surfacing_threshold_log_odds)
        market_high = _market_p_for_log_odds(model_probability, -surfacing_threshold_log_odds)
        if market_low is not None:
            out.append(
                {
                    "type": "market_move",
                    "direction": "market_p_falls_to",
                    "threshold": float(market_low),
                    "current_value": float(market_probability),
                    "description": (
                        f"if market_p moves to {float(market_low):.4f} "
                        "the surfacing threshold no longer holds (model "
                        "and market converge)"
                    ),
                }
            )
        if market_high is not None:
            out.append(
                {
                    "type": "market_move",
                    "direction": "market_p_rises_to",
                    "threshold": float(market_high),
                    "current_value": float(market_probability),
                    "description": (
                        f"if market_p moves to {float(market_high):.4f} "
                        "the surfacing threshold no longer holds (market "
                        "moves past the model)"
                    ),
                }
            )

    if scanner_trace:
        warnings = scanner_trace.get("warnings") or []
        if "low_signature_confidence" in warnings or "low_confidence_signatures" in warnings:
            out.append(
                {
                    "type": "general_caveat",
                    "description": (
                        "model signature confidence is low; revisit if "
                        "additional signals strengthen the model side"
                    ),
                }
            )
        if "library_stale_warning" in warnings or "library_stale" in warnings:
            out.append(
                {
                    "type": "general_caveat",
                    "description": (
                        "pattern_library is stale; revisit after the next library refresh"
                    ),
                }
            )

    return out


# -- internals --------------------------------------------------------------


def _market_p_for_log_odds(model_p: float, target_delta: float) -> float | None:
    """Solve for the ``market_p`` that produces ``log_odds_delta = target``.

    log_odds(model) - log_odds(market) = target
    log_odds(market) = log_odds(model) - target
    market = sigmoid(log_odds(model) - target)
    """
    p = max(1e-6, min(1.0 - 1e-6, model_p))
    log_odds_model = math.log(p / (1.0 - p))
    target_log_odds_market = log_odds_model - target_delta
    market = 1.0 / (1.0 + math.exp(-target_log_odds_market))
    if not (0.0 < market < 1.0):
        return None
    return float(market)
