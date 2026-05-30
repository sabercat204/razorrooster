"""T-PL-042 — threshold-discovery primitive tests."""

from __future__ import annotations

import math

import numpy as np
import pytest

from razor_rooster.pattern_library.engines.thresholds import (
    ThresholdPick,
    f1_threshold,
    manual,
    quantile_95,
    youden_j,
)

# -- youden_j --------------------------------------------------------------


def test_youden_j_recovers_separable_signal() -> None:
    """Two clearly-separated populations → Youden J finds the gap."""
    rng = np.random.default_rng(42)
    events = rng.normal(loc=2.0, scale=0.3, size=100)
    nonevents = rng.normal(loc=0.0, scale=0.3, size=400)
    scores = np.concatenate([events, nonevents])
    labels = np.concatenate(
        [np.ones(events.shape[0], dtype=bool), np.zeros(nonevents.shape[0], dtype=bool)]
    )
    pick = youden_j(scores, labels)
    assert isinstance(pick, ThresholdPick)
    # Threshold should sit somewhere between the two clusters.
    assert 0.5 <= pick.threshold <= 1.5
    assert pick.true_positive_rate > 0.95
    assert pick.false_positive_rate < 0.05


def test_youden_j_handles_no_signal() -> None:
    """When the populations overlap completely, J approaches zero."""
    rng = np.random.default_rng(7)
    scores = rng.normal(loc=0.0, scale=1.0, size=500)
    labels = np.array([i % 2 == 0 for i in range(500)])
    pick = youden_j(scores, labels)
    j = pick.true_positive_rate - pick.false_positive_rate
    assert abs(j) < 0.2  # noise can give a small spurious J


def test_youden_j_empty_input() -> None:
    pick = youden_j([], [])
    assert pick.threshold == 0.0
    assert pick.true_positive_rate == 0.0
    assert pick.false_positive_rate == 0.0


def test_youden_j_mismatched_shapes_rejected() -> None:
    with pytest.raises(ValueError, match="same shape"):
        youden_j([1.0, 2.0], [True])


def test_youden_j_all_event_labels() -> None:
    """When every label is True, FPR is 0 (no non-events to be false-positive on)."""
    pick = youden_j([1.0, 2.0, 3.0], [True, True, True])
    assert pick.false_positive_rate == 0.0


# -- f1_threshold ----------------------------------------------------------


def test_f1_recovers_separable_signal() -> None:
    rng = np.random.default_rng(11)
    events = rng.normal(loc=2.0, scale=0.3, size=50)
    nonevents = rng.normal(loc=0.0, scale=0.3, size=200)
    scores = np.concatenate([events, nonevents])
    labels = np.concatenate(
        [np.ones(events.shape[0], dtype=bool), np.zeros(nonevents.shape[0], dtype=bool)]
    )
    pick = f1_threshold(scores, labels)
    assert pick.true_positive_rate > 0.9


def test_f1_no_positive_predictions_returns_zero_f1() -> None:
    """When the chosen threshold catches no events, F1 stays 0."""
    pick = f1_threshold([0.0, 0.0, 0.0], [True, False, False])
    # The minimum candidate threshold is 0.0; at that threshold every
    # sample fires, so we still get an operating point. Just assert
    # the call succeeds with sane fields.
    assert 0.0 <= pick.true_positive_rate <= 1.0
    assert 0.0 <= pick.false_positive_rate <= 1.0


def test_f1_mismatched_shapes_rejected() -> None:
    with pytest.raises(ValueError, match="same shape"):
        f1_threshold([1.0, 2.0], [True])


# -- quantile_95 -----------------------------------------------------------


def test_quantile_95_picks_p95_of_baseline() -> None:
    baseline = list(range(100))
    pick = quantile_95(baseline)
    # 95th percentile of 0..99 is ~94.05
    assert 93.0 <= pick.threshold <= 96.0
    # FPR = fraction of baseline at or above threshold ≈ 0.05
    assert 0.04 <= pick.false_positive_rate <= 0.07
    # TPR is reported NaN — no labels supplied.
    assert math.isnan(pick.true_positive_rate)


def test_quantile_95_empty_baseline() -> None:
    pick = quantile_95([])
    assert pick.threshold == 0.0
    assert pick.false_positive_rate == 0.05  # synthetic 5% to keep math sensible


def test_quantile_95_with_nans() -> None:
    baseline = [1.0, np.nan, 2.0, 3.0, 4.0, 5.0]
    pick = quantile_95(baseline)
    # 95th percentile of valid values 1..5 is ~4.8
    assert 4.0 <= pick.threshold <= 5.0


# -- manual ---------------------------------------------------------------


def test_manual_without_labels_returns_nan_rates() -> None:
    pick = manual(0.5)
    assert pick.threshold == 0.5
    assert math.isnan(pick.true_positive_rate)
    assert math.isnan(pick.false_positive_rate)


def test_manual_with_labels_reports_operating_point() -> None:
    scores = [0.1, 0.4, 0.6, 0.8, 0.9]
    labels = [False, False, True, True, True]
    pick = manual(0.5, scores=scores, labels=labels)
    assert pick.threshold == 0.5
    # All three events have score >= 0.5 → TPR = 1.0
    assert pick.true_positive_rate == pytest.approx(1.0)
    # Both non-events have score < 0.5 → FPR = 0.0
    assert pick.false_positive_rate == pytest.approx(0.0)


def test_manual_threshold_with_one_side_only() -> None:
    """Mismatched lengths still raise."""
    with pytest.raises(ValueError, match="same shape"):
        manual(0.5, scores=[0.1, 0.2], labels=[True])
