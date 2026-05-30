"""T-MON-022 — alert tier ranking tests."""

from __future__ import annotations

from razor_rooster.monitor.engines.alert_ranker import (
    TIER_PRIORITY,
    compute_alert_tiers,
)
from razor_rooster.monitor.models import PrecursorSnapshot, ShiftResult


def _none_shift() -> ShiftResult:
    return ShiftResult(value=0.0, band="none")


def _minor_shift() -> ShiftResult:
    return ShiftResult(value=0.02, band="minor")


def _material_shift() -> ShiftResult:
    return ShiftResult(value=0.07, band="material")


def _major_shift() -> ShiftResult:
    return ShiftResult(value=0.20, band="major")


def _precursor(*, threshold_crossed: bool) -> PrecursorSnapshot:
    return PrecursorSnapshot(
        variable_id="v1",
        title="Var",
        threshold=5.0,
        direction="high_signals_event",
        analysis_value=4.0,
        current_value=6.0 if threshold_crossed else 4.5,
        analysis_fired=False,
        current_fired=threshold_crossed,
    )


def test_priority_ordering_constant() -> None:
    assert TIER_PRIORITY == (
        "resolution",
        "invalidation_triggered",
        "material_shift",
        "precursor_shift",
        "time_decay",
    )


def test_no_alerts_when_unresolved_and_quiet() -> None:
    primary, applicable = compute_alert_tiers(
        resolution_status="unresolved",
        invalidation_triggered_count=0,
        model_shift=_none_shift(),
        market_shift=_none_shift(),
        precursor_snapshot=(),
        time_decay_alert=False,
    )
    assert primary is None
    assert applicable == ()


def test_resolution_takes_top_priority() -> None:
    primary, applicable = compute_alert_tiers(
        resolution_status="resolved_yes",
        invalidation_triggered_count=2,
        model_shift=_major_shift(),
        market_shift=_major_shift(),
        precursor_snapshot=(_precursor(threshold_crossed=True),),
        time_decay_alert=True,
    )
    assert primary == "resolution"
    # All five tiers apply, in priority order.
    assert applicable == TIER_PRIORITY


def test_invalidation_outranks_shift() -> None:
    primary, applicable = compute_alert_tiers(
        resolution_status="unresolved",
        invalidation_triggered_count=1,
        model_shift=_material_shift(),
        market_shift=_minor_shift(),
        precursor_snapshot=(),
        time_decay_alert=False,
    )
    assert primary == "invalidation_triggered"
    assert applicable == ("invalidation_triggered", "material_shift")


def test_material_shift_in_either_dimension() -> None:
    primary_model, _ = compute_alert_tiers(
        resolution_status="unresolved",
        invalidation_triggered_count=0,
        model_shift=_material_shift(),
        market_shift=_minor_shift(),
        precursor_snapshot=(),
        time_decay_alert=False,
    )
    assert primary_model == "material_shift"

    primary_market, _ = compute_alert_tiers(
        resolution_status="unresolved",
        invalidation_triggered_count=0,
        model_shift=_minor_shift(),
        market_shift=_major_shift(),
        precursor_snapshot=(),
        time_decay_alert=False,
    )
    assert primary_market == "material_shift"


def test_minor_shifts_alone_do_not_alert() -> None:
    primary, applicable = compute_alert_tiers(
        resolution_status="unresolved",
        invalidation_triggered_count=0,
        model_shift=_minor_shift(),
        market_shift=_minor_shift(),
        precursor_snapshot=(),
        time_decay_alert=False,
    )
    assert primary is None
    assert applicable == ()


def test_precursor_shift_alone() -> None:
    primary, applicable = compute_alert_tiers(
        resolution_status="unresolved",
        invalidation_triggered_count=0,
        model_shift=_minor_shift(),
        market_shift=_minor_shift(),
        precursor_snapshot=(_precursor(threshold_crossed=True),),
        time_decay_alert=False,
    )
    assert primary == "precursor_shift"
    assert applicable == ("precursor_shift",)


def test_precursor_without_crossing_does_not_alert() -> None:
    primary, applicable = compute_alert_tiers(
        resolution_status="unresolved",
        invalidation_triggered_count=0,
        model_shift=_none_shift(),
        market_shift=_none_shift(),
        precursor_snapshot=(_precursor(threshold_crossed=False),),
        time_decay_alert=False,
    )
    assert primary is None
    assert applicable == ()


def test_time_decay_alone_is_lowest_priority() -> None:
    primary, applicable = compute_alert_tiers(
        resolution_status="unresolved",
        invalidation_triggered_count=0,
        model_shift=_minor_shift(),
        market_shift=_minor_shift(),
        precursor_snapshot=(),
        time_decay_alert=True,
    )
    assert primary == "time_decay"
    assert applicable == ("time_decay",)


def test_unresolved_with_other_alerts_still_aligns_to_priority() -> None:
    """A non-resolution case with multiple alerts: shift > precursor > time."""
    primary, applicable = compute_alert_tiers(
        resolution_status="unresolved",
        invalidation_triggered_count=0,
        model_shift=_material_shift(),
        market_shift=_minor_shift(),
        precursor_snapshot=(_precursor(threshold_crossed=True),),
        time_decay_alert=True,
    )
    assert primary == "material_shift"
    assert applicable == ("material_shift", "precursor_shift", "time_decay")
