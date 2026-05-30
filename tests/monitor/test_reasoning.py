"""T-MON-023 — reasoning text builder tests."""

from __future__ import annotations

from razor_rooster.monitor.engines.invalidation_evaluator import (
    InvalidationsResult,
)
from razor_rooster.monitor.engines.reasoning import build_reasoning_text
from razor_rooster.monitor.models import (
    InvalidationEvaluation,
    PrecursorSnapshot,
    ShiftResult,
)


def _baseline_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "class_id": "cls-1",
        "condition_id": "cond-1",
        "days_since_analysis": 5,
        "days_to_resolution": 30,
        "resolution_status": "unresolved",
        "model_shift": ShiftResult(value=0.02, band="minor"),
        "market_shift": ShiftResult(value=0.01, band="minor"),
        "precursor_snapshot": (),
        "invalidations": InvalidationsResult(evaluations=(), triggered_count=0),
        "primary_alert_tier": None,
        "all_alert_tiers": (),
        "recommended_review": False,
        "time_decay_alert_days": 7,
    }
    base.update(overrides)
    return base


def test_deterministic_output() -> None:
    """Same inputs produce identical text every time."""
    kwargs = _baseline_kwargs()
    a = build_reasoning_text(**kwargs)  # type: ignore[arg-type]
    b = build_reasoning_text(**kwargs)  # type: ignore[arg-type]
    assert a == b


def test_resolution_short_circuit_skips_shift_lines() -> None:
    text = build_reasoning_text(
        **_baseline_kwargs(  # type: ignore[arg-type]
            resolution_status="resolved_yes",
            primary_alert_tier="resolution",
            all_alert_tiers=("resolution",),
            recommended_review=True,
        )
    )
    assert "has resolved" in text
    assert "yes" in text
    # The shift lines are suppressed when resolved.
    assert "Model probability moved" not in text
    assert "Market probability moved" not in text


def test_recommended_review_framing_lists_tiers_in_priority_order() -> None:
    text = build_reasoning_text(
        **_baseline_kwargs(  # type: ignore[arg-type]
            primary_alert_tier="material_shift",
            all_alert_tiers=("material_shift", "time_decay"),
            recommended_review=True,
        )
    )
    assert "Review recommended" in text
    assert "primary alert: material_shift" in text
    # Tiers appear in TIER_PRIORITY order.
    idx_shift = text.find("material_shift")
    idx_time = text.find("time_decay", idx_shift)
    assert idx_shift != -1 and idx_time > idx_shift


def test_no_review_framing() -> None:
    text = build_reasoning_text(**_baseline_kwargs())  # type: ignore[arg-type]
    assert "No review recommended" in text


def test_age_label_today_for_zero_days() -> None:
    text = build_reasoning_text(
        **_baseline_kwargs(days_since_analysis=0)  # type: ignore[arg-type]
    )
    assert "today" in text


def test_age_label_pluralized_otherwise() -> None:
    text = build_reasoning_text(
        **_baseline_kwargs(days_since_analysis=4)  # type: ignore[arg-type]
    )
    assert "4 days ago" in text


def test_unobservable_model_shift_explains_absence() -> None:
    text = build_reasoning_text(
        **_baseline_kwargs(  # type: ignore[arg-type]
            model_shift=ShiftResult(value=None, band=None)
        )
    )
    assert "Model probability is not currently observable" in text


def test_unobservable_market_shift_explains_absence() -> None:
    text = build_reasoning_text(
        **_baseline_kwargs(  # type: ignore[arg-type]
            market_shift=ShiftResult(value=None, band=None)
        )
    )
    assert "Market probability is not currently observable" in text


def test_precursor_now_fires_line() -> None:
    snap = PrecursorSnapshot(
        variable_id="v1",
        title="Variable One",
        threshold=5.0,
        direction="high_signals_event",
        analysis_value=3.0,
        current_value=6.0,
        analysis_fired=False,
        current_fired=True,
    )
    text = build_reasoning_text(
        **_baseline_kwargs(precursor_snapshot=(snap,))  # type: ignore[arg-type]
    )
    assert "now fires" in text
    assert "Variable One" in text


def test_precursor_no_longer_fires_line() -> None:
    snap = PrecursorSnapshot(
        variable_id="v1",
        title="Variable One",
        threshold=5.0,
        direction="high_signals_event",
        analysis_value=6.0,
        current_value=3.0,
        analysis_fired=True,
        current_fired=False,
    )
    text = build_reasoning_text(
        **_baseline_kwargs(precursor_snapshot=(snap,))  # type: ignore[arg-type]
    )
    assert "no longer fires" in text


def test_precursor_movement_without_crossing() -> None:
    snap = PrecursorSnapshot(
        variable_id="v1",
        title="Variable One",
        threshold=5.0,
        direction="high_signals_event",
        analysis_value=3.0,
        current_value=4.0,
        analysis_fired=False,
        current_fired=False,
    )
    text = build_reasoning_text(
        **_baseline_kwargs(precursor_snapshot=(snap,))  # type: ignore[arg-type]
    )
    assert "moved from" in text
    assert "+1" in text or "+1.000" in text


def test_invalidation_triggered_lists_descriptions() -> None:
    triggered = InvalidationEvaluation(
        criterion={
            "type": "precursor_shift",
            "description": "Variable v1 drops back below 5.0.",
        },
        status="triggered",
    )
    text = build_reasoning_text(
        **_baseline_kwargs(  # type: ignore[arg-type]
            invalidations=InvalidationsResult(evaluations=(triggered,), triggered_count=1),
            primary_alert_tier="invalidation_triggered",
            all_alert_tiers=("invalidation_triggered",),
            recommended_review=True,
        )
    )
    assert "Invalidation criteria triggered" in text
    assert "drops back below" in text


def test_no_invalidations_triggered_summary_line() -> None:
    not_triggered = InvalidationEvaluation(
        criterion={"type": "market_move", "direction": "market_p_falls_to"},
        status="not_triggered",
    )
    text = build_reasoning_text(
        **_baseline_kwargs(  # type: ignore[arg-type]
            invalidations=InvalidationsResult(evaluations=(not_triggered,), triggered_count=0)
        )
    )
    assert "Invalidation criteria evaluated" in text
    assert "0 triggered" in text


def test_time_decay_inside_window_explicit() -> None:
    text = build_reasoning_text(
        **_baseline_kwargs(  # type: ignore[arg-type]
            days_to_resolution=3,
            time_decay_alert_days=7,
        )
    )
    assert "3 days remaining" in text
    assert "7-day window" in text


def test_time_decay_outside_window_does_not_mention_threshold() -> None:
    text = build_reasoning_text(
        **_baseline_kwargs(  # type: ignore[arg-type]
            days_to_resolution=30,
            time_decay_alert_days=7,
        )
    )
    assert "30 days remaining to resolution." in text
    assert "7-day window" not in text


def test_includes_class_and_market_ids() -> None:
    text = build_reasoning_text(
        **_baseline_kwargs(  # type: ignore[arg-type]
            class_id="my-class", condition_id="my-market"
        )
    )
    assert "my-class" in text
    assert "my-market" in text
