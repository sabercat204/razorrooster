"""Unit and integration tests for T-CB-023 ScoreSummary aggregation.

Covers the deliverables locked by T-CB-023 (design §3.6, REQ-CB-SCORE-001
through REQ-CB-SCORE-004):

* :meth:`ScoreSummary.to_json` round-trip — serialise then re-parse
  yields the original structure.
* :meth:`ScoreSummary.to_json` byte-determinism — identical inputs
  produce byte-identical output, locking the canonical
  ``json.dumps(sort_keys=True)`` encoding the persistence layer relies
  on.
* ``zero_resolutions_*`` propagation when sectors / classes contribute
  zero scored predictions.
* :func:`aggregate_run_summary` integration — synthetic run with mixed
  scored / skipped / fallback-polarity rows produces the expected
  overall Brier, per-sector / per-class Brier, reliability diagrams,
  fallback counters, and rate.
* :func:`persist_score_summary` round-trip — the
  :class:`ScoreSummary` payload survives the persistence boundary and
  the ``backtest_runs.summary_json`` column carries the canonical
  encoding.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import duckdb
import pytest

from razor_rooster.calibration_backtest.engines.scoring import aggregate_run_summary
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
    ReliabilityBin,
    ReliabilityDiagram,
    ScoreSummary,
    SkipReason,
)
from razor_rooster.calibration_backtest.persistence import operations
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield an in-memory DuckDB connection with calibration_backtest migrations applied."""
    connection = duckdb.connect(":memory:")
    try:
        run_pending_calibration_backtest_migrations(connection)
        yield connection
    finally:
        connection.close()


_SINCE = datetime(2024, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2024, 6, 1, tzinfo=UTC)
_STARTED = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
_COMPLETED = datetime(2024, 6, 1, 0, 5, 0, tzinfo=UTC)
_PRED_TS = datetime(2024, 1, 8, tzinfo=UTC)
_RES_TS = datetime(2024, 1, 15, tzinfo=UTC)


def _make_bin(
    *,
    lower_p: float,
    upper_p: float,
    count: int = 0,
    mean_predicted_p: float | None = None,
    empirical_rate: float | None = None,
) -> ReliabilityBin:
    return ReliabilityBin(
        lower_p=lower_p,
        upper_p=upper_p,
        count=count,
        mean_predicted_p=mean_predicted_p,
        empirical_rate=empirical_rate,
    )


def _make_diagram() -> ReliabilityDiagram:
    return ReliabilityDiagram(
        bin_count=2,
        bins=(
            _make_bin(lower_p=0.0, upper_p=0.5, count=2, mean_predicted_p=0.3, empirical_rate=0.5),
            _make_bin(lower_p=0.5, upper_p=1.0, count=1, mean_predicted_p=0.8, empirical_rate=1.0),
        ),
    )


def _make_summary(**overrides: Any) -> ScoreSummary:
    base: dict[str, Any] = {
        "overall_brier": 0.21,
        "per_sector_brier": {"public_health": 0.18, "economics": 0.30},
        "per_class_brier": {"flu_h2h": 0.22, "rate_hike": 0.10},
        "reliability_diagrams": {"public_health": _make_diagram()},
        "zero_resolutions_sectors": (),
        "zero_resolutions_classes": (),
        "fallback_polarity_count": 0,
        "fallback_polarity_rate": 0.0,
    }
    base.update(overrides)
    return ScoreSummary(**base)


def _make_prediction(**overrides: Any) -> BacktestPrediction:
    base: dict[str, Any] = {
        "run_id": "run-a",
        "prediction_id": "pred-001",
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


def _make_run(run_id: str = "run-a") -> BacktestRun:
    return BacktestRun(
        run_id=run_id,
        since_ts=_SINCE,
        until_ts=_UNTIL,
        lag_days=7,
        class_ids=("flu_h2h",),
        sectors=("public_health",),
        venues=("polymarket",),
        library_version=1,
        system_revision="deadbeef",
        started_at=_STARTED,
        completed_at=None,
        status=BacktestStatus.IN_PROGRESS,
        error_summary=None,
        predictions_total=0,
        predictions_scored=0,
        predictions_skipped=0,
        overall_brier=None,
        summary_json=None,
        bin_count_global=10,
        bin_count_per_sector={},
        fallback_polarity_count=0,
        allow_recent=False,
        disclaimer_version="v1",
    )


# ---------------------------------------------------------------------------
# ScoreSummary.to_json — round-trip + determinism
# ---------------------------------------------------------------------------


def test_to_json_round_trip_preserves_structure() -> None:
    summary = _make_summary(
        zero_resolutions_sectors=("economics",),
        zero_resolutions_classes=("rate_hike",),
        fallback_polarity_count=2,
        fallback_polarity_rate=0.25,
    )
    payload = json.loads(summary.to_json())

    assert payload["overall_brier"] == pytest.approx(0.21)
    assert payload["per_sector_brier"] == {"economics": 0.30, "public_health": 0.18}
    assert payload["per_class_brier"] == {"flu_h2h": 0.22, "rate_hike": 0.10}
    assert payload["zero_resolutions_sectors"] == ["economics"]
    assert payload["zero_resolutions_classes"] == ["rate_hike"]
    assert payload["fallback_polarity_count"] == 2
    assert payload["fallback_polarity_rate"] == pytest.approx(0.25)
    diagram = payload["reliability_diagrams"]["public_health"]
    assert diagram["bin_count"] == 2
    assert len(diagram["bins"]) == 2
    assert diagram["bins"][0]["lower_p"] == 0.0
    assert diagram["bins"][0]["upper_p"] == 0.5
    assert diagram["bins"][0]["count"] == 2
    assert diagram["bins"][1]["lower_p"] == 0.5


def test_to_json_is_byte_deterministic_for_identical_inputs() -> None:
    a = _make_summary()
    # Construct an equivalent summary with mappings populated in the
    # opposite insertion order — ``sort_keys=True`` plus the canonical
    # encoder must collapse them to the same byte sequence.
    b = ScoreSummary(
        overall_brier=0.21,
        per_sector_brier={"economics": 0.30, "public_health": 0.18},
        per_class_brier={"rate_hike": 0.10, "flu_h2h": 0.22},
        reliability_diagrams={"public_health": _make_diagram()},
        zero_resolutions_sectors=(),
        zero_resolutions_classes=(),
        fallback_polarity_count=0,
        fallback_polarity_rate=0.0,
    )
    assert a.to_json() == b.to_json()
    assert a.to_json().encode("utf-8") == b.to_json().encode("utf-8")


def test_to_json_uses_sort_keys() -> None:
    # Lock the canonical key order: every top-level key sorted ASCII.
    summary = _make_summary()
    payload = summary.to_json()
    keys_in_order = [
        "fallback_polarity_count",
        "fallback_polarity_rate",
        "overall_brier",
        "per_class_brier",
        "per_sector_brier",
        "reliability_diagrams",
        "zero_resolutions_classes",
        "zero_resolutions_sectors",
    ]
    last_index = -1
    for key in keys_in_order:
        idx = payload.find(f'"{key}"')
        assert idx > last_index, f"key {key!r} appeared out of sorted order"
        last_index = idx


def test_zero_resolutions_emit_sorted_lists() -> None:
    summary = _make_summary(
        zero_resolutions_sectors=("zeta", "alpha"),
        zero_resolutions_classes=("zulu", "alfa"),
    )
    payload = json.loads(summary.to_json())
    assert payload["zero_resolutions_sectors"] == ["alpha", "zeta"]
    assert payload["zero_resolutions_classes"] == ["alfa", "zulu"]


# ---------------------------------------------------------------------------
# ScoreSummary validators on the new fields
# ---------------------------------------------------------------------------


def test_fallback_polarity_count_must_be_non_negative() -> None:
    with pytest.raises(Exception, match="fallback_polarity_count"):
        _make_summary(fallback_polarity_count=-1)


def test_fallback_polarity_rate_range() -> None:
    with pytest.raises(Exception, match="fallback_polarity_rate"):
        _make_summary(fallback_polarity_rate=1.5)


# ---------------------------------------------------------------------------
# aggregate_run_summary integration
# ---------------------------------------------------------------------------


def test_aggregate_run_summary_mixed_predictions(conn: duckdb.DuckDBPyConnection) -> None:
    # Seed a run with:
    #   - public_health / flu_h2h: 2 scored rows (one fallback polarity).
    #   - economics / rate_hike: 1 scored row + 1 skipped row.
    #   - climate / heatwave: 1 skipped row only -> zero scored, surfaces in
    #     zero_resolutions_sectors and zero_resolutions_classes.
    operations.insert_run(conn, _make_run("run-a"))
    predictions = [
        _make_prediction(
            prediction_id="p-1",
            sector="public_health",
            class_id="flu_h2h",
            model_p=0.4,
            observed=1.0,
            brier_contribution=0.36,
            polarity_source=PolaritySource.COMPARISON_RESOLUTIONS,
        ),
        _make_prediction(
            prediction_id="p-2",
            sector="public_health",
            class_id="flu_h2h",
            model_p=0.6,
            observed=1.0,
            brier_contribution=0.16,
            polarity_source=PolaritySource.CURRENT_MAPPING_FALLBACK,
            mapping_mismatch_warning=True,
        ),
        _make_prediction(
            prediction_id="p-3",
            sector="economics",
            class_id="rate_hike",
            model_p=0.2,
            observed=0.0,
            brier_contribution=0.04,
            polarity_source=PolaritySource.COMPARISON_RESOLUTIONS,
        ),
        _make_prediction(
            prediction_id="p-4",
            sector="economics",
            class_id="rate_hike",
            status=PredictionStatus.SKIPPED,
            skip_reason=SkipReason.NO_POLARITY_RESOLUTION,
            model_p=None,
            observed=None,
            polarity=None,
            polarity_source=PolaritySource.NO_POLARITY,
            brier_contribution=None,
        ),
        _make_prediction(
            prediction_id="p-5",
            sector="climate",
            class_id="heatwave",
            status=PredictionStatus.SKIPPED,
            skip_reason=SkipReason.MAPPING_NOT_FOUND,
            model_p=None,
            observed=None,
            polarity=None,
            polarity_source=PolaritySource.NO_POLARITY,
            brier_contribution=None,
        ),
    ]
    operations.insert_predictions_batch(conn, predictions)

    summary = aggregate_run_summary(
        conn,
        "run-a",
        bin_count_global=10,
        bin_count_per_sector={},
    )

    # Overall = AVG(0.36, 0.16, 0.04) = 0.18666...
    assert summary.overall_brier == pytest.approx((0.36 + 0.16 + 0.04) / 3, abs=1e-9)
    # public_health = AVG(0.36, 0.16) = 0.26
    # economics = AVG(0.04) = 0.04
    assert summary.per_sector_brier == {
        "public_health": pytest.approx(0.26, abs=1e-9),
        "economics": pytest.approx(0.04, abs=1e-9),
    }
    # flu_h2h = 0.26; rate_hike = 0.04
    assert summary.per_class_brier == {
        "flu_h2h": pytest.approx(0.26, abs=1e-9),
        "rate_hike": pytest.approx(0.04, abs=1e-9),
    }
    # climate / heatwave appear only as skipped rows -> zero scored.
    assert summary.zero_resolutions_sectors == ("climate",)
    assert summary.zero_resolutions_classes == ("heatwave",)
    # Per-sector reliability diagrams: only sectors with scored rows.
    assert set(summary.reliability_diagrams) == {"public_health", "economics"}
    assert summary.reliability_diagrams["public_health"].bin_count == 10
    assert summary.reliability_diagrams["economics"].bin_count == 10
    # 1 fallback / 3 scored.
    assert summary.fallback_polarity_count == 1
    assert summary.fallback_polarity_rate == pytest.approx(1 / 3, abs=1e-9)


def test_aggregate_run_summary_empty_run(conn: duckdb.DuckDBPyConnection) -> None:
    operations.insert_run(conn, _make_run("run-empty"))
    summary = aggregate_run_summary(
        conn,
        "run-empty",
        bin_count_global=10,
        bin_count_per_sector={},
    )
    assert summary.overall_brier == 0.0
    assert summary.per_sector_brier == {}
    assert summary.per_class_brier == {}
    assert summary.zero_resolutions_sectors == ()
    assert summary.zero_resolutions_classes == ()
    assert summary.reliability_diagrams == {}
    assert summary.fallback_polarity_count == 0
    assert summary.fallback_polarity_rate == 0.0


def test_aggregate_run_summary_isolates_run(conn: duckdb.DuckDBPyConnection) -> None:
    operations.insert_run(conn, _make_run("run-a"))
    operations.insert_run(conn, _make_run("run-b"))
    operations.insert_predictions_batch(
        conn,
        [
            _make_prediction(
                run_id="run-a",
                prediction_id="p-a1",
                sector="public_health",
                class_id="flu_h2h",
                model_p=0.4,
                observed=1.0,
                brier_contribution=0.36,
            ),
            _make_prediction(
                run_id="run-b",
                prediction_id="p-b1",
                sector="economics",
                class_id="rate_hike",
                model_p=0.9,
                observed=0.0,
                brier_contribution=0.81,
            ),
        ],
    )
    summary_a = aggregate_run_summary(
        conn,
        "run-a",
        bin_count_global=10,
        bin_count_per_sector={},
    )
    summary_b = aggregate_run_summary(
        conn,
        "run-b",
        bin_count_global=10,
        bin_count_per_sector={},
    )
    assert summary_a.per_sector_brier == {"public_health": pytest.approx(0.36, abs=1e-9)}
    assert summary_b.per_sector_brier == {"economics": pytest.approx(0.81, abs=1e-9)}


def test_aggregate_run_summary_per_sector_bin_overrides(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    operations.insert_run(conn, _make_run("run-a"))
    operations.insert_predictions_batch(
        conn,
        [
            _make_prediction(
                prediction_id="p-1",
                sector="public_health",
                class_id="flu_h2h",
                model_p=0.4,
                observed=1.0,
                brier_contribution=0.36,
            ),
            _make_prediction(
                prediction_id="p-2",
                sector="economics",
                class_id="rate_hike",
                model_p=0.2,
                observed=0.0,
                brier_contribution=0.04,
            ),
        ],
    )
    summary = aggregate_run_summary(
        conn,
        "run-a",
        bin_count_global=10,
        bin_count_per_sector={"economics": 5},
    )
    assert summary.reliability_diagrams["public_health"].bin_count == 10
    assert summary.reliability_diagrams["economics"].bin_count == 5


# ---------------------------------------------------------------------------
# persist_score_summary integration
# ---------------------------------------------------------------------------


def test_persist_score_summary_writes_summary_json(conn: duckdb.DuckDBPyConnection) -> None:
    operations.insert_run(conn, _make_run("run-a"))
    summary = _make_summary(
        overall_brier=0.21,
        fallback_polarity_count=2,
        fallback_polarity_rate=0.5,
    )
    operations.persist_score_summary(
        conn,
        "run-a",
        summary,
        completed_at=_COMPLETED,
        predictions_total=4,
        predictions_scored=4,
        predictions_skipped=0,
    )
    fetched = operations.fetch_run(conn, "run-a")
    assert fetched is not None
    assert fetched.status is BacktestStatus.COMPLETE
    assert fetched.completed_at == _COMPLETED
    assert fetched.overall_brier == pytest.approx(0.21)
    assert fetched.fallback_polarity_count == 2
    assert fetched.summary_json is not None
    # The persistence layer re-encodes via _dumps_canonical (also
    # sort_keys=True), so the on-disk payload mirrors ScoreSummary.to_json
    # field-for-field — the round-trip preserves every key.
    assert fetched.summary_json["overall_brier"] == pytest.approx(0.21)
    assert fetched.summary_json["fallback_polarity_count"] == 2
    assert fetched.summary_json["fallback_polarity_rate"] == pytest.approx(0.5)
    assert "reliability_diagrams" in fetched.summary_json
    assert "per_sector_brier" in fetched.summary_json
    assert "per_class_brier" in fetched.summary_json
