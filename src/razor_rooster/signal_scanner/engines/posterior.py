"""Posterior computation with Monte Carlo CI propagation (T-SCAN-020).

Implements Bayesian update with naive-Bayes-style likelihood ratios
and a 1,000-sample Monte Carlo for credible-interval propagation
(OQ-SCAN-001 / OQ-SCAN-002 resolutions; design §3.5).

Per precursor: when the current value crosses the threshold (in the
direction the class declares), the likelihood ratio is
``hit / fpr``. When it doesn't, the LR is ``(1 - hit) / (1 - fpr)``.
The product of LRs across precursors is the joint update on the prior
odds, with a co-occurrence correction term applied at the end to
discount over-stating joint signal when historical co-occurrence is
high.

Monte Carlo sampling: prior probability is drawn from a Beta posterior
parameterised by the class's ``prior_alpha`` / ``prior_beta`` plus
the observed occurrence count. Per-variable hit/fpr rates are drawn
from beta posteriors parameterised by the signature's sample sizes.
At v1 scale (8 classes, up to 10 variables, 1000 samples) this is
sub-second per class.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats

from razor_rooster.pattern_library.models.base_rate import BaseRateResult
from razor_rooster.pattern_library.models.signature import SignatureResult

logger = logging.getLogger(__name__)


DEFAULT_MONTE_CARLO_SAMPLES: int = 1000


@dataclass(frozen=True, slots=True)
class PosteriorResult:
    """Output of the posterior engine for one class evaluation."""

    posterior: float
    posterior_ci_lower: float
    posterior_ci_upper: float
    log_odds_shift: float
    n_samples: int
    fired_count: int
    likelihood_ratios: tuple[float, ...]
    co_occurrence_correction: float


def posterior_with_ci(
    base_rate: BaseRateResult,
    signatures: Sequence[SignatureResult],
    current_values: Mapping[str, float | None],
    *,
    co_occurrence_correction: float = 0.0,
    n_samples: int = DEFAULT_MONTE_CARLO_SAMPLES,
    rng: np.random.Generator | None = None,
) -> PosteriorResult:
    """Return point estimate + 95% credible interval for the posterior.

    Args:
        base_rate: BaseRateResult from pattern_library for the class.
            Provides the prior point estimate (rate_per_year clamped
            to a probability) and the CI used to seed the prior beta
            sampling distribution.
        signatures: Per-precursor signature results. Variables with
            ``hit_rate is None`` or ``false_positive_rate is None``
            are skipped (they carry no signal).
        current_values: Map from ``variable_id`` to the operator's
            current value of that variable. ``None`` skips the
            variable as if it were unmeasurable in this scan.
        co_occurrence_correction: log-odds adjustment applied after
            the per-variable updates. Positive amplifies, negative
            attenuates. v1 default is 0 (no correction); the
            scan orchestrator computes a per-class value when it has
            access to pattern_library's co-occurrence cache.
        n_samples: Monte Carlo sample count. v1 default is 1000.
        rng: Optional seeded NumPy generator for reproducibility.

    Returns:
        :class:`PosteriorResult` with point posterior, 95% CI bounds,
        log-odds shift, sample count, and the LRs that were applied
        (for trace generation).
    """
    if rng is None:
        rng = np.random.default_rng(seed=hash(base_rate.class_id) & 0xFFFFFFFF)
    if n_samples < 100:
        raise ValueError(f"n_samples must be >= 100, got {n_samples}")

    prior_point = _rate_to_probability(base_rate.rate_per_year)
    prior_alpha, prior_beta = _prior_beta_params(base_rate)
    prior_samples = stats.beta.rvs(prior_alpha, prior_beta, size=n_samples, random_state=rng)
    prior_samples = np.clip(prior_samples, 1e-6, 1 - 1e-6)

    # Apply per-precursor likelihood updates in log-odds space so the
    # math stays well-conditioned for tiny base rates.
    log_odds = np.log(prior_samples / (1.0 - prior_samples))
    fired_count = 0
    lrs: list[float] = []
    for sig in signatures:
        if sig.hit_rate is None or sig.false_positive_rate is None:
            continue
        current = current_values.get(sig.variable_id)
        if current is None or sig.threshold_value is None:
            continue
        fired = _direction_fired(current, sig.threshold_value, sig.direction)
        hit_samples = _sample_rate(
            rate=sig.hit_rate, n=sig.sample_size_events, size=n_samples, rng=rng
        )
        fpr_samples = _sample_rate(
            rate=sig.false_positive_rate, n=sig.sample_size_baseline, size=n_samples, rng=rng
        )
        lr_samples = (
            np.where(hit_samples > 0.0, hit_samples / np.maximum(fpr_samples, 1e-6), 1e-6)
            if fired
            else np.where(
                hit_samples < 1.0, (1.0 - hit_samples) / np.maximum(1.0 - fpr_samples, 1e-6), 1e-6
            )
        )
        lr_samples = np.clip(lr_samples, 1e-3, 1e3)
        log_odds = log_odds + np.log(lr_samples)
        if fired:
            fired_count += 1
        # Use the point-estimate LR for the trace.
        if fired:
            point_lr = sig.hit_rate / max(sig.false_positive_rate, 1e-6)
        else:
            point_lr = (1.0 - sig.hit_rate) / max(1.0 - sig.false_positive_rate, 1e-6)
        lrs.append(float(point_lr))

    log_odds = log_odds + co_occurrence_correction
    posterior_samples = 1.0 / (1.0 + np.exp(-log_odds))
    posterior_samples = np.clip(posterior_samples, 1e-6, 1 - 1e-6)

    point = float(posterior_samples.mean())
    ci_lower = float(np.percentile(posterior_samples, 2.5))
    ci_upper = float(np.percentile(posterior_samples, 97.5))
    log_odds_shift = float(
        np.log(point / (1.0 - point)) - np.log(prior_point / (1.0 - prior_point))
    )

    return PosteriorResult(
        posterior=point,
        posterior_ci_lower=ci_lower,
        posterior_ci_upper=ci_upper,
        log_odds_shift=log_odds_shift,
        n_samples=n_samples,
        fired_count=fired_count,
        likelihood_ratios=tuple(lrs),
        co_occurrence_correction=co_occurrence_correction,
    )


def base_rate_only(base_rate: BaseRateResult) -> PosteriorResult:
    """Posterior result for the no-update fallback (REQ-SCAN-PROB-003).

    When current data is missing or stale the posterior equals the
    prior; the CI matches the base rate's CI; the log-odds shift is
    zero. Used by the scan orchestrator when it cannot evaluate
    precursors.
    """
    prior_point = _rate_to_probability(base_rate.rate_per_year)
    return PosteriorResult(
        posterior=prior_point,
        posterior_ci_lower=_rate_to_probability(base_rate.credible_interval_lower),
        posterior_ci_upper=_rate_to_probability(base_rate.credible_interval_upper),
        log_odds_shift=0.0,
        n_samples=0,
        fired_count=0,
        likelihood_ratios=(),
        co_occurrence_correction=0.0,
    )


# -- internals --------------------------------------------------------------


def _direction_fired(value: float, threshold: float, direction: str) -> bool:
    if direction == "high_signals_event":
        return value >= threshold
    if direction == "low_signals_event":
        return value <= threshold
    raise ValueError(f"unknown direction {direction!r}")


def _sample_rate(
    *,
    rate: float,
    n: int,
    size: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Sample a binomial-rate posterior given a point estimate and n.

    Uses Beta(alpha, beta) with alpha = rate*n + 0.5, beta = (1-rate)*n
    + 0.5 (Jeffreys prior). When n is zero we collapse to the point
    estimate (no posterior to sample from).
    """
    if n <= 0:
        return np.full(size, max(min(rate, 1.0), 0.0), dtype=float)
    alpha = max(rate * n + 0.5, 0.5)
    beta = max((1.0 - rate) * n + 0.5, 0.5)
    samples = stats.beta.rvs(alpha, beta, size=size, random_state=rng)
    return np.asarray(samples, dtype=float)


def _prior_beta_params(base_rate: BaseRateResult) -> tuple[float, float]:
    """Return (alpha, beta) for sampling the prior probability.

    The base-rate result reports a Beta posterior over the per-year
    occurrence rate. We approximate sampling that posterior by
    matching a Beta distribution to the reported (rate_per_year,
    credible interval) pair: the mean is the rate, the variance is
    derived from the CI width assuming approximately symmetric tails
    in probability space. This is exact for the Jeffreys-prior case
    when occurrences are small, and a reasonable approximation
    otherwise.

    For the no-occurrences edge case we fall back to the prior alone.
    """
    rate = float(min(max(base_rate.rate_per_year, 1e-6), 0.999))
    if base_rate.occurrences <= 0:
        return base_rate.prior_alpha, base_rate.prior_beta

    ci_width = max(
        base_rate.credible_interval_upper - base_rate.credible_interval_lower,
        1e-6,
    )
    # 95% CI of a normal approximation has total width about 4 sigma;
    # convert to Beta concentration. Beta(a, b) with mean mu has
    # variance mu*(1-mu) / (a+b+1), so a+b+1 = mu*(1-mu) / sigma**2.
    sigma = ci_width / 4.0
    if sigma <= 0:
        return base_rate.prior_alpha, base_rate.prior_beta
    concentration = max(rate * (1.0 - rate) / (sigma * sigma) - 1.0, 1.0)
    alpha = max(rate * concentration, 0.5)
    beta = max((1.0 - rate) * concentration, 0.5)
    return alpha, beta


def _rate_to_probability(rate_per_year: float) -> float:
    """Clamp a per-year rate into the [0, 1] probability domain.

    Base rates from pattern_library are reported as occurrences per
    year, which is the same as a probability for the "in any given
    year" question when the rate is small. For rates approaching 1
    we cap at 0.999 so log-odds math stays well-defined.
    """
    return float(min(max(rate_per_year, 1e-6), 0.999))
