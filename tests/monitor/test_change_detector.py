"""T-MON-020 — change-detector tests."""

from __future__ import annotations

from razor_rooster.monitor.config.loader import ShiftBandConfig
from razor_rooster.monitor.engines.change_detector import (
    classify_band,
    compute_market_shift,
    compute_model_shift,
    precursor_snapshot_to_dict,
    snapshot_precursors,
)


def _bands() -> ShiftBandConfig:
    return ShiftBandConfig(minor_threshold=0.01, material_threshold=0.05, major_threshold=0.15)


def test_classify_band_at_each_edge() -> None:
    bands = _bands()
    assert classify_band(0.0, bands) == "none"
    assert classify_band(0.009, bands) == "none"
    assert classify_band(0.01, bands) == "minor"
    assert classify_band(0.04, bands) == "minor"
    assert classify_band(0.05, bands) == "material"
    assert classify_band(0.10, bands) == "material"
    assert classify_band(0.15, bands) == "major"
    assert classify_band(0.30, bands) == "major"


def test_classify_band_uses_absolute_value() -> None:
    bands = _bands()
    assert classify_band(-0.20, bands) == "major"
    assert classify_band(-0.07, bands) == "material"
    assert classify_band(-0.02, bands) == "minor"


def test_compute_model_shift_with_none_current_returns_unobservable() -> None:
    result = compute_model_shift(analysis_model_p=0.30, current_model_p=None, bands=_bands())
    assert result.value is None
    assert result.band is None


def test_compute_model_shift_basic() -> None:
    result = compute_model_shift(analysis_model_p=0.30, current_model_p=0.40, bands=_bands())
    assert result.value is not None
    assert abs(result.value - 0.10) < 1e-9
    assert result.band == "material"


def test_compute_model_shift_negative() -> None:
    result = compute_model_shift(analysis_model_p=0.50, current_model_p=0.30, bands=_bands())
    assert result.value is not None
    assert abs(result.value - (-0.20)) < 1e-9
    assert result.band == "major"


def test_compute_market_shift_with_missing_either_side() -> None:
    bands = _bands()
    a = compute_market_shift(analysis_market_p=None, current_market_p=0.30, bands=bands)
    b = compute_market_shift(analysis_market_p=0.30, current_market_p=None, bands=bands)
    assert a.value is None and a.band is None
    assert b.value is None and b.band is None


def test_compute_market_shift_basic() -> None:
    result = compute_market_shift(analysis_market_p=0.10, current_market_p=0.18, bands=_bands())
    assert result.value is not None
    assert abs(result.value - 0.08) < 1e-9
    assert result.band == "material"


def test_snapshot_precursors_paired() -> None:
    analysis = [
        {
            "variable_id": "v1",
            "title": "Variable 1",
            "threshold": 5.0,
            "direction": "high_signals_event",
            "current_value": 4.0,
            "fired": False,
        }
    ]
    current = [
        {
            "variable_id": "v1",
            "current_value": 6.0,
            "fired": True,
        }
    ]
    snapshots = snapshot_precursors(analysis_precursors=analysis, current_precursors=current)
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.variable_id == "v1"
    assert snap.analysis_value == 4.0
    assert snap.current_value == 6.0
    assert snap.analysis_fired is False
    assert snap.current_fired is True
    assert snap.threshold_crossed is True


def test_snapshot_precursors_unpaired_keeps_analysis_fired_state() -> None:
    """When current scan lacks a precursor, threshold_crossed should be False."""
    analysis = [
        {
            "variable_id": "v1",
            "title": "Variable 1",
            "threshold": 5.0,
            "direction": "high_signals_event",
            "current_value": 6.0,
            "fired": True,
        }
    ]
    snapshots = snapshot_precursors(analysis_precursors=analysis, current_precursors=[])
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.current_value is None
    assert snap.current_fired is True  # mirrors analysis state when absent
    assert snap.threshold_crossed is False


def test_snapshot_precursors_empty_returns_empty() -> None:
    assert snapshot_precursors(analysis_precursors=None, current_precursors=None) == []
    assert snapshot_precursors(analysis_precursors=[], current_precursors=[]) == []


def test_snapshot_precursors_threshold_crossed_when_state_changes() -> None:
    """A fired -> not-fired transition is also a crossing."""
    analysis = [
        {
            "variable_id": "v1",
            "title": "V",
            "threshold": 5.0,
            "direction": "high_signals_event",
            "current_value": 6.0,
            "fired": True,
        }
    ]
    current = [{"variable_id": "v1", "current_value": 4.0, "fired": False}]
    snapshots = snapshot_precursors(analysis_precursors=analysis, current_precursors=current)
    assert len(snapshots) == 1
    assert snapshots[0].threshold_crossed is True


def test_precursor_snapshot_to_dict_includes_threshold_crossed_flag() -> None:
    analysis = [
        {
            "variable_id": "v1",
            "title": "Var",
            "threshold": 5.0,
            "direction": "high_signals_event",
            "current_value": 4.0,
            "fired": False,
        }
    ]
    current = [{"variable_id": "v1", "current_value": 6.0, "fired": True}]
    snap = snapshot_precursors(analysis_precursors=analysis, current_precursors=current)[0]
    payload = precursor_snapshot_to_dict(snap)
    assert payload["variable_id"] == "v1"
    assert payload["threshold_crossed"] is True
    assert payload["analysis_value"] == 4.0
    assert payload["current_value"] == 6.0


def test_snapshot_precursors_skips_non_mapping_entries() -> None:
    """Defensive: malformed entries should be silently skipped."""
    snapshots = snapshot_precursors(
        analysis_precursors=["not a mapping", 42],  # type: ignore[list-item]
        current_precursors=None,
    )
    assert snapshots == []
