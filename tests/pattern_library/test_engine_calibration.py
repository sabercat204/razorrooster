"""T-PL-046 — calibration engine tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.pattern_library.engines.calibration import (
    DEFAULT_RELIABILITY_BINS,
    MIN_OCCURRENCES_FOR_CALIBRATION,
    compute_calibration,
)
from razor_rooster.pattern_library.models.outcomes import OutcomeRecord
from razor_rooster.pattern_library.models.signature import SignatureResult


def _make_outcome(idx: int) -> OutcomeRecord:
    return OutcomeRecord(
        class_id="test_class",
        occurrence_id=f"occ-{idx}",
        occurrence_ts=datetime(2020 + idx % 5, 6, 1, tzinfo=UTC),
    )


def _make_signature(
    *,
    hit_rate: float,
    fp_rate: float,
    variable_id: str = "v",
) -> SignatureResult:
    return SignatureResult(
        class_id="test_class",
        variable_id=variable_id,
        library_version=1,
        definition_version=1,
        threshold_method="youden_j",
        threshold_value=0.5,
        direction="high_signals_event",
        lead_time_window_days=180,
        pre_event_mean=None,
        pre_event_p25=None,
        pre_event_p50=None,
        pre_event_p75=None,
        baseline_mean=None,
        baseline_p25=None,
        baseline_p50=None,
        baseline_p75=None,
        hit_rate=hit_rate,
        false_positive_rate=fp_rate,
        sample_size_events=10,
        sample_size_baseline=100,
        confidence_score=0.7,
        computed_at=datetime(2026, 5, 14, tzinfo=UTC),
    )


# -- happy paths ----------------------------------------------------------


def test_well_calibrated_signature_produces_low_brier(tmp_path: Path) -> None:
    """A signature with hit_rate=1.0 / fp_rate=0.0 on a clean event/baseline
    split should score Brier=0 (perfectly calibrated by construction).
    """
    outcomes = [_make_outcome(i) for i in range(15)]
    sig = _make_signature(hit_rate=1.0, fp_rate=0.0)
    result = compute_calibration(
        class_id="test_class",
        library_version=1,
        definition_version=1,
        outcomes=outcomes,
        signatures=[sig],
        baseline_size=100,
        trace_dir=tmp_path,
    )
    assert result.method == "leave_one_out_signature"
    assert result.brier_score is not None
    assert result.brier_score == pytest.approx(0.0, abs=1e-6)


def test_poorly_calibrated_signature_produces_high_brier(tmp_path: Path) -> None:
    """A signature with hit_rate=0.0 / fp_rate=1.0 (predictions inverted from
    truth) should score Brier=1.0.
    """
    outcomes = [_make_outcome(i) for i in range(15)]
    sig = _make_signature(hit_rate=0.0, fp_rate=1.0)
    result = compute_calibration(
        class_id="test_class",
        library_version=1,
        definition_version=1,
        outcomes=outcomes,
        signatures=[sig],
        baseline_size=100,
        trace_dir=tmp_path,
    )
    assert result.brier_score is not None
    assert result.brier_score == pytest.approx(1.0, abs=1e-6)


def test_moderate_calibration_is_in_between(tmp_path: Path) -> None:
    outcomes = [_make_outcome(i) for i in range(20)]
    sig = _make_signature(hit_rate=0.5, fp_rate=0.5)
    result = compute_calibration(
        class_id="test_class",
        library_version=1,
        definition_version=1,
        outcomes=outcomes,
        signatures=[sig],
        baseline_size=100,
        trace_dir=tmp_path,
    )
    assert result.brier_score is not None
    assert 0.0 < result.brier_score < 1.0


def test_calibration_writes_trace_file(tmp_path: Path) -> None:
    outcomes = [_make_outcome(i) for i in range(15)]
    sig = _make_signature(hit_rate=0.7, fp_rate=0.1)
    result = compute_calibration(
        class_id="test_class",
        library_version=1,
        definition_version=1,
        outcomes=outcomes,
        signatures=[sig],
        baseline_size=50,
        trace_dir=tmp_path,
    )
    trace_path = Path(result.prediction_trace_path)
    assert trace_path.exists()
    payload = json.loads(trace_path.read_text())
    assert payload["class_id"] == "test_class"
    assert payload["method"] == "leave_one_out_signature"
    assert payload["n_events"] == 15
    # Trace contains 15 events + 50 baselines = 65 rows.
    assert len(payload["traces"]) == 65
    # Event traces should have predicted_p == hit_rate.
    event_traces = [t for t in payload["traces"] if t["is_event"]]
    assert all(t["predicted_p"] == pytest.approx(0.7) for t in event_traces)


def test_calibration_reliability_bins_present(tmp_path: Path) -> None:
    outcomes = [_make_outcome(i) for i in range(20)]
    # Use moderate predictions so multiple bins might fire.
    sig_hi = _make_signature(hit_rate=0.8, fp_rate=0.2, variable_id="hi")
    result = compute_calibration(
        class_id="test_class",
        library_version=1,
        definition_version=1,
        outcomes=outcomes,
        signatures=[sig_hi],
        baseline_size=100,
        trace_dir=tmp_path,
    )
    # At least one bin per used probability.
    assert len(result.reliability_bins) >= 1
    for bin_ in result.reliability_bins:
        assert 0 <= bin_.bin_low <= 1.0
        assert 0 <= bin_.bin_high <= 1.0
        assert bin_.count > 0


# -- insufficient data path -----------------------------------------------


def test_insufficient_data_returns_sentinel(tmp_path: Path) -> None:
    outcomes = [_make_outcome(i) for i in range(5)]  # < 10 → insufficient
    sig = _make_signature(hit_rate=0.7, fp_rate=0.1)
    result = compute_calibration(
        class_id="rare",
        library_version=1,
        definition_version=1,
        outcomes=outcomes,
        signatures=[sig],
        baseline_size=20,
        trace_dir=tmp_path,
    )
    assert result.method == "insufficient_data"
    assert result.brier_score is None
    assert result.reliability_bins == ()
    assert (
        result.notes is not None and "insufficient" in result.notes.lower()
    ) or "occurrences" in result.notes.lower()


def test_insufficient_data_still_writes_trace_file(tmp_path: Path) -> None:
    """Even when calibration is skipped, a trace file is created so the path
    is consistent.
    """
    outcomes = [_make_outcome(i) for i in range(3)]
    sig = _make_signature(hit_rate=0.7, fp_rate=0.1)
    result = compute_calibration(
        class_id="rare",
        library_version=1,
        definition_version=1,
        outcomes=outcomes,
        signatures=[sig],
        baseline_size=20,
        trace_dir=tmp_path,
    )
    trace_path = Path(result.prediction_trace_path)
    assert trace_path.exists()
    payload = json.loads(trace_path.read_text())
    assert payload["method"] == "insufficient_data"
    assert payload["traces"] == []


# -- constants -----------------------------------------------------------


def test_minimum_occurrences_constant() -> None:
    assert MIN_OCCURRENCES_FOR_CALIBRATION == 10


def test_default_reliability_bins_constant() -> None:
    assert DEFAULT_RELIABILITY_BINS == 10


# -- edge cases ----------------------------------------------------------


def test_calibration_with_no_signatures_uses_zero_predictions(tmp_path: Path) -> None:
    """Empty signature list → zero predictions → Brier == event-fraction²."""
    outcomes = [_make_outcome(i) for i in range(12)]
    result = compute_calibration(
        class_id="test_class",
        library_version=1,
        definition_version=1,
        outcomes=outcomes,
        signatures=[],
        baseline_size=88,
        trace_dir=tmp_path,
    )
    # n_events=12, baseline_size=88, total=100. Predicted=0 for all.
    # Brier = (12 * (0-1)^2 + 88 * (0-0)^2) / 100 = 12/100 = 0.12
    assert result.brier_score is not None
    assert result.brier_score == pytest.approx(0.12, abs=1e-6)


def test_calibration_with_zero_baseline(tmp_path: Path) -> None:
    """baseline_size=0 → only event predictions go into Brier."""
    outcomes = [_make_outcome(i) for i in range(15)]
    sig = _make_signature(hit_rate=1.0, fp_rate=0.0)
    result = compute_calibration(
        class_id="test_class",
        library_version=1,
        definition_version=1,
        outcomes=outcomes,
        signatures=[sig],
        baseline_size=0,
        trace_dir=tmp_path,
    )
    # 15 events with prediction 1.0 — Brier = 0.
    assert result.brier_score is not None
    assert result.brier_score == pytest.approx(0.0, abs=1e-6)
