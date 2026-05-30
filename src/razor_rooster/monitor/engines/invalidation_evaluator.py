"""Invalidation-criterion evaluator (T-MON-021; design §3.6).

Evaluates each criterion stored on a ``position_engine.Analysis``
against current state. Three known criterion types from
``position_engine.engines.invalidation``:

- ``precursor_shift``: a precursor variable crosses its threshold in
  a specified direction.
- ``market_move``: market_p moves to a level that would invalidate
  the original signal.
- ``general_caveat``: untestable caveats. These never trigger; they
  surface as ``cannot_evaluate``.

Unknown criterion types are returned as ``cannot_evaluate`` with an
explanatory ``reason`` so they show up in the follow-up record
without breaking the cycle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from razor_rooster.monitor.models import InvalidationEvaluation, InvalidationStatus


@dataclass(frozen=True, slots=True)
class InvalidationsResult:
    """Aggregate result of evaluating a list of criteria."""

    evaluations: tuple[InvalidationEvaluation, ...]
    triggered_count: int


def evaluate_invalidations(
    *,
    invalidation_criteria: Sequence[Mapping[str, Any]] | None,
    current_precursors: Sequence[Mapping[str, Any]] | None,
    current_market_p: float | None,
) -> InvalidationsResult:
    """Evaluate all criteria and return the typed result."""
    if not invalidation_criteria:
        return InvalidationsResult(evaluations=(), triggered_count=0)

    precursor_index = {
        str(entry.get("variable_id")): entry
        for entry in (current_precursors or [])
        if isinstance(entry, Mapping)
    }

    evaluations: list[InvalidationEvaluation] = []
    triggered = 0
    for raw in invalidation_criteria:
        if not isinstance(raw, Mapping):
            evaluations.append(
                InvalidationEvaluation(
                    criterion={},
                    status="cannot_evaluate",
                    reason="criterion is not a mapping",
                )
            )
            continue
        ctype = str(raw.get("type", ""))
        if ctype == "precursor_shift":
            ev = _evaluate_precursor_shift(raw, precursor_index)
        elif ctype == "market_move":
            ev = _evaluate_market_move(raw, current_market_p)
        elif ctype == "general_caveat":
            ev = InvalidationEvaluation(
                criterion=dict(raw),
                status="cannot_evaluate",
                reason="general caveat is not a mechanically testable criterion",
            )
        else:
            ev = InvalidationEvaluation(
                criterion=dict(raw),
                status="cannot_evaluate",
                reason=f"unknown criterion type: {ctype!r}",
            )
        evaluations.append(ev)
        if ev.status == "triggered":
            triggered += 1
    return InvalidationsResult(evaluations=tuple(evaluations), triggered_count=triggered)


def evaluation_to_dict(ev: InvalidationEvaluation) -> dict[str, Any]:
    """Serialize an evaluation for JSON storage."""
    return {
        "criterion": dict(ev.criterion),
        "status": ev.status,
        "current_value": ev.current_value,
        "reason": ev.reason,
    }


# -- internals --------------------------------------------------------------


def _evaluate_precursor_shift(
    criterion: Mapping[str, Any],
    precursor_index: Mapping[str, Mapping[str, Any]],
) -> InvalidationEvaluation:
    variable_id = str(criterion.get("variable_id", ""))
    threshold = criterion.get("threshold")
    direction = str(criterion.get("direction", "high_signals_event"))
    description = str(criterion.get("description", ""))

    current = precursor_index.get(variable_id)
    if current is None or threshold is None:
        return InvalidationEvaluation(
            criterion=dict(criterion),
            status="cannot_evaluate",
            reason=(
                f"precursor {variable_id!r} not found in current scan"
                if current is None
                else "criterion missing threshold"
            ),
        )
    current_value = current.get("current_value")
    if current_value is None:
        return InvalidationEvaluation(
            criterion=dict(criterion),
            status="cannot_evaluate",
            reason=f"precursor {variable_id!r} has no current value",
        )
    current_value_f = float(current_value)
    threshold_f = float(threshold)

    # Per position_engine invalidation extraction:
    # - For a fired precursor (the criterion was set when fired=True),
    #   the criterion describes a "drops back below threshold" trigger.
    # - For a non-fired precursor, the criterion describes a
    #   "crosses above threshold" trigger.
    # The criterion's description text discloses which case applies; we
    # also have the direction field for high/low semantics.
    triggered = False
    if "drops back below" in description.lower():
        triggered = (
            current_value_f < threshold_f
            if direction == "high_signals_event"
            else current_value_f > threshold_f
        )
    elif "crosses above" in description.lower():
        triggered = (
            current_value_f > threshold_f
            if direction == "high_signals_event"
            else current_value_f < threshold_f
        )
    else:
        # Fallback: use direction + threshold to compare against current.
        triggered = (
            current_value_f >= threshold_f
            if direction == "high_signals_event"
            else current_value_f <= threshold_f
        )
    return InvalidationEvaluation(
        criterion=dict(criterion),
        status="triggered" if triggered else "not_triggered",
        current_value=current_value_f,
    )


def _evaluate_market_move(
    criterion: Mapping[str, Any], current_market_p: float | None
) -> InvalidationEvaluation:
    threshold = criterion.get("threshold")
    direction = str(criterion.get("direction", ""))
    if current_market_p is None or threshold is None:
        return InvalidationEvaluation(
            criterion=dict(criterion),
            status="cannot_evaluate",
            reason="missing current market_p or criterion threshold",
        )
    threshold_f = float(threshold)
    current_p = float(current_market_p)
    if direction == "market_p_falls_to":
        triggered = current_p <= threshold_f
    elif direction == "market_p_rises_to":
        triggered = current_p >= threshold_f
    else:
        return InvalidationEvaluation(
            criterion=dict(criterion),
            status="cannot_evaluate",
            reason=f"unknown market_move direction: {direction!r}",
        )
    return InvalidationEvaluation(
        criterion=dict(criterion),
        status="triggered" if triggered else "not_triggered",
        current_value=current_p,
    )


__all__ = [
    "InvalidationStatus",
    "InvalidationsResult",
    "evaluate_invalidations",
    "evaluation_to_dict",
]
