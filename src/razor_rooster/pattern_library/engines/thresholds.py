"""Threshold-discovery primitives for the signature engine (T-PL-042).

Per OQ-PL-002 and design §3.5, four threshold-finding methods. Each
returns a typed :class:`ThresholdPick` carrying the threshold plus its
true-positive and false-positive rate at that point. Callers in
:mod:`engines.signatures` pick which method to invoke based on the
``PrecursorVariable.threshold_method`` setting.

All four functions take score arrays already projected so that
"high score signals event" — callers handle the
``low_signals_event`` direction by negating before calling these
helpers and negating the returned threshold afterward.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Union

import numpy as np

# Score / label inputs accept any iterable of numbers / bools. We use
# typing.Union here (not the | operator) to keep the alias evaluable at
# module-import time even when ``from __future__ import annotations``
# isn't in scope at the alias-definition point.
_Scores = Union[Sequence[float], "np.ndarray[Any, Any]"]
_Labels = Union[Sequence[bool], "np.ndarray[Any, Any]"]


@dataclass(frozen=True, slots=True)
class ThresholdPick:
    """The threshold a method chose plus its operating point.

    ``threshold`` is the value above which the predictor fires.
    ``true_positive_rate`` is the fraction of event-labeled samples at
    or above the threshold; ``false_positive_rate`` is the fraction of
    non-event-labeled samples at or above it.
    """

    threshold: float
    true_positive_rate: float
    false_positive_rate: float


def youden_j(
    scores: _Scores,
    labels: _Labels,
) -> ThresholdPick:
    """Pick the threshold maximizing Youden's J = TPR - FPR (OQ-PL-002 default).

    The candidate threshold set is each unique score value in
    ``scores``. Ties at the same J are resolved by picking the
    threshold with the highest TPR (so the chosen point is the
    "stricter" of two equally-good operating points — fewer
    false positives, same hits).
    """
    score_array, label_array = _validate(scores, labels)
    if score_array.size == 0:
        return ThresholdPick(threshold=0.0, true_positive_rate=0.0, false_positive_rate=0.0)

    candidates = np.unique(score_array)
    best_j = -np.inf
    best_pick: ThresholdPick | None = None
    for threshold in candidates:
        tpr, fpr = _operating_point(score_array, label_array, threshold)
        j = tpr - fpr
        if j > best_j or (
            j == best_j and best_pick is not None and tpr > best_pick.true_positive_rate
        ):
            best_j = j
            best_pick = ThresholdPick(
                threshold=float(threshold),
                true_positive_rate=tpr,
                false_positive_rate=fpr,
            )
    assert best_pick is not None  # candidates non-empty when score_array non-empty
    return best_pick


def f1_threshold(
    scores: _Scores,
    labels: _Labels,
) -> ThresholdPick:
    """Pick the threshold maximizing F1 against the labels.

    Useful when false-positives carry asymmetric cost relative to
    Youden's J. Same tie-break rule as ``youden_j``: higher TPR wins.
    """
    score_array, label_array = _validate(scores, labels)
    if score_array.size == 0:
        return ThresholdPick(threshold=0.0, true_positive_rate=0.0, false_positive_rate=0.0)

    candidates = np.unique(score_array)
    best_f1 = -np.inf
    best_pick: ThresholdPick | None = None
    for threshold in candidates:
        tpr, fpr = _operating_point(score_array, label_array, threshold)
        precision, recall = _precision_recall(score_array, label_array, threshold)
        f1 = 0.0 if precision + recall == 0 else 2.0 * precision * recall / (precision + recall)
        if f1 > best_f1 or (
            f1 == best_f1 and best_pick is not None and tpr > best_pick.true_positive_rate
        ):
            best_f1 = f1
            best_pick = ThresholdPick(
                threshold=float(threshold),
                true_positive_rate=tpr,
                false_positive_rate=fpr,
            )
    assert best_pick is not None
    return best_pick


def quantile_95(baseline_scores: _Scores) -> ThresholdPick:
    """Pick the 95th percentile of the baseline score distribution.

    The TPR / FPR pair is computed against the baseline only — there
    are no labels — so TPR is reported as ``nan`` to make the
    "labels-not-considered" semantics explicit. Callers that want a
    label-aware operating point should use ``youden_j`` or
    ``f1_threshold`` instead.
    """
    arr = np.asarray(baseline_scores, dtype=float)
    if arr.size == 0:
        return ThresholdPick(threshold=0.0, true_positive_rate=0.0, false_positive_rate=0.05)
    threshold = float(np.nanpercentile(arr, 95))
    valid = arr[~np.isnan(arr)]
    fpr = 0.05 if valid.size == 0 else float((valid >= threshold).sum() / valid.size)
    return ThresholdPick(
        threshold=threshold,
        true_positive_rate=float("nan"),
        false_positive_rate=fpr,
    )


def manual(
    threshold: float,
    *,
    scores: _Scores | None = None,
    labels: _Labels | None = None,
) -> ThresholdPick:
    """Use the operator-supplied threshold value.

    When ``scores`` and ``labels`` are also supplied, the TPR / FPR pair
    at that threshold is reported. When omitted, TPR / FPR are reported
    as ``nan``.
    """
    if scores is None or labels is None:
        return ThresholdPick(
            threshold=float(threshold),
            true_positive_rate=float("nan"),
            false_positive_rate=float("nan"),
        )
    score_array, label_array = _validate(scores, labels)
    tpr, fpr = _operating_point(score_array, label_array, threshold)
    return ThresholdPick(
        threshold=float(threshold),
        true_positive_rate=tpr,
        false_positive_rate=fpr,
    )


# -- internals --------------------------------------------------------------


def _validate(
    scores: _Scores,
    labels: _Labels,
) -> tuple[np.ndarray, np.ndarray]:
    score_array = np.asarray(scores, dtype=float)
    label_array = np.asarray(labels, dtype=bool)
    if score_array.shape != label_array.shape:
        raise ValueError(
            f"scores and labels must have the same shape, got "
            f"{score_array.shape} vs {label_array.shape}"
        )
    return score_array, label_array


def _operating_point(
    scores: np.ndarray,
    labels: np.ndarray,
    threshold: float,
) -> tuple[float, float]:
    """Compute TPR / FPR at the given threshold.

    A score >= threshold counts as a positive prediction. Edge case:
    if there are no events (or no non-events), the corresponding rate
    is reported as 0 (not NaN) so downstream comparisons stay
    well-defined.
    """
    fired = scores >= threshold
    n_events = int(labels.sum())
    n_nonevents = int((~labels).sum())
    tp = int((fired & labels).sum())
    fp = int((fired & ~labels).sum())
    tpr = tp / n_events if n_events > 0 else 0.0
    fpr = fp / n_nonevents if n_nonevents > 0 else 0.0
    return tpr, fpr


def _precision_recall(
    scores: np.ndarray,
    labels: np.ndarray,
    threshold: float,
) -> tuple[float, float]:
    fired = scores >= threshold
    tp = int((fired & labels).sum())
    fp = int((fired & ~labels).sum())
    fn = int((~fired & labels).sum())
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return precision, recall
