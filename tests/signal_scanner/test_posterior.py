"""T-SCAN-020 — posterior computation acceptance tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from razor_rooster.pattern_library.models.base_rate import BaseRateResult
from razor_rooster.pattern_library.models.signature import SignatureResult
from razor_rooster.signal_scanner.engines.posterior import (
    DEFAULT_MONTE_CARLO_SAMPLES,
    base_rate_only,
    posterior_with_ci,
)


def _base_rate(
    rate: float = 0.05,
    occurrences: int = 5,
    *,
    class_id: str = "test-class",
) -> BaseRateResult:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    return BaseRateResult(
        class_id=class_id,
        window_start=now - timedelta(days=365 * 10),
        window_end=now,
        occurrences=occurrences,
        rate_per_year=rate,
        credible_interval_lower=max(rate * 0.5, 1e-4),
        credible_interval_upper=min(rate * 1.5, 0.999),
        prior_alpha=0.5,
        prior_beta=0.5,
        library_version=1,
        definition_version=1,
        data_as_of=now,
        computed_at=now,
    )


def _signature(
    *,
    variable_id: str = "v1",
    hit_rate: float = 0.7,
    fpr: float = 0.2,
    threshold: float = 5.0,
    direction: str = "high_signals_event",
    confidence: float = 0.8,
) -> SignatureResult:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    return SignatureResult(
        class_id="test-class",
        variable_id=variable_id,
        library_version=1,
        definition_version=1,
        threshold_method="youden_j",
        threshold_value=threshold,
        direction=direction,  # type: ignore[arg-type]
        lead_time_window_days=180,
        pre_event_mean=8.0,
        pre_event_p25=6.0,
        pre_event_p50=8.0,
        pre_event_p75=10.0,
        baseline_mean=3.0,
        baseline_p25=1.5,
        baseline_p50=3.0,
        baseline_p75=4.5,
        hit_rate=hit_rate,
        false_positive_rate=fpr,
        sample_size_events=20,
        sample_size_baseline=200,
        confidence_score=confidence,
        computed_at=now,
    )


def test_posterior_increases_when_signature_fires() -> None:
    base = _base_rate(rate=0.05)
    sig = _signature(hit_rate=0.7, fpr=0.1)  # strong positive signal
    rng = np.random.default_rng(seed=42)
    res = posterior_with_ci(base, [sig], current_values={"v1": 8.0}, n_samples=1000, rng=rng)
    assert res.posterior > 0.05  # moved up from prior
    assert res.log_odds_shift > 0
    assert res.fired_count == 1
    assert len(res.likelihood_ratios) == 1


def test_posterior_decreases_when_signature_does_not_fire() -> None:
    base = _base_rate(rate=0.05)
    sig = _signature(hit_rate=0.7, fpr=0.1)
    rng = np.random.default_rng(seed=42)
    res = posterior_with_ci(base, [sig], current_values={"v1": 1.0}, n_samples=1000, rng=rng)
    # Below threshold -> negative LR, posterior should drop slightly
    assert res.fired_count == 0
    assert res.log_odds_shift <= 0


def test_ci_widens_with_uncertain_signatures() -> None:
    """A signature with tiny sample size produces wider posterior CI."""
    base = _base_rate(rate=0.10, occurrences=50)
    confident = _signature()
    confident_rng = np.random.default_rng(seed=42)
    confident_res = posterior_with_ci(
        base, [confident], current_values={"v1": 8.0}, n_samples=2000, rng=confident_rng
    )

    uncertain = SignatureResult(
        class_id="test-class",
        variable_id="v1",
        library_version=1,
        definition_version=1,
        threshold_method="youden_j",
        threshold_value=5.0,
        direction="high_signals_event",
        lead_time_window_days=180,
        pre_event_mean=8.0,
        pre_event_p25=6.0,
        pre_event_p50=8.0,
        pre_event_p75=10.0,
        baseline_mean=3.0,
        baseline_p25=1.5,
        baseline_p50=3.0,
        baseline_p75=4.5,
        hit_rate=0.7,
        false_positive_rate=0.2,
        sample_size_events=2,  # tiny n
        sample_size_baseline=5,
        confidence_score=0.2,
        computed_at=datetime(2026, 5, 15, tzinfo=UTC),
    )
    uncertain_rng = np.random.default_rng(seed=42)
    uncertain_res = posterior_with_ci(
        base, [uncertain], current_values={"v1": 8.0}, n_samples=2000, rng=uncertain_rng
    )

    confident_width = confident_res.posterior_ci_upper - confident_res.posterior_ci_lower
    uncertain_width = uncertain_res.posterior_ci_upper - uncertain_res.posterior_ci_lower
    assert uncertain_width > confident_width


def test_base_rate_only_passthrough() -> None:
    base = _base_rate(rate=0.07)
    res = base_rate_only(base)
    assert res.posterior == pytest.approx(0.07)
    assert res.log_odds_shift == 0.0
    assert res.n_samples == 0
    assert res.likelihood_ratios == ()


def test_co_occurrence_correction_applied_in_log_odds() -> None:
    base = _base_rate(rate=0.05)
    sig = _signature(hit_rate=0.7, fpr=0.2)
    no_correction = posterior_with_ci(
        base,
        [sig],
        current_values={"v1": 8.0},
        co_occurrence_correction=0.0,
        n_samples=1000,
        rng=np.random.default_rng(seed=42),
    )
    with_correction = posterior_with_ci(
        base,
        [sig],
        current_values={"v1": 8.0},
        co_occurrence_correction=-1.0,  # downweight joint signal
        n_samples=1000,
        rng=np.random.default_rng(seed=42),
    )
    # Negative correction should pull posterior down vs. no correction.
    assert with_correction.posterior < no_correction.posterior


def test_n_samples_default() -> None:
    """The default is 1000 per design §3.5."""
    assert DEFAULT_MONTE_CARLO_SAMPLES == 1000


def test_low_n_samples_raises() -> None:
    base = _base_rate()
    with pytest.raises(ValueError):
        posterior_with_ci(base, [], current_values={}, n_samples=10)


def test_skips_signatures_with_missing_rates() -> None:
    """Signatures with hit_rate=None contribute no LR."""
    base = _base_rate(rate=0.05)
    sig_with_rate = _signature(variable_id="v1", hit_rate=0.7, fpr=0.2)
    sig_no_rate = SignatureResult(
        class_id="test-class",
        variable_id="v2",
        library_version=1,
        definition_version=1,
        threshold_method="manual",
        threshold_value=None,
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
        hit_rate=None,
        false_positive_rate=None,
        sample_size_events=0,
        sample_size_baseline=0,
        confidence_score=0.0,
        computed_at=datetime(2026, 5, 15, tzinfo=UTC),
        low_confidence_warning=True,
    )
    rng = np.random.default_rng(seed=42)
    res = posterior_with_ci(
        base,
        [sig_with_rate, sig_no_rate],
        current_values={"v1": 8.0, "v2": 5.0},
        n_samples=1000,
        rng=rng,
    )
    # Only v1 contributes
    assert res.fired_count == 1
    assert len(res.likelihood_ratios) == 1


def test_low_signal_direction_inverted() -> None:
    """A low_signals_event variable fires when value is BELOW threshold."""
    base = _base_rate(rate=0.05)
    sig = _signature(direction="low_signals_event", threshold=5.0, hit_rate=0.7, fpr=0.2)
    rng = np.random.default_rng(seed=42)
    res_below = posterior_with_ci(base, [sig], current_values={"v1": 1.0}, n_samples=1000, rng=rng)
    rng2 = np.random.default_rng(seed=42)
    res_above = posterior_with_ci(
        base, [sig], current_values={"v1": 10.0}, n_samples=1000, rng=rng2
    )
    # below threshold = fired (low_signals_event)
    assert res_below.fired_count == 1
    assert res_above.fired_count == 0


def test_determinism_with_same_seed() -> None:
    base = _base_rate(rate=0.05)
    sig = _signature()
    res_a = posterior_with_ci(
        base,
        [sig],
        current_values={"v1": 8.0},
        n_samples=1000,
        rng=np.random.default_rng(seed=42),
    )
    res_b = posterior_with_ci(
        base,
        [sig],
        current_values={"v1": 8.0},
        n_samples=1000,
        rng=np.random.default_rng(seed=42),
    )
    assert res_a.posterior == pytest.approx(res_b.posterior)
    assert res_a.posterior_ci_lower == pytest.approx(res_b.posterior_ci_lower)
    assert res_a.posterior_ci_upper == pytest.approx(res_b.posterior_ci_upper)


def test_no_signatures_returns_prior() -> None:
    base = _base_rate(rate=0.05)
    rng = np.random.default_rng(seed=42)
    res = posterior_with_ci(base, [], current_values={}, n_samples=1000, rng=rng)
    # With no precursors, posterior point should be close to prior_point.
    assert res.posterior == pytest.approx(0.05, abs=0.04)
    assert abs(res.log_odds_shift) < 0.5
    assert res.fired_count == 0
