"""T-CB-004 — calibration_backtest data model validation tests.

Covers construction, immutability, validator coverage (happy + failure
paths), StrEnum lowercase-string convention, optional-field None
acceptance, and re-export from the package public surface for every
dataclass and enum defined in
:mod:`razor_rooster.calibration_backtest.models`.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from razor_rooster.calibration_backtest import models as models_module
from razor_rooster.calibration_backtest.errors import BacktestConfigError
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
    BacktestTrace,
    CompressionAlgorithm,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
    ReliabilityBin,
    ReliabilityDiagram,
    ScoreSummary,
    SkipReason,
)

# -- helpers ---------------------------------------------------------------


_SINCE = datetime(2024, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2024, 6, 1, tzinfo=UTC)
_STARTED = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
_COMPLETED = datetime(2024, 6, 1, 0, 5, 0, tzinfo=UTC)
_PRED_TS = datetime(2024, 1, 8, tzinfo=UTC)
_RES_TS = datetime(2024, 1, 15, tzinfo=UTC)


def _valid_bin(**overrides: Any) -> ReliabilityBin:
    base: dict[str, Any] = {
        "lower_p": 0.0,
        "upper_p": 0.5,
        "count": 3,
        "mean_predicted_p": 0.25,
        "empirical_rate": 0.33,
    }
    base.update(overrides)
    return ReliabilityBin(**base)


def _valid_diagram() -> ReliabilityDiagram:
    return ReliabilityDiagram(
        bin_count=2,
        bins=(
            _valid_bin(lower_p=0.0, upper_p=0.5),
            _valid_bin(lower_p=0.5, upper_p=1.0),
        ),
    )


def _valid_score_summary(**overrides: Any) -> ScoreSummary:
    base: dict[str, Any] = {
        "overall_brier": 0.21,
        "per_sector_brier": {"public_health": 0.18},
        "per_class_brier": {"flu_h2h": 0.22},
        "reliability_per_sector": {"public_health": _valid_diagram()},
        "zero_resolutions_sectors": (),
        "zero_resolutions_classes": (),
    }
    base.update(overrides)
    return ScoreSummary(**base)


def _valid_run(**overrides: Any) -> BacktestRun:
    base: dict[str, Any] = {
        "run_id": "abc123",
        "since_ts": _SINCE,
        "until_ts": _UNTIL,
        "lag_days": 7,
        "class_ids": ("flu_h2h",),
        "sectors": ("public_health",),
        "venues": ("polymarket",),
        "library_version": 1,
        "system_revision": "deadbeef",
        "started_at": _STARTED,
        "completed_at": _COMPLETED,
        "status": BacktestStatus.COMPLETE,
        "error_summary": None,
        "predictions_total": 10,
        "predictions_scored": 8,
        "predictions_skipped": 2,
        "overall_brier": 0.2,
        "summary_json": {"per_sector": {"public_health": 0.18}},
        "bin_count_global": 10,
        "bin_count_per_sector": {"public_health": 5},
        "fallback_polarity_count": 0,
        "allow_recent": False,
        "disclaimer_version": "v1",
    }
    base.update(overrides)
    return BacktestRun(**base)


def _valid_prediction(**overrides: Any) -> BacktestPrediction:
    base: dict[str, Any] = {
        "run_id": "abc123",
        "prediction_id": "pred-1",
        "class_id": "flu_h2h",
        "condition_id": "cond-1",
        "venue": "polymarket",
        "sector": "public_health",
        "prediction_ts": _PRED_TS,
        "resolution_ts": _RES_TS,
        "model_p": 0.4,
        "observed": 1.0,
        "polarity": PolarityValue.FORWARD,
        "polarity_source": PolaritySource.COMPARISON_RESOLUTIONS,
        "mapping_mismatch_warning": False,
        "definition_version": 1,
        "status": PredictionStatus.SCORED,
        "skip_reason": None,
        "brier_contribution": 0.36,
    }
    base.update(overrides)
    return BacktestPrediction(**base)


def _valid_trace(**overrides: Any) -> BacktestTrace:
    base: dict[str, Any] = {
        "run_id": "abc123",
        "prediction_id": "pred-1",
        "trace_json_compressed": b"\x28\xb5\x2f\xfd",
        "decompressed_size_bytes": 1024,
    }
    base.update(overrides)
    return BacktestTrace(**base)


# -- ReliabilityBin --------------------------------------------------------


class TestReliabilityBin:
    def test_construct_minimal(self) -> None:
        bin_ = _valid_bin()
        assert bin_.lower_p == 0.0
        assert bin_.upper_p == 0.5
        assert bin_.count == 3

    def test_optional_fields_none(self) -> None:
        bin_ = _valid_bin(count=0, mean_predicted_p=None, empirical_rate=None)
        assert bin_.mean_predicted_p is None
        assert bin_.empirical_rate is None

    def test_immutability(self) -> None:
        bin_ = _valid_bin()
        with pytest.raises(dataclasses.FrozenInstanceError):
            bin_.count = 99  # type: ignore[misc]

    def test_lower_p_out_of_range_rejected(self) -> None:
        with pytest.raises(BacktestConfigError, match="lower_p"):
            _valid_bin(lower_p=-0.1)

    def test_upper_p_must_exceed_lower(self) -> None:
        with pytest.raises(BacktestConfigError, match="upper_p"):
            _valid_bin(lower_p=0.5, upper_p=0.5)

    def test_upper_p_above_one_rejected(self) -> None:
        with pytest.raises(BacktestConfigError, match="upper_p"):
            _valid_bin(lower_p=0.5, upper_p=1.5)

    def test_negative_count_rejected(self) -> None:
        with pytest.raises(BacktestConfigError, match="count"):
            _valid_bin(count=-1)

    def test_mean_predicted_p_out_of_range_rejected(self) -> None:
        with pytest.raises(BacktestConfigError, match="mean_predicted_p"):
            _valid_bin(mean_predicted_p=1.5)

    def test_empirical_rate_out_of_range_rejected(self) -> None:
        with pytest.raises(BacktestConfigError, match="empirical_rate"):
            _valid_bin(empirical_rate=-0.01)


# -- ReliabilityDiagram ----------------------------------------------------


class TestReliabilityDiagram:
    def test_construct_minimal(self) -> None:
        diagram = _valid_diagram()
        assert diagram.bin_count == 2
        assert len(diagram.bins) == 2

    def test_immutability(self) -> None:
        diagram = _valid_diagram()
        with pytest.raises(dataclasses.FrozenInstanceError):
            diagram.bin_count = 99  # type: ignore[misc]

    def test_bin_count_floor(self) -> None:
        with pytest.raises(BacktestConfigError, match="bin_count"):
            ReliabilityDiagram(bin_count=1, bins=(_valid_bin(),))

    def test_bin_count_must_match_bins_length(self) -> None:
        with pytest.raises(BacktestConfigError, match="length"):
            ReliabilityDiagram(
                bin_count=3, bins=(_valid_bin(), _valid_bin(lower_p=0.5, upper_p=1.0))
            )


# -- ScoreSummary ----------------------------------------------------------


class TestScoreSummary:
    def test_construct_minimal(self) -> None:
        summary = _valid_score_summary()
        assert summary.overall_brier == 0.21

    def test_immutability(self) -> None:
        summary = _valid_score_summary()
        with pytest.raises(dataclasses.FrozenInstanceError):
            summary.overall_brier = 0.99  # type: ignore[misc]

    def test_overall_brier_out_of_range_rejected(self) -> None:
        with pytest.raises(BacktestConfigError, match="overall_brier"):
            _valid_score_summary(overall_brier=1.5)

    def test_per_sector_brier_validated(self) -> None:
        with pytest.raises(BacktestConfigError, match="per_sector_brier"):
            _valid_score_summary(per_sector_brier={"public_health": 1.5})

    def test_per_class_brier_validated(self) -> None:
        with pytest.raises(BacktestConfigError, match="per_class_brier"):
            _valid_score_summary(per_class_brier={"flu_h2h": -0.1})

    def test_zero_resolutions_can_be_empty(self) -> None:
        summary = _valid_score_summary(
            zero_resolutions_sectors=(),
            zero_resolutions_classes=(),
        )
        assert summary.zero_resolutions_sectors == ()
        assert summary.zero_resolutions_classes == ()


# -- BacktestRun -----------------------------------------------------------


class TestBacktestRun:
    def test_construct_minimal(self) -> None:
        run = _valid_run()
        assert run.run_id == "abc123"
        assert run.status is BacktestStatus.COMPLETE

    def test_optional_fields_none(self) -> None:
        run = _valid_run(
            completed_at=None,
            error_summary=None,
            overall_brier=None,
            summary_json=None,
            status=BacktestStatus.IN_PROGRESS,
        )
        assert run.completed_at is None
        assert run.error_summary is None
        assert run.overall_brier is None
        assert run.summary_json is None

    def test_immutability(self) -> None:
        run = _valid_run()
        with pytest.raises(dataclasses.FrozenInstanceError):
            run.run_id = "other"  # type: ignore[misc]

    def test_run_id_required(self) -> None:
        with pytest.raises(BacktestConfigError, match="run_id"):
            _valid_run(run_id="")

    def test_lag_days_floor(self) -> None:
        with pytest.raises(BacktestConfigError, match="lag_days"):
            _valid_run(lag_days=0)

    def test_since_must_precede_until(self) -> None:
        with pytest.raises(BacktestConfigError, match="since_ts"):
            _valid_run(since_ts=_UNTIL, until_ts=_SINCE)

    def test_library_version_floor(self) -> None:
        with pytest.raises(BacktestConfigError, match="library_version"):
            _valid_run(library_version=0)

    def test_system_revision_required(self) -> None:
        with pytest.raises(BacktestConfigError, match="system_revision"):
            _valid_run(system_revision="")

    def test_predictions_total_non_negative(self) -> None:
        with pytest.raises(BacktestConfigError, match="predictions_total"):
            _valid_run(predictions_total=-1)

    def test_predictions_scored_non_negative(self) -> None:
        with pytest.raises(BacktestConfigError, match="predictions_scored"):
            _valid_run(predictions_total=5, predictions_scored=-1, predictions_skipped=0)

    def test_predictions_skipped_non_negative(self) -> None:
        with pytest.raises(BacktestConfigError, match="predictions_skipped"):
            _valid_run(predictions_total=5, predictions_scored=0, predictions_skipped=-1)

    def test_predictions_scored_le_total(self) -> None:
        with pytest.raises(BacktestConfigError, match="predictions_scored"):
            _valid_run(predictions_total=5, predictions_scored=10, predictions_skipped=0)

    def test_predictions_skipped_le_total(self) -> None:
        with pytest.raises(BacktestConfigError, match="predictions_skipped"):
            _valid_run(predictions_total=5, predictions_scored=0, predictions_skipped=10)

    def test_overall_brier_range_when_set(self) -> None:
        with pytest.raises(BacktestConfigError, match="overall_brier"):
            _valid_run(overall_brier=1.5)

    def test_bin_count_global_floor(self) -> None:
        with pytest.raises(BacktestConfigError, match="bin_count_global"):
            _valid_run(bin_count_global=1)

    def test_bin_count_per_sector_floor(self) -> None:
        with pytest.raises(BacktestConfigError, match="bin_count_per_sector"):
            _valid_run(bin_count_per_sector={"public_health": 1})

    def test_fallback_polarity_count_non_negative(self) -> None:
        with pytest.raises(BacktestConfigError, match="fallback_polarity_count"):
            _valid_run(fallback_polarity_count=-1)

    def test_disclaimer_version_required(self) -> None:
        with pytest.raises(BacktestConfigError, match="disclaimer_version"):
            _valid_run(disclaimer_version="")

    def test_completed_at_must_be_after_started_at(self) -> None:
        with pytest.raises(BacktestConfigError, match="completed_at"):
            _valid_run(completed_at=_STARTED - timedelta(seconds=1))

    def test_completed_at_equal_to_started_at_allowed(self) -> None:
        run = _valid_run(completed_at=_STARTED)
        assert run.completed_at == _STARTED


# -- BacktestPrediction ----------------------------------------------------


class TestBacktestPrediction:
    def test_construct_minimal(self) -> None:
        pred = _valid_prediction()
        assert pred.status is PredictionStatus.SCORED
        assert pred.skip_reason is None

    def test_optional_fields_none(self) -> None:
        pred = _valid_prediction(
            model_p=None,
            observed=None,
            polarity=None,
            brier_contribution=None,
        )
        assert pred.model_p is None
        assert pred.observed is None
        assert pred.polarity is None
        assert pred.brier_contribution is None

    def test_immutability(self) -> None:
        pred = _valid_prediction()
        with pytest.raises(dataclasses.FrozenInstanceError):
            pred.run_id = "other"  # type: ignore[misc]

    def test_run_id_required(self) -> None:
        with pytest.raises(BacktestConfigError, match="run_id"):
            _valid_prediction(run_id="")

    def test_prediction_id_required(self) -> None:
        with pytest.raises(BacktestConfigError, match="prediction_id"):
            _valid_prediction(prediction_id="")

    def test_class_id_required(self) -> None:
        with pytest.raises(BacktestConfigError, match="class_id"):
            _valid_prediction(class_id="")

    def test_condition_id_required(self) -> None:
        with pytest.raises(BacktestConfigError, match="condition_id"):
            _valid_prediction(condition_id="")

    def test_venue_required(self) -> None:
        with pytest.raises(BacktestConfigError, match="venue"):
            _valid_prediction(venue="")

    def test_sector_required(self) -> None:
        with pytest.raises(BacktestConfigError, match="sector"):
            _valid_prediction(sector="")

    def test_definition_version_floor(self) -> None:
        with pytest.raises(BacktestConfigError, match="definition_version"):
            _valid_prediction(definition_version=0)

    def test_model_p_range_when_set(self) -> None:
        with pytest.raises(BacktestConfigError, match="model_p"):
            _valid_prediction(model_p=1.5)

    def test_observed_range_when_set(self) -> None:
        with pytest.raises(BacktestConfigError, match="observed"):
            _valid_prediction(observed=-0.1)

    def test_brier_contribution_range_when_set(self) -> None:
        with pytest.raises(BacktestConfigError, match="brier_contribution"):
            _valid_prediction(brier_contribution=2.0)

    def test_skipped_requires_skip_reason(self) -> None:
        with pytest.raises(BacktestConfigError, match="skipped"):
            _valid_prediction(
                status=PredictionStatus.SKIPPED,
                skip_reason=None,
                model_p=None,
                observed=None,
                polarity=None,
                brier_contribution=None,
            )

    def test_scored_must_not_carry_skip_reason(self) -> None:
        with pytest.raises(BacktestConfigError, match="scored"):
            _valid_prediction(
                status=PredictionStatus.SCORED,
                skip_reason=SkipReason.INSUFFICIENT_LAG,
            )

    def test_skipped_with_reason_constructs(self) -> None:
        pred = _valid_prediction(
            status=PredictionStatus.SKIPPED,
            skip_reason=SkipReason.INSUFFICIENT_LAG,
            model_p=None,
            observed=None,
            polarity=None,
            brier_contribution=None,
        )
        assert pred.status is PredictionStatus.SKIPPED
        assert pred.skip_reason is SkipReason.INSUFFICIENT_LAG


# -- BacktestTrace ---------------------------------------------------------


class TestBacktestTrace:
    def test_construct_minimal(self) -> None:
        trace = _valid_trace()
        assert trace.compression_algorithm is CompressionAlgorithm.ZSTD

    def test_immutability(self) -> None:
        trace = _valid_trace()
        with pytest.raises(dataclasses.FrozenInstanceError):
            trace.run_id = "other"  # type: ignore[misc]

    def test_run_id_required(self) -> None:
        with pytest.raises(BacktestConfigError, match="run_id"):
            _valid_trace(run_id="")

    def test_prediction_id_required(self) -> None:
        with pytest.raises(BacktestConfigError, match="prediction_id"):
            _valid_trace(prediction_id="")

    def test_decompressed_size_non_negative(self) -> None:
        with pytest.raises(BacktestConfigError, match="decompressed_size_bytes"):
            _valid_trace(decompressed_size_bytes=-1)

    def test_compression_algorithm_default_is_zstd(self) -> None:
        trace = _valid_trace()
        assert trace.compression_algorithm == CompressionAlgorithm.ZSTD


# -- StrEnums --------------------------------------------------------------


class TestEnums:
    def test_backtest_status_values(self) -> None:
        assert BacktestStatus.IN_PROGRESS.value == "in_progress"
        assert BacktestStatus.COMPLETE.value == "complete"
        assert BacktestStatus.FAILED.value == "failed"

    def test_prediction_status_values(self) -> None:
        assert PredictionStatus.SCORED.value == "scored"
        assert PredictionStatus.SKIPPED.value == "skipped"

    def test_polarity_value_values(self) -> None:
        # FORWARD's string value is "direct" to match the v1
        # ``backtest_predictions.polarity`` CHECK constraint.
        assert PolarityValue.FORWARD.value == "direct"
        assert PolarityValue.INVERTED.value == "inverted"

    def test_polarity_source_values(self) -> None:
        assert PolaritySource.COMPARISON_RESOLUTIONS.value == "comparison_resolutions"
        assert PolaritySource.CURRENT_MAPPING_FALLBACK.value == "current_mapping_fallback"
        assert PolaritySource.NO_POLARITY.value == "no_polarity"

    def test_skip_reason_values(self) -> None:
        assert SkipReason.INSUFFICIENT_LAG.value == "insufficient_lag"
        assert SkipReason.SOURCE_DATA_NOT_FROZEN.value == "source_data_not_frozen"
        assert SkipReason.NO_POLARITY_RESOLUTION.value == "no_polarity_resolution"
        assert SkipReason.INVALID_RESOLUTION.value == "invalid_resolution"
        assert SkipReason.EXCEPTION.value == "exception"
        assert SkipReason.MAPPING_NOT_FOUND.value == "mapping_not_found"
        # The on-disk value is "insufficient_data" to match the v1 schema
        # CHECK constraint and design §3.13 enumeration.
        assert SkipReason.INSUFFICIENT_PRECURSOR_DATA.value == "insufficient_data"

    def test_compression_algorithm_values(self) -> None:
        assert CompressionAlgorithm.ZSTD.value == "zstd"

    @pytest.mark.parametrize(
        "enum_cls",
        [
            BacktestStatus,
            PredictionStatus,
            PolarityValue,
            PolaritySource,
            SkipReason,
            CompressionAlgorithm,
        ],
    )
    def test_enum_values_are_lowercase_strings(self, enum_cls: type[Any]) -> None:
        """All StrEnum values are lowercase (snake_case) strings."""
        for member in enum_cls:
            assert isinstance(member.value, str)
            assert member.value == member.value.lower()
            # Members are str instances by virtue of StrEnum.
            assert isinstance(member, str)


# -- public surface --------------------------------------------------------


class TestPublicSurface:
    def test_all_listed_alphabetical(self) -> None:
        public = list(models_module.__all__)
        assert public == sorted(public)

    def test_all_listed_exhaustive(self) -> None:
        expected = {
            "BacktestPrediction",
            "BacktestRun",
            "BacktestStatus",
            "BacktestTrace",
            "CompressionAlgorithm",
            "PolaritySource",
            "PolarityValue",
            "PredictionStatus",
            "ReliabilityBin",
            "ReliabilityDiagram",
            "RunParameters",
            "ScoreSummary",
            "SkipReason",
        }
        assert set(models_module.__all__) == expected

    def test_models_reexported_from_package(self) -> None:
        import razor_rooster.calibration_backtest as cb_pkg

        for name in models_module.__all__:
            assert name in cb_pkg.__all__
            assert getattr(cb_pkg, name) is getattr(models_module, name)
