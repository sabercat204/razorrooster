"""Follow-up reasoning text builder (T-MON-023; design §3.8).

Template-driven. Same inputs always produce the same text. The output
is meant to be human-readable and ``report_generator``-consumable;
it never contains imperative phrasing (the position_engine's
forbidden-phrase catalog does not apply here, but the same framing
discipline holds — describe, don't direct).
"""

from __future__ import annotations

from collections.abc import Sequence

from razor_rooster.monitor.engines.alert_ranker import TIER_PRIORITY
from razor_rooster.monitor.engines.invalidation_evaluator import (
    InvalidationsResult,
)
from razor_rooster.monitor.models import (
    AlertTier,
    PrecursorSnapshot,
    ShiftResult,
)


def build_reasoning_text(
    *,
    class_id: str,
    condition_id: str,
    days_since_analysis: int,
    days_to_resolution: int | None,
    resolution_status: str,
    model_shift: ShiftResult,
    market_shift: ShiftResult,
    precursor_snapshot: Sequence[PrecursorSnapshot],
    invalidations: InvalidationsResult,
    primary_alert_tier: AlertTier | None,
    all_alert_tiers: Sequence[AlertTier],
    recommended_review: bool,
    time_decay_alert_days: int,
    venue: str = "polymarket",
) -> str:
    """Build the structured reasoning text for one follow-up."""
    lines: list[str] = []
    lines.append(
        f"Watched analysis for class {class_id!r} (mapped to market {condition_id!r} on {venue})."
    )

    age_label = "today" if days_since_analysis == 0 else f"{days_since_analysis} days ago"
    lines.append(f"Since the analysis ({age_label}):")

    # Resolution short-circuits much of the narrative.
    if resolution_status != "unresolved":
        outcome = resolution_status.removeprefix("resolved_")
        lines.append(f"  - The underlying market has resolved ({outcome}).")
    else:
        if model_shift.value is not None and model_shift.band is not None:
            lines.append(
                f"  - Model probability moved by {model_shift.value:+.4f} ({model_shift.band})."
            )
        else:
            lines.append("  - Model probability is not currently observable.")
        if market_shift.value is not None and market_shift.band is not None:
            lines.append(
                f"  - Market probability moved by {market_shift.value:+.4f} ({market_shift.band})."
            )
        else:
            lines.append("  - Market probability is not currently observable.")
        if precursor_snapshot:
            for snap in precursor_snapshot:
                lines.append(_describe_precursor(snap))
        triggered = [ev for ev in invalidations.evaluations if ev.status == "triggered"]
        if triggered:
            lines.append("  - Invalidation criteria triggered:")
            for ev in triggered:
                description = ev.criterion.get("description") or ev.criterion.get(
                    "type", "(criterion)"
                )
                lines.append(f"    * {description}")
        elif invalidations.evaluations:
            lines.append(
                f"  - Invalidation criteria evaluated; {invalidations.triggered_count} triggered."
            )
        if days_to_resolution is not None:
            if days_to_resolution <= time_decay_alert_days:
                lines.append(
                    f"  - {days_to_resolution} days remaining to resolution "
                    f"(below the {time_decay_alert_days}-day window)."
                )
            else:
                lines.append(f"  - {days_to_resolution} days remaining to resolution.")

    if recommended_review:
        primary_label = primary_alert_tier or "(unranked)"
        all_str = ", ".join(t for t in TIER_PRIORITY if t in all_alert_tiers)
        lines.append(f"Review recommended (primary alert: {primary_label}; all tiers: {all_str}).")
    else:
        lines.append("No review recommended at this time.")

    return "\n".join(lines)


# -- internals --------------------------------------------------------------


def _describe_precursor(snap: PrecursorSnapshot) -> str:
    """One-line description of a precursor snapshot for the reasoning text."""
    title = snap.title or snap.variable_id
    if snap.current_value is None:
        return f"  - Precursor {title!r} has no current value."
    if snap.threshold_crossed:
        if snap.current_fired and not snap.analysis_fired:
            return (
                f"  - Precursor {title!r} now fires "
                f"({snap.current_value:.3f} crossing threshold {snap.threshold})."
            )
        return (
            f"  - Precursor {title!r} no longer fires "
            f"({snap.current_value:.3f} below threshold {snap.threshold})."
        )
    if snap.analysis_value is not None:
        movement = snap.current_value - snap.analysis_value
        return (
            f"  - Precursor {title!r} moved from "
            f"{snap.analysis_value:.3f} to {snap.current_value:.3f} ({movement:+.3f})."
        )
    return f"  - Precursor {title!r} now at {snap.current_value:.3f}."
