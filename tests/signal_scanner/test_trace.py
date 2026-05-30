"""T-SCAN-021 — trace builder/renderer acceptance tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pandas as pd

from razor_rooster.pattern_library.models.base_rate import BaseRateResult
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    PrecursorVariable,
    Sector,
)
from razor_rooster.pattern_library.models.signature import SignatureResult
from razor_rooster.signal_scanner.engines.posterior import PosteriorResult
from razor_rooster.signal_scanner.engines.trace import build_trace, render_trace_text


def _occurrences(_conn: object) -> pd.DataFrame:
    return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})


def _precursor_query(_conn: object, _start: object, _end: object) -> pd.Series:
    return pd.Series(dtype=float)


def _make_class() -> EventClass:
    return EventClass(
        class_id="test_class",
        title="Test class",
        description="Synthetic class for trace tests",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_occurrences,
        precursors=(
            PrecursorVariable(
                variable_id="v1",
                title="First precursor",
                query=_precursor_query,
                direction="high_signals_event",
                lead_time_window=timedelta(days=180),
            ),
        ),
    )


def _make_base_rate() -> BaseRateResult:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    return BaseRateResult(
        class_id="test_class",
        window_start=now - timedelta(days=365 * 10),
        window_end=now,
        occurrences=5,
        rate_per_year=0.05,
        credible_interval_lower=0.02,
        credible_interval_upper=0.10,
        prior_alpha=0.5,
        prior_beta=0.5,
        library_version=1,
        definition_version=1,
        data_as_of=now,
        computed_at=now,
    )


def _make_signature() -> SignatureResult:
    now = datetime(2026, 5, 15, tzinfo=UTC)
    return SignatureResult(
        class_id="test_class",
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
        sample_size_events=20,
        sample_size_baseline=200,
        confidence_score=0.8,
        computed_at=now,
    )


def _posterior_result() -> PosteriorResult:
    return PosteriorResult(
        posterior=0.18,
        posterior_ci_lower=0.07,
        posterior_ci_upper=0.34,
        log_odds_shift=1.2,
        n_samples=1000,
        fired_count=1,
        likelihood_ratios=(3.5,),
        co_occurrence_correction=0.0,
    )


def test_build_trace_populates_expected_fields() -> None:
    cls = _make_class()
    base = _make_base_rate()
    sig = _make_signature()
    posterior = _posterior_result()
    trace = build_trace(
        cls=cls,
        base_rate=base,
        signatures=[sig],
        current_values={"v1": 8.0},
        posterior=posterior,
        is_candidate=True,
        candidate_direction="elevated",
        warnings=("low_sample",),
        library_version=1,
        data_as_of=base.data_as_of,
    )
    assert trace["class_id"] == "test_class"
    assert trace["library_version"] == 1
    assert trace["prior"]["point"] == 0.05
    assert trace["posterior"]["point"] == 0.18
    assert trace["log_odds_shift"] == 1.2
    assert trace["is_candidate"] is True
    assert trace["candidate_direction"] == "elevated"
    assert trace["warnings"] == ["low_sample"]
    assert len(trace["precursors"]) == 1
    p = trace["precursors"][0]
    assert p["variable_id"] == "v1"
    assert p["title"] == "First precursor"
    assert p["fired"] is True
    assert p["hit_rate"] == 0.7
    assert p["likelihood_ratio_applied"] == 3.5
    assert trace["ci_method"] == "monte_carlo_1000_samples"


def test_build_trace_no_update_short_circuits_precursors() -> None:
    cls = _make_class()
    base = _make_base_rate()
    posterior = PosteriorResult(
        posterior=0.05,
        posterior_ci_lower=0.02,
        posterior_ci_upper=0.10,
        log_odds_shift=0.0,
        n_samples=0,
        fired_count=0,
        likelihood_ratios=(),
        co_occurrence_correction=0.0,
    )
    trace = build_trace(
        cls=cls,
        base_rate=base,
        signatures=[_make_signature()],
        current_values={},
        posterior=posterior,
        is_candidate=False,
        candidate_direction=None,
        warnings=("source_stale",),
        no_update_applied=True,
        no_update_reason="all sources stale",
        library_version=1,
        data_as_of=base.data_as_of,
    )
    assert trace["no_update_applied"] is True
    assert trace["no_update_reason"] == "all sources stale"
    assert trace["precursors"] == []
    assert trace["ci_method"] == "no_update_prior_passthrough"


def test_trace_is_json_serializable() -> None:
    """The trace dict must round-trip through json without errors."""
    cls = _make_class()
    base = _make_base_rate()
    sig = _make_signature()
    posterior = _posterior_result()
    trace = build_trace(
        cls=cls,
        base_rate=base,
        signatures=[sig],
        current_values={"v1": 8.0},
        posterior=posterior,
        is_candidate=True,
        candidate_direction="elevated",
        warnings=(),
        library_version=1,
        data_as_of=base.data_as_of,
    )
    serialized = json.dumps(trace)
    deserialized = json.loads(serialized)
    assert deserialized == trace


def test_render_trace_text_contains_expected_lines() -> None:
    cls = _make_class()
    base = _make_base_rate()
    sig = _make_signature()
    posterior = _posterior_result()
    trace = build_trace(
        cls=cls,
        base_rate=base,
        signatures=[sig],
        current_values={"v1": 8.0},
        posterior=posterior,
        is_candidate=True,
        candidate_direction="elevated",
        warnings=("low_sample",),
        library_version=1,
        data_as_of=base.data_as_of,
    )
    rendered = render_trace_text(trace)
    assert "test_class" in rendered
    assert "FIRED" in rendered  # variable above threshold
    assert "elevated" in rendered
    assert "low_sample" in rendered
    assert "monte_carlo_1000_samples" in rendered


def test_render_trace_text_handles_no_update() -> None:
    cls = _make_class()
    base = _make_base_rate()
    posterior = PosteriorResult(
        posterior=0.05,
        posterior_ci_lower=0.02,
        posterior_ci_upper=0.10,
        log_odds_shift=0.0,
        n_samples=0,
        fired_count=0,
        likelihood_ratios=(),
        co_occurrence_correction=0.0,
    )
    trace = build_trace(
        cls=cls,
        base_rate=base,
        signatures=[],
        current_values={},
        posterior=posterior,
        is_candidate=False,
        candidate_direction=None,
        warnings=(),
        no_update_applied=True,
        no_update_reason="missing data",
        library_version=1,
        data_as_of=base.data_as_of,
    )
    rendered = render_trace_text(trace)
    assert "no_update" in rendered
    assert "missing data" in rendered
    assert "no_update_prior_passthrough" in rendered
