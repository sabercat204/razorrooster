"""T-MON-021 — invalidation evaluator tests."""

from __future__ import annotations

from razor_rooster.monitor.engines.invalidation_evaluator import (
    evaluate_invalidations,
    evaluation_to_dict,
)


def test_no_criteria_returns_empty_result() -> None:
    result = evaluate_invalidations(
        invalidation_criteria=None,
        current_precursors=None,
        current_market_p=0.30,
    )
    assert result.evaluations == ()
    assert result.triggered_count == 0


def test_precursor_drops_back_below_triggered() -> None:
    criterion = {
        "type": "precursor_shift",
        "variable_id": "v1",
        "threshold": 5.0,
        "direction": "high_signals_event",
        "description": "Variable v1 drops back below 5.0.",
    }
    current = [{"variable_id": "v1", "current_value": 3.5}]
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=current,
        current_market_p=None,
    )
    assert result.triggered_count == 1
    assert result.evaluations[0].status == "triggered"
    assert result.evaluations[0].current_value == 3.5


def test_precursor_drops_back_below_not_triggered_when_still_high() -> None:
    criterion = {
        "type": "precursor_shift",
        "variable_id": "v1",
        "threshold": 5.0,
        "direction": "high_signals_event",
        "description": "Variable v1 drops back below 5.0.",
    }
    current = [{"variable_id": "v1", "current_value": 6.5}]
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=current,
        current_market_p=None,
    )
    assert result.triggered_count == 0
    assert result.evaluations[0].status == "not_triggered"


def test_precursor_crosses_above_triggered() -> None:
    criterion = {
        "type": "precursor_shift",
        "variable_id": "v1",
        "threshold": 5.0,
        "direction": "high_signals_event",
        "description": "Variable v1 crosses above 5.0.",
    }
    current = [{"variable_id": "v1", "current_value": 7.0}]
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=current,
        current_market_p=None,
    )
    assert result.triggered_count == 1
    assert result.evaluations[0].status == "triggered"


def test_precursor_low_signals_event_inverts() -> None:
    """For 'low_signals_event' direction, 'crosses above' fires when below."""
    criterion = {
        "type": "precursor_shift",
        "variable_id": "v1",
        "threshold": 5.0,
        "direction": "low_signals_event",
        "description": "Variable v1 crosses above 5.0.",
    }
    current = [{"variable_id": "v1", "current_value": 3.0}]
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=current,
        current_market_p=None,
    )
    assert result.evaluations[0].status == "triggered"


def test_precursor_missing_from_current_returns_cannot_evaluate() -> None:
    criterion = {
        "type": "precursor_shift",
        "variable_id": "v1",
        "threshold": 5.0,
        "direction": "high_signals_event",
        "description": "Variable v1 drops back below 5.0.",
    }
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=[],
        current_market_p=None,
    )
    assert result.triggered_count == 0
    ev = result.evaluations[0]
    assert ev.status == "cannot_evaluate"
    assert ev.reason is not None and "not found" in ev.reason


def test_precursor_with_no_current_value_returns_cannot_evaluate() -> None:
    criterion = {
        "type": "precursor_shift",
        "variable_id": "v1",
        "threshold": 5.0,
        "direction": "high_signals_event",
        "description": "Variable v1 drops back below 5.0.",
    }
    current = [{"variable_id": "v1", "current_value": None}]
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=current,
        current_market_p=None,
    )
    assert result.evaluations[0].status == "cannot_evaluate"


def test_market_move_falls_to_triggered() -> None:
    criterion = {
        "type": "market_move",
        "direction": "market_p_falls_to",
        "threshold": 0.20,
    }
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=None,
        current_market_p=0.18,
    )
    assert result.triggered_count == 1
    assert result.evaluations[0].status == "triggered"
    assert result.evaluations[0].current_value == 0.18


def test_market_move_rises_to_triggered() -> None:
    criterion = {
        "type": "market_move",
        "direction": "market_p_rises_to",
        "threshold": 0.50,
    }
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=None,
        current_market_p=0.55,
    )
    assert result.evaluations[0].status == "triggered"


def test_market_move_not_triggered() -> None:
    criterion = {
        "type": "market_move",
        "direction": "market_p_falls_to",
        "threshold": 0.20,
    }
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=None,
        current_market_p=0.30,
    )
    assert result.evaluations[0].status == "not_triggered"


def test_market_move_missing_current_returns_cannot_evaluate() -> None:
    criterion = {
        "type": "market_move",
        "direction": "market_p_falls_to",
        "threshold": 0.20,
    }
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=None,
        current_market_p=None,
    )
    assert result.evaluations[0].status == "cannot_evaluate"
    assert result.evaluations[0].reason is not None


def test_market_move_unknown_direction_returns_cannot_evaluate() -> None:
    criterion = {
        "type": "market_move",
        "direction": "weird_direction",
        "threshold": 0.20,
    }
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=None,
        current_market_p=0.30,
    )
    ev = result.evaluations[0]
    assert ev.status == "cannot_evaluate"
    assert ev.reason is not None and "weird_direction" in ev.reason


def test_general_caveat_always_cannot_evaluate() -> None:
    criterion = {
        "type": "general_caveat",
        "description": "Significant policy change shifts base rates.",
    }
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=None,
        current_market_p=None,
    )
    assert result.triggered_count == 0
    assert result.evaluations[0].status == "cannot_evaluate"


def test_unknown_criterion_type_falls_back_to_cannot_evaluate() -> None:
    criterion = {"type": "mystery_type", "value": 42}
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=None,
        current_market_p=None,
    )
    assert result.evaluations[0].status == "cannot_evaluate"
    assert result.evaluations[0].reason is not None
    assert "mystery_type" in result.evaluations[0].reason


def test_non_mapping_criterion_skipped_with_explanation() -> None:
    result = evaluate_invalidations(
        invalidation_criteria=["not a dict"],  # type: ignore[list-item]
        current_precursors=None,
        current_market_p=None,
    )
    assert len(result.evaluations) == 1
    assert result.evaluations[0].status == "cannot_evaluate"


def test_evaluation_to_dict_round_trip() -> None:
    criterion = {
        "type": "market_move",
        "direction": "market_p_rises_to",
        "threshold": 0.50,
    }
    result = evaluate_invalidations(
        invalidation_criteria=[criterion],
        current_precursors=None,
        current_market_p=0.55,
    )
    payload = evaluation_to_dict(result.evaluations[0])
    assert payload["status"] == "triggered"
    assert payload["current_value"] == 0.55
    assert payload["criterion"]["direction"] == "market_p_rises_to"


def test_mixed_criteria_aggregate_count() -> None:
    criteria = [
        {
            "type": "precursor_shift",
            "variable_id": "v1",
            "threshold": 5.0,
            "direction": "high_signals_event",
            "description": "Variable v1 drops back below 5.0.",
        },
        {
            "type": "market_move",
            "direction": "market_p_falls_to",
            "threshold": 0.20,
        },
        {"type": "general_caveat", "description": "Watch for surprises."},
    ]
    current_pre = [{"variable_id": "v1", "current_value": 3.0}]
    result = evaluate_invalidations(
        invalidation_criteria=criteria,
        current_precursors=current_pre,
        current_market_p=0.18,
    )
    assert result.triggered_count == 2
    statuses = [ev.status for ev in result.evaluations]
    assert statuses == ["triggered", "triggered", "cannot_evaluate"]
