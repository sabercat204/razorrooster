"""T-PL-010 — model dataclass validation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from razor_rooster.pattern_library.models.analogue import (
    AnalogueFeatureSpace,
    AnalogueMatch,
    AnalogueResults,
)
from razor_rooster.pattern_library.models.base_rate import BaseRateResult
from razor_rooster.pattern_library.models.calibration import (
    CalibrationOutput,
    ReliabilityBin,
)
from razor_rooster.pattern_library.models.event_class import (
    AnalogueFeature,
    BaselineStrategy,
    EventClass,
    Normalization,
    PrecursorVariable,
    Sector,
    ThresholdMethod,
)
from razor_rooster.pattern_library.models.outcomes import OutcomeRecord
from razor_rooster.pattern_library.models.signature import SignatureResult

# -- EventClass + nested ---------------------------------------------------


def _stub_query(*_args: object, **_kwargs: object) -> object:
    return None


def _valid_event_class() -> EventClass:
    return EventClass(
        class_id="test_class",
        title="Test Class",
        description="A test class.",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_stub_query,
    )


def test_event_class_valid_construction() -> None:
    cls = _valid_event_class()
    assert cls.class_id == "test_class"
    assert cls.outcome_type == "binary"
    assert cls.definition_version == 1
    assert cls.prior_alpha == 0.5
    assert cls.prior_beta == 0.5


def test_event_class_rejects_empty_class_id() -> None:
    with pytest.raises(ValueError, match="class_id"):
        EventClass(
            class_id="",
            title="x",
            description="x",
            domain_sector=Sector.GEOPOLITICAL,
            occurrence_query=_stub_query,
        )


def test_event_class_rejects_invalid_class_id_characters() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        EventClass(
            class_id="bad-id-with-dashes",
            title="x",
            description="x",
            domain_sector=Sector.GEOPOLITICAL,
            occurrence_query=_stub_query,
        )


def test_event_class_rejects_non_binary_outcome() -> None:
    """OQ-PL-006: only binary classes in v1."""
    with pytest.raises(ValueError, match="outcome_type"):
        EventClass(
            class_id="test_class",
            title="x",
            description="x",
            domain_sector=Sector.GEOPOLITICAL,
            occurrence_query=_stub_query,
            outcome_type="continuous",  # type: ignore[arg-type]
        )


def test_event_class_rejects_zero_definition_version() -> None:
    with pytest.raises(ValueError, match="definition_version"):
        EventClass(
            class_id="test_class",
            title="x",
            description="x",
            domain_sector=Sector.GEOPOLITICAL,
            occurrence_query=_stub_query,
            definition_version=0,
        )


def test_event_class_rejects_zero_refractory_months() -> None:
    with pytest.raises(ValueError, match="refractory_months"):
        EventClass(
            class_id="test_class",
            title="x",
            description="x",
            domain_sector=Sector.GEOPOLITICAL,
            occurrence_query=_stub_query,
            refractory_months=0,
        )


def test_event_class_rejects_invalid_priors() -> None:
    with pytest.raises(ValueError, match="prior"):
        EventClass(
            class_id="test_class",
            title="x",
            description="x",
            domain_sector=Sector.GEOPOLITICAL,
            occurrence_query=_stub_query,
            prior_alpha=0.0,
        )


def test_event_class_rejects_duplicate_secondary_sector() -> None:
    with pytest.raises(ValueError, match="secondary_sectors"):
        EventClass(
            class_id="test_class",
            title="x",
            description="x",
            domain_sector=Sector.GEOPOLITICAL,
            occurrence_query=_stub_query,
            secondary_sectors=(Sector.GEOPOLITICAL,),
        )


def test_event_class_rejects_duplicate_precursor_ids() -> None:
    p = PrecursorVariable(
        variable_id="p",
        title="P",
        query=_stub_query,
        direction="high_signals_event",
    )
    with pytest.raises(ValueError, match="precursor variable_ids"):
        EventClass(
            class_id="test_class",
            title="x",
            description="x",
            domain_sector=Sector.GEOPOLITICAL,
            occurrence_query=_stub_query,
            precursors=(p, p),
        )


def test_event_class_rejects_duplicate_feature_ids() -> None:
    f = AnalogueFeature(feature_id="f", query=_stub_query)
    with pytest.raises(ValueError, match="analogue feature_ids"):
        EventClass(
            class_id="test_class",
            title="x",
            description="x",
            domain_sector=Sector.GEOPOLITICAL,
            occurrence_query=_stub_query,
            analogue_features=(f, f),
        )


def test_event_class_with_full_config() -> None:
    """A class with all knobs explicitly set should construct cleanly."""
    p = PrecursorVariable(
        variable_id="p",
        title="P",
        query=_stub_query,
        direction="low_signals_event",
        lead_time_window=timedelta(days=90),
        threshold_method=ThresholdMethod.QUANTILE_95,
    )
    f = AnalogueFeature(feature_id="f", query=_stub_query, weight=2.0)
    cls = EventClass(
        class_id="full_class",
        title="Full",
        description="All knobs.",
        domain_sector=Sector.CLIMATE,
        secondary_sectors=(Sector.COMMODITY,),
        occurrence_query=_stub_query,
        precursors=(p,),
        analogue_features=(f,),
        baseline_strategy=BaselineStrategy.UNIFORM_RANDOM,
        baseline_sample_size=500,
        prior_alpha=1.0,
        prior_beta=1.0,
        refractory_months=6,
    )
    assert cls.precursors[0].lead_time_window == timedelta(days=90)
    assert cls.analogue_features[0].weight == 2.0
    assert cls.baseline_strategy == BaselineStrategy.UNIFORM_RANDOM


def test_precursor_manual_threshold_required_when_method_is_manual() -> None:
    with pytest.raises(ValueError, match="manual_threshold"):
        PrecursorVariable(
            variable_id="p",
            title="P",
            query=_stub_query,
            direction="high_signals_event",
            threshold_method=ThresholdMethod.MANUAL,
        )


def test_precursor_manual_threshold_rejected_when_method_isnt_manual() -> None:
    with pytest.raises(ValueError, match="manual_threshold"):
        PrecursorVariable(
            variable_id="p",
            title="P",
            query=_stub_query,
            direction="high_signals_event",
            threshold_method=ThresholdMethod.YOUDEN_J,
            manual_threshold=0.5,
        )


def test_analogue_feature_rejects_zero_weight() -> None:
    with pytest.raises(ValueError, match="weight"):
        AnalogueFeature(feature_id="f", query=_stub_query, weight=0)


def test_normalization_enum_default_is_zscore() -> None:
    f = AnalogueFeature(feature_id="f", query=_stub_query)
    assert f.normalization == Normalization.ZSCORE


# -- BaseRateResult --------------------------------------------------------


def _br_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "class_id": "c",
        "window_start": datetime(2010, 1, 1, tzinfo=UTC),
        "window_end": datetime(2025, 1, 1, tzinfo=UTC),
        "occurrences": 5,
        "rate_per_year": 0.33,
        "credible_interval_lower": 0.1,
        "credible_interval_upper": 0.9,
        "prior_alpha": 0.5,
        "prior_beta": 0.5,
        "library_version": 1,
        "definition_version": 1,
        "data_as_of": datetime(2026, 5, 14, tzinfo=UTC),
        "computed_at": datetime(2026, 5, 14, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def test_base_rate_valid() -> None:
    br = BaseRateResult(**_br_kwargs())  # type: ignore[arg-type]
    assert br.occurrences == 5
    assert br.low_sample_warning is False


def test_base_rate_rejects_inverted_window() -> None:
    with pytest.raises(ValueError, match="window_start"):
        BaseRateResult(
            **_br_kwargs(  # type: ignore[arg-type]
                window_start=datetime(2030, 1, 1, tzinfo=UTC),
                window_end=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )


def test_base_rate_rejects_inverted_ci() -> None:
    with pytest.raises(ValueError, match="CI"):
        BaseRateResult(
            **_br_kwargs(  # type: ignore[arg-type]
                credible_interval_lower=0.9, credible_interval_upper=0.1
            )
        )


def test_base_rate_rejects_negative_occurrences() -> None:
    with pytest.raises(ValueError, match="occurrences"):
        BaseRateResult(**_br_kwargs(occurrences=-1))  # type: ignore[arg-type]


# -- SignatureResult -------------------------------------------------------


def _sig_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "class_id": "c",
        "variable_id": "v",
        "library_version": 1,
        "definition_version": 1,
        "threshold_method": "youden_j",
        "threshold_value": 0.5,
        "direction": "high_signals_event",
        "lead_time_window_days": 180,
        "pre_event_mean": 0.3,
        "pre_event_p25": 0.1,
        "pre_event_p50": 0.3,
        "pre_event_p75": 0.5,
        "baseline_mean": 0.1,
        "baseline_p25": 0.05,
        "baseline_p50": 0.1,
        "baseline_p75": 0.15,
        "hit_rate": 0.8,
        "false_positive_rate": 0.1,
        "sample_size_events": 10,
        "sample_size_baseline": 1000,
        "confidence_score": 0.7,
        "computed_at": datetime(2026, 5, 14, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def test_signature_result_valid() -> None:
    sig = SignatureResult(**_sig_kwargs())  # type: ignore[arg-type]
    assert sig.confidence_score == 0.7
    assert sig.low_confidence_warning is False


def test_signature_result_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError, match="confidence_score"):
        SignatureResult(**_sig_kwargs(confidence_score=1.5))  # type: ignore[arg-type]


def test_signature_result_rejects_out_of_range_hit_rate() -> None:
    with pytest.raises(ValueError, match="hit_rate"):
        SignatureResult(**_sig_kwargs(hit_rate=1.5))  # type: ignore[arg-type]


# -- AnalogueFeatureSpace + AnalogueMatch + AnalogueResults ---------------


def test_analogue_feature_space_valid() -> None:
    space = AnalogueFeatureSpace(
        class_id="c",
        library_version=1,
        definition_version=1,
        feature_ids=("f1", "f2"),
        point_count=100,
        event_count=10,
        normalization_params={"f1": {"mean": 0.0, "std": 1.0}},
    )
    assert space.event_count == 10


def test_analogue_feature_space_rejects_bad_counts() -> None:
    with pytest.raises(ValueError, match="point_count"):
        AnalogueFeatureSpace(
            class_id="c",
            library_version=1,
            definition_version=1,
            feature_ids=("f1",),
            point_count=5,
            event_count=10,
            normalization_params={},
        )


def test_analogue_feature_space_rejects_empty_features() -> None:
    with pytest.raises(ValueError, match="feature_ids"):
        AnalogueFeatureSpace(
            class_id="c",
            library_version=1,
            definition_version=1,
            feature_ids=(),
            point_count=10,
            event_count=1,
            normalization_params={},
        )


def test_analogue_match_rejects_negative_distance() -> None:
    with pytest.raises(ValueError, match="distance"):
        AnalogueMatch(
            point_id="event:1",
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            is_event=True,
            distance=-0.1,
            feature_vector_normalized={"f1": 0.0},
        )


def test_analogue_results_default_empty() -> None:
    res = AnalogueResults(
        class_id="c",
        library_version=1,
        definition_version=1,
        query_timestamp=datetime(2026, 5, 14, tzinfo=UTC),
    )
    assert res.matches == ()


# -- CalibrationOutput + ReliabilityBin -----------------------------------


def test_reliability_bin_valid() -> None:
    bin_ = ReliabilityBin(
        bin_low=0.0,
        bin_high=0.1,
        predicted_mean=0.05,
        observed_freq=0.04,
        count=20,
    )
    assert bin_.count == 20


def test_reliability_bin_rejects_inverted_edges() -> None:
    with pytest.raises(ValueError, match="bin_low"):
        ReliabilityBin(
            bin_low=0.5,
            bin_high=0.1,
            predicted_mean=0.3,
            observed_freq=0.3,
            count=5,
        )


def test_reliability_bin_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="bin edges"):
        ReliabilityBin(
            bin_low=-0.1,
            bin_high=0.1,
            predicted_mean=0.0,
            observed_freq=0.0,
            count=1,
        )


def test_reliability_bin_rejects_negative_count() -> None:
    with pytest.raises(ValueError, match="count"):
        ReliabilityBin(
            bin_low=0.0,
            bin_high=0.1,
            predicted_mean=0.05,
            observed_freq=0.04,
            count=-1,
        )


def test_calibration_output_insufficient_data() -> None:
    """Sparse class → method='insufficient_data' with brier_score=None."""
    out = CalibrationOutput(
        class_id="rare_class",
        library_version=1,
        definition_version=1,
        method="insufficient_data",
        brier_score=None,
        reliability_bins=(),
        prediction_trace_path="data/library/calibration/rare_class.json",
        computed_at=datetime(2026, 5, 14, tzinfo=UTC),
    )
    assert out.brier_score is None


def test_calibration_output_rejects_out_of_range_brier() -> None:
    with pytest.raises(ValueError, match="brier_score"):
        CalibrationOutput(
            class_id="c",
            library_version=1,
            definition_version=1,
            method="leave_one_out_signature",
            brier_score=1.5,
            reliability_bins=(),
            prediction_trace_path="path",
            computed_at=datetime(2026, 5, 14, tzinfo=UTC),
        )


# -- OutcomeRecord ---------------------------------------------------------


def test_outcome_record_valid() -> None:
    rec = OutcomeRecord(
        class_id="c",
        occurrence_id="abc123",
        occurrence_ts=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert rec.source_records == ()
    assert rec.end_ts is None


def test_outcome_record_rejects_empty_class_id() -> None:
    with pytest.raises(ValueError, match="class_id"):
        OutcomeRecord(
            class_id="",
            occurrence_id="abc",
            occurrence_ts=datetime(2025, 1, 1, tzinfo=UTC),
        )


def test_outcome_record_rejects_end_before_start() -> None:
    with pytest.raises(ValueError, match="end_ts"):
        OutcomeRecord(
            class_id="c",
            occurrence_id="abc",
            occurrence_ts=datetime(2025, 1, 1, tzinfo=UTC),
            end_ts=datetime(2024, 1, 1, tzinfo=UTC),
        )
