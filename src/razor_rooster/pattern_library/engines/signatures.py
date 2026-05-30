"""Precursor signature engine (T-PL-043, T-PL-044; design §3.5).

Computes a :class:`SignatureResult` for each precursor variable in an
event class, then a multi-variable :class:`CombinedScore` for the
"current state" via geometric-mean-with-co-occurrence-correction.

Pipeline per variable (design §3.5):

1. Pull the variable's time series via ``precursor.query(conn, start, end)``.
2. For each event occurrence in the class, extract the variable's
   value within the lead-time window (``mean over the lead window``).
3. Build the baseline distribution by sampling timestamps per
   ``cls.baseline_strategy``, excluding the refractory zone around
   each occurrence (OQ-PL-003).
4. Compute pre-event vs. baseline summary statistics.
5. Discover the threshold per ``precursor.threshold_method`` (T-PL-042).
6. Compute hit rate / false-positive rate at that threshold.
7. Compute confidence score: sample-size weight * distributional
   separation (Cohen's d) * threshold bootstrap stability.

The combined-variable step (T-PL-044) takes the per-variable hit rates
and a "currently past threshold?" indicator per variable, and produces
a calibrated joint score using a co-occurrence lookup table built
during signature computation.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import duckdb
import numpy as np
import pandas as pd

from razor_rooster.pattern_library.engines.thresholds import (
    ThresholdPick,
    f1_threshold,
    manual,
    quantile_95,
    youden_j,
)
from razor_rooster.pattern_library.models.event_class import (
    BaselineStrategy,
    PrecursorVariable,
    ThresholdMethod,
)
from razor_rooster.pattern_library.models.outcomes import OutcomeRecord
from razor_rooster.pattern_library.models.signature import SignatureResult

if TYPE_CHECKING:
    from razor_rooster.pattern_library.models.event_class import EventClass


logger = logging.getLogger(__name__)


# Default bootstrap sample count for threshold-stability scoring.
DEFAULT_BOOTSTRAP_ITERATIONS: int = 100

# Confidence sub-score that fires when n_events is below this floor.
LOW_CONFIDENCE_EVENT_THRESHOLD: int = 5


@dataclass(frozen=True, slots=True)
class CombinedScore:
    """Output of :func:`combine_variables` (T-PL-044).

    ``score`` in [0, 1] is the calibrated joint hit-rate estimate.
    ``method`` records whether the joint estimate came from the
    co-occurrence lookup or the geometric-mean fallback.
    ``contributing_variable_ids`` is the subset of variables currently
    past their thresholds.
    """

    score: float
    method: str  # 'co_occurrence' | 'geometric_mean' | 'no_signal'
    contributing_variable_ids: tuple[str, ...]
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _PrecursorSamples:
    """Internal: pre-event and baseline scalar arrays plus baseline timestamps."""

    pre_event_values: np.ndarray
    baseline_values: np.ndarray
    baseline_timestamps: tuple[datetime, ...]


def compute_signature(
    conn: duckdb.DuckDBPyConnection,
    cls: EventClass,
    *,
    outcomes: Sequence[OutcomeRecord],
    library_version: int,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    rng: np.random.Generator | None = None,
    now: datetime | None = None,
) -> tuple[tuple[SignatureResult, ...], dict[str, dict[str, np.ndarray]]]:
    """Compute one signature per precursor variable in ``cls``.

    Returns the per-variable signatures and a dict of per-variable
    pre-event / baseline value arrays (used by
    :func:`build_co_occurrence_table` for T-PL-044).

    ``outcomes`` is the persisted occurrence list (typically loaded via
    ``query_outcomes`` after :func:`engines.refresh` writes them).
    Passing them in explicitly avoids re-running the class's
    occurrence_query for each precursor.
    """
    started = now or datetime.now(tz=UTC)
    rng = rng or np.random.default_rng(seed=_class_seed(cls.class_id))

    if not outcomes:
        return (), {}

    occurrence_ts = sorted(rec.occurrence_ts for rec in outcomes)
    window_start, window_end = _compute_signature_window(cls, occurrence_ts, now=started)

    baseline_timestamps = _sample_baseline_timestamps(
        cls=cls,
        occurrence_ts=occurrence_ts,
        window_start=window_start,
        window_end=window_end,
        rng=rng,
    )

    signatures: list[SignatureResult] = []
    samples_by_variable: dict[str, dict[str, np.ndarray]] = {}

    for precursor in cls.precursors:
        try:
            samples = _extract_samples(
                conn,
                precursor=precursor,
                occurrence_ts=tuple(occurrence_ts),
                baseline_ts=baseline_timestamps,
                window_start=window_start,
                window_end=window_end,
            )
        except Exception as exc:
            logger.exception(
                "signature engine: precursor %s.%s extraction failed",
                cls.class_id,
                precursor.variable_id,
            )
            signatures.append(
                _empty_signature(
                    cls=cls,
                    precursor=precursor,
                    library_version=library_version,
                    when=started,
                    note=f"extraction_failed: {type(exc).__name__}: {exc}",
                )
            )
            continue

        pick = _pick_threshold(precursor, samples)
        sig = _build_signature_result(
            cls=cls,
            precursor=precursor,
            samples=samples,
            pick=pick,
            library_version=library_version,
            bootstrap_iterations=bootstrap_iterations,
            rng=rng,
            when=started,
        )
        signatures.append(sig)
        samples_by_variable[precursor.variable_id] = {
            "pre_event": samples.pre_event_values,
            "baseline": samples.baseline_values,
        }

    return tuple(signatures), samples_by_variable


def build_co_occurrence_table(
    *,
    samples_by_variable: dict[str, dict[str, np.ndarray]],
    signatures: Sequence[SignatureResult],
) -> dict[frozenset[str], float]:
    """Build the co-occurrence lookup used by :func:`combine_variables`.

    For each historical event index, identify the subset of variables
    whose pre-event value crossed the variable's discovered threshold
    (in the variable's expected direction). The returned dict maps
    ``frozenset(subset)`` to the empirical "events where exactly this
    subset fired" hit rate.

    Subsets that never fired together get no entry; the caller falls
    back to the geometric-mean estimate for those.
    """
    if not signatures:
        return {}

    sig_by_id = {s.variable_id: s for s in signatures}
    valid_var_ids = [
        s.variable_id
        for s in signatures
        if s.variable_id in samples_by_variable and s.threshold_value is not None
    ]
    if not valid_var_ids:
        return {}

    # Each event index has the same length of pre_event_values per variable.
    n_events = next(len(samples_by_variable[v]["pre_event"]) for v in valid_var_ids)
    if n_events == 0:
        return {}

    fired_per_event: list[frozenset[str]] = []
    for i in range(n_events):
        contributing: list[str] = []
        for v in valid_var_ids:
            sig = sig_by_id[v]
            value = float(samples_by_variable[v]["pre_event"][i])
            threshold = float(sig.threshold_value or 0.0)
            if math.isnan(value):
                continue
            if _crosses_threshold(value, threshold, direction=sig.direction):
                contributing.append(v)
        fired_per_event.append(frozenset(contributing))

    # Empirical co-occurrence lookup: for each non-empty subset that
    # appears at least once in fired_per_event, the lookup value is
    # the fraction of events where that exact subset fired.
    table: dict[frozenset[str], float] = {}
    for fired in fired_per_event:
        if not fired:
            continue
        if fired in table:
            continue
        count = sum(1 for s in fired_per_event if s == fired)
        table[fired] = count / n_events
    return table


def combine_variables(
    *,
    signatures: Sequence[SignatureResult],
    current_values: dict[str, float],
    co_occurrence: dict[frozenset[str], float],
) -> CombinedScore:
    """Combine per-variable hit rates into a calibrated joint score (T-PL-044).

    Steps:

    1. Identify which variables are currently past threshold in the
       expected direction.
    2. If the firing subset has a co-occurrence entry, use that empirical
       joint hit rate.
    3. Otherwise, use the geometric mean of per-variable hit rates as
       the fallback.
    4. If no variables are firing, return a zero-score "no_signal"
       result.
    """
    contributing: list[str] = []
    sig_by_id = {s.variable_id: s for s in signatures}
    for s in signatures:
        value = current_values.get(s.variable_id)
        if value is None or math.isnan(value):
            continue
        if s.threshold_value is None:
            continue
        if _crosses_threshold(value, float(s.threshold_value), direction=s.direction):
            contributing.append(s.variable_id)

    if not contributing:
        return CombinedScore(
            score=0.0,
            method="no_signal",
            contributing_variable_ids=(),
        )

    subset = frozenset(contributing)
    if subset in co_occurrence:
        return CombinedScore(
            score=float(co_occurrence[subset]),
            method="co_occurrence",
            contributing_variable_ids=tuple(sorted(contributing)),
        )

    # Geometric mean of per-variable hit rates, treating NaN/None as 0.
    hit_rates: list[float] = []
    for v in contributing:
        s = sig_by_id[v]
        rate = float(s.hit_rate) if s.hit_rate is not None else 0.0
        hit_rates.append(rate)
    if not hit_rates or all(r <= 0 for r in hit_rates):
        return CombinedScore(
            score=0.0,
            method="geometric_mean",
            contributing_variable_ids=tuple(sorted(contributing)),
            fallback_reason="all_hit_rates_zero",
        )
    geometric_mean = math.exp(sum(math.log(max(r, 1e-12)) for r in hit_rates) / len(hit_rates))
    return CombinedScore(
        score=geometric_mean,
        method="geometric_mean",
        contributing_variable_ids=tuple(sorted(contributing)),
        fallback_reason="not_in_co_occurrence_table",
    )


# -- internals --------------------------------------------------------------


def _crosses_threshold(value: float, threshold: float, *, direction: str) -> bool:
    if direction == "high_signals_event":
        return value >= threshold
    if direction == "low_signals_event":
        return value <= threshold
    raise ValueError(f"unknown direction {direction!r}")


def _class_seed(class_id: str) -> int:
    """Deterministic seed per class so refresh runs are reproducible."""
    return int.from_bytes(hashlib.sha256(class_id.encode("utf-8")).digest()[:4], "big")


def _compute_signature_window(
    cls: EventClass,
    occurrence_ts: Sequence[datetime],
    *,
    now: datetime,
) -> tuple[datetime, datetime]:
    """Window covering the longest plausible pre-event lead time.

    Bounded above by ``now`` and below by the earliest occurrence's
    pre-event window start. This is the time range for which the
    precursor query is invoked; baseline sampling stays inside it too.
    """
    if not occurrence_ts:
        return (now - cls.base_rate_window_default, now)
    max_lead = (
        max(p.lead_time_window for p in cls.precursors) if cls.precursors else timedelta(days=180)
    )
    earliest = min(occurrence_ts)
    start = earliest - max_lead - timedelta(days=30)  # small cushion
    return (start, now)


def _sample_baseline_timestamps(
    *,
    cls: EventClass,
    occurrence_ts: Sequence[datetime],
    window_start: datetime,
    window_end: datetime,
    rng: np.random.Generator,
) -> tuple[datetime, ...]:
    """Sample baseline timestamps per the class's strategy, excluding the refractory zone."""
    refractory = timedelta(days=30 * cls.refractory_months)

    refractory_zones: list[tuple[datetime, datetime]] = [
        (ts - refractory, ts + refractory) for ts in occurrence_ts
    ]
    target_count = max(cls.baseline_sample_size, 5 * len(occurrence_ts))

    span_seconds = (window_end - window_start).total_seconds()
    if span_seconds <= 0:
        return ()

    # Generate enough candidates that filtering doesn't starve us.
    oversample = max(target_count * 4, 100)
    if cls.baseline_strategy == BaselineStrategy.REGULAR_GRID:
        # Regular grid spans the window with target_count points.
        candidates = [
            window_start + timedelta(seconds=span_seconds * (i + 0.5) / target_count)
            for i in range(target_count)
        ]
    else:
        # uniform_random and stratified_random both use uniform random
        # sampling at v1; stratified is "uniform with refractory exclusion."
        # This is a v1 simplification — true stratification (e.g. by
        # season) lives in DEFER-PL-001 / DEFER-PL-002.
        offsets = rng.uniform(0.0, span_seconds, size=oversample)
        candidates = [window_start + timedelta(seconds=float(s)) for s in offsets]

    selected: list[datetime] = []
    for ts in candidates:
        if any(low <= ts <= high for low, high in refractory_zones):
            continue
        selected.append(ts)
        if len(selected) >= target_count:
            break
    return tuple(selected)


def _extract_samples(
    conn: duckdb.DuckDBPyConnection,
    *,
    precursor: PrecursorVariable,
    occurrence_ts: tuple[datetime, ...],
    baseline_ts: tuple[datetime, ...],
    window_start: datetime,
    window_end: datetime,
) -> _PrecursorSamples:
    """Pull the precursor series once, then summarize per pre-event window.

    The mean-over-lead-window summarization is the v1 default. Per
    OQ-PL-004, class authors can wrap their query with transforms
    (zscore, percentile_rank, etc.) before the engine sees the values;
    the engine just consumes the resulting series.
    """
    series = precursor.query(conn, window_start, window_end)
    if not isinstance(series, pd.Series):
        raise TypeError(
            f"precursor {precursor.variable_id!r} query must return a pandas Series, "
            f"got {type(series).__name__}"
        )
    if series.empty:
        empty = np.array([], dtype=float)
        return _PrecursorSamples(
            pre_event_values=empty,
            baseline_values=empty,
            baseline_timestamps=baseline_ts,
        )

    # Coerce index to UTC datetimes for windowed slicing.
    series = series.copy()
    series.index = pd.to_datetime(series.index, utc=True, errors="coerce")
    series = series.dropna()

    pre_event = np.array(
        [_window_mean(series, ts, precursor.lead_time_window) for ts in occurrence_ts],
        dtype=float,
    )
    baseline = np.array(
        [_window_mean(series, ts, precursor.lead_time_window) for ts in baseline_ts],
        dtype=float,
    )
    return _PrecursorSamples(
        pre_event_values=pre_event,
        baseline_values=baseline,
        baseline_timestamps=baseline_ts,
    )


def _window_mean(
    series: pd.Series,
    anchor: datetime,
    lead_window: timedelta,
) -> float:
    """Mean of the series over ``[anchor - lead_window, anchor)``.

    Returns NaN when the window contains no data.
    """
    start = pd.Timestamp(anchor - lead_window)
    end = pd.Timestamp(anchor)
    mask = (series.index >= start) & (series.index < end)
    sliced = series.loc[mask]
    if sliced.empty:
        return float("nan")
    return float(sliced.mean())


def _pick_threshold(
    precursor: PrecursorVariable,
    samples: _PrecursorSamples,
) -> ThresholdPick:
    """Discover the threshold per the precursor's chosen method."""
    # Drop NaNs from labels-and-scores together to keep arrays aligned.
    pre = samples.pre_event_values
    base = samples.baseline_values
    pre_clean = pre[~np.isnan(pre)]
    base_clean = base[~np.isnan(base)]

    # If the variable wants high values to signal events, score is the
    # raw value; if low values signal events, negate so "high" still
    # means "more event-like" for the threshold helpers.
    if precursor.direction == "high_signals_event":
        scores = np.concatenate([pre_clean, base_clean])
        labels = np.concatenate(
            [
                np.ones(pre_clean.size, dtype=bool),
                np.zeros(base_clean.size, dtype=bool),
            ]
        )
    else:
        scores = -np.concatenate([pre_clean, base_clean])
        labels = np.concatenate(
            [
                np.ones(pre_clean.size, dtype=bool),
                np.zeros(base_clean.size, dtype=bool),
            ]
        )

    if scores.size == 0:
        return ThresholdPick(
            threshold=0.0,
            true_positive_rate=0.0,
            false_positive_rate=0.0,
        )

    if precursor.threshold_method == ThresholdMethod.YOUDEN_J:
        pick = youden_j(scores, labels)
    elif precursor.threshold_method == ThresholdMethod.F1:
        pick = f1_threshold(scores, labels)
    elif precursor.threshold_method == ThresholdMethod.QUANTILE_95:
        baseline_for_quantile = (
            -base_clean if precursor.direction == "low_signals_event" else base_clean
        )
        pick = quantile_95(baseline_for_quantile)
    elif precursor.threshold_method == ThresholdMethod.MANUAL:
        pick = manual(
            float(precursor.manual_threshold or 0.0),
            scores=scores,
            labels=labels,
        )
    else:
        raise ValueError(f"unknown threshold_method {precursor.threshold_method!r}")

    # Convert the chosen threshold back into the original sign space.
    if precursor.direction == "low_signals_event":
        pick = ThresholdPick(
            threshold=-pick.threshold,
            true_positive_rate=pick.true_positive_rate,
            false_positive_rate=pick.false_positive_rate,
        )
    return pick


def _build_signature_result(
    *,
    cls: EventClass,
    precursor: PrecursorVariable,
    samples: _PrecursorSamples,
    pick: ThresholdPick,
    library_version: int,
    bootstrap_iterations: int,
    rng: np.random.Generator,
    when: datetime,
) -> SignatureResult:
    pre = samples.pre_event_values[~np.isnan(samples.pre_event_values)]
    base = samples.baseline_values[~np.isnan(samples.baseline_values)]

    pre_summary = _summary(pre)
    base_summary = _summary(base)
    cohens_d = _cohens_d(pre, base)
    bootstrap_stability = _bootstrap_threshold_stability(
        precursor=precursor,
        samples=samples,
        rng=rng,
        iterations=bootstrap_iterations,
    )
    confidence_score = _confidence(
        n_events=int(pre.size),
        cohens_d=cohens_d,
        bootstrap_stability=bootstrap_stability,
    )
    low_confidence = int(pre.size) < LOW_CONFIDENCE_EVENT_THRESHOLD or confidence_score < 0.3

    return SignatureResult(
        class_id=cls.class_id,
        variable_id=precursor.variable_id,
        library_version=library_version,
        definition_version=cls.definition_version,
        threshold_method=precursor.threshold_method.value,
        threshold_value=float(pick.threshold) if not math.isnan(pick.threshold) else None,
        direction=precursor.direction,
        lead_time_window_days=int(precursor.lead_time_window.total_seconds() / 86400),
        pre_event_mean=pre_summary["mean"],
        pre_event_p25=pre_summary["p25"],
        pre_event_p50=pre_summary["p50"],
        pre_event_p75=pre_summary["p75"],
        baseline_mean=base_summary["mean"],
        baseline_p25=base_summary["p25"],
        baseline_p50=base_summary["p50"],
        baseline_p75=base_summary["p75"],
        hit_rate=(
            float(pick.true_positive_rate) if not math.isnan(pick.true_positive_rate) else None
        ),
        false_positive_rate=(
            float(pick.false_positive_rate) if not math.isnan(pick.false_positive_rate) else None
        ),
        sample_size_events=int(pre.size),
        sample_size_baseline=int(base.size),
        confidence_score=float(confidence_score),
        computed_at=when,
        low_confidence_warning=bool(low_confidence),
    )


def _empty_signature(
    *,
    cls: EventClass,
    precursor: PrecursorVariable,
    library_version: int,
    when: datetime,
    note: str,
) -> SignatureResult:
    """Construct a zero-confidence signature for a failed precursor extraction."""
    logger.warning("signature %s.%s: %s", cls.class_id, precursor.variable_id, note)
    return SignatureResult(
        class_id=cls.class_id,
        variable_id=precursor.variable_id,
        library_version=library_version,
        definition_version=cls.definition_version,
        threshold_method=precursor.threshold_method.value,
        threshold_value=None,
        direction=precursor.direction,
        lead_time_window_days=int(precursor.lead_time_window.total_seconds() / 86400),
        pre_event_mean=None,
        pre_event_p25=None,
        pre_event_p50=None,
        pre_event_p75=None,
        baseline_mean=None,
        baseline_p25=None,
        baseline_p50=None,
        baseline_p75=None,
        hit_rate=None,
        false_positive_rate=None,
        sample_size_events=0,
        sample_size_baseline=0,
        confidence_score=0.0,
        computed_at=when,
        low_confidence_warning=True,
    )


def _summary(values: np.ndarray) -> dict[str, float | None]:
    if values.size == 0:
        return {"mean": None, "p25": None, "p50": None, "p75": None}
    return {
        "mean": float(np.mean(values)),
        "p25": float(np.percentile(values, 25)),
        "p50": float(np.percentile(values, 50)),
        "p75": float(np.percentile(values, 75)),
    }


def _cohens_d(events: np.ndarray, nonevents: np.ndarray) -> float:
    """Pooled-standard-deviation effect size between the two populations.

    When both populations have zero variance (constant values, edge case
    in synthetic fixtures), return 1.0 if the means differ and 0.0 if
    they don't — a perfect-signal fixture should not collapse to a
    zero effect size just because std=0.
    """
    if events.size < 2 or nonevents.size < 2:
        return 0.0
    mean_diff = float(np.mean(events) - np.mean(nonevents))
    pooled_var = (
        (events.size - 1) * float(np.var(events, ddof=1))
        + (nonevents.size - 1) * float(np.var(nonevents, ddof=1))
    ) / max(1, events.size + nonevents.size - 2)
    if pooled_var <= 0:
        # Both populations have zero variance. If their means differ, the
        # signal is effectively perfectly separated → return the large-effect
        # ceiling. If the means match, no effect.
        return 1.0 if abs(mean_diff) > 0 else 0.0
    pooled_std = math.sqrt(pooled_var)
    if pooled_std == 0:
        return 1.0 if abs(mean_diff) > 0 else 0.0
    return abs(mean_diff) / pooled_std


def _bootstrap_threshold_stability(
    *,
    precursor: PrecursorVariable,
    samples: _PrecursorSamples,
    rng: np.random.Generator,
    iterations: int,
) -> float:
    """Fraction of bootstrap replicates whose chosen threshold is within
    one standard deviation of the original pick.

    Returns 0.5 when there's not enough data to bootstrap reliably.
    """
    pre_clean = samples.pre_event_values[~np.isnan(samples.pre_event_values)]
    base_clean = samples.baseline_values[~np.isnan(samples.baseline_values)]
    if pre_clean.size < 3 or base_clean.size < 3:
        return 0.5

    base_pick = _pick_threshold(precursor, samples)
    base_threshold = float(base_pick.threshold)
    base_std = (
        float(np.std(np.concatenate([pre_clean, base_clean])))
        if pre_clean.size + base_clean.size > 1
        else 0.0
    )
    if base_std == 0.0:
        return 0.5
    tolerance = base_std

    in_tolerance = 0
    for _ in range(iterations):
        pre_sample = rng.choice(pre_clean, size=pre_clean.size, replace=True)
        base_sample = rng.choice(base_clean, size=base_clean.size, replace=True)
        sub = _PrecursorSamples(
            pre_event_values=pre_sample,
            baseline_values=base_sample,
            baseline_timestamps=samples.baseline_timestamps,
        )
        pick = _pick_threshold(precursor, sub)
        if abs(pick.threshold - base_threshold) <= tolerance:
            in_tolerance += 1
    return in_tolerance / iterations


def _confidence(
    *,
    n_events: int,
    cohens_d: float,
    bootstrap_stability: float,
) -> float:
    """Combine three sub-scores into a final confidence in [0, 1].

    The three components:
    - Sample-size weight: 1 - e^(-n/10), so n=10 gives ~0.63, n=30
      gives ~0.95.
    - Effect size: Cohen's d clipped to [0, 1] (d=1 is "large effect").
    - Bootstrap stability: fraction of resamples within tolerance.

    The product is the final score, which keeps any single component
    being weak from inflating overall confidence.
    """
    sample_weight = 1.0 - math.exp(-n_events / 10.0)
    effect = max(0.0, min(1.0, cohens_d))
    stability = max(0.0, min(1.0, bootstrap_stability))
    return float(sample_weight * effect * stability)


# Public alias for the engine module — exported here so the refresh
# runner doesn't need to know about the helper-only types.
__all__ = [
    "DEFAULT_BOOTSTRAP_ITERATIONS",
    "LOW_CONFIDENCE_EVENT_THRESHOLD",
    "CombinedScore",
    "build_co_occurrence_table",
    "combine_variables",
    "compute_signature",
]


def _unused_import_marker() -> Iterable[str]:  # pragma: no cover
    """Keep the otherwise-unused Iterable import live."""
    return ()
