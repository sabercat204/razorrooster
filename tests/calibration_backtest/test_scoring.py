"""Unit tests for calibration_backtest Brier arithmetic (T-CB-021).

Covers:

* :func:`compute_brier_overall` — hand-computed reference within ``1e-9``;
  scored / skipped filtering; explicit ``0.0`` on the empty-input path.
* :func:`compute_brier_per_sector` /
  :func:`compute_brier_per_class` — DuckDB-side
  ``AVG(brier_contribution)`` aggregation matching the canonical
  aggregation locked by the T-CB-023 amendment.
* :func:`detect_zero_resolution_groups` — sectors / classes that appear in
  ``backtest_predictions`` for the run but contribute zero rows to the
  scored aggregation surface in the returned tuples.

Each test acquires a fresh in-memory DuckDB connection via the ``conn``
fixture; the migration runner applies the calibration_backtest migrations
so the connection carries the canonical schema before any tests run.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import duckdb
import pytest

from razor_rooster.calibration_backtest.engines.scoring import (
    compute_brier_overall,
    compute_brier_per_class,
    compute_brier_per_sector,
    detect_zero_resolution_groups,
)
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
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


_PRED_TS = datetime(2024, 1, 8, tzinfo=UTC)
_RES_TS = datetime(2024, 1, 15, tzinfo=UTC)


def _make_prediction(**overrides: Any) -> BacktestPrediction:
    """Construct a canonical scored :class:`BacktestPrediction` with optional overrides."""
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


# ---------------------------------------------------------------------------
# compute_brier_overall
# ---------------------------------------------------------------------------


def test_compute_brier_overall_hand_computed_three_predictions() -> None:
    # (model_p, observed) -> brier_contribution = (model_p - observed) ** 2
    # (0.10, 0.0) -> 0.01
    # (0.50, 1.0) -> 0.25
    # (0.80, 1.0) -> 0.04
    # mean = (0.01 + 0.25 + 0.04) / 3 = 0.30 / 3 = 0.10
    predictions = [
        _make_prediction(prediction_id="p-1", model_p=0.10, observed=0.0, brier_contribution=0.01),
        _make_prediction(prediction_id="p-2", model_p=0.50, observed=1.0, brier_contribution=0.25),
        _make_prediction(prediction_id="p-3", model_p=0.80, observed=1.0, brier_contribution=0.04),
    ]
    assert compute_brier_overall(predictions) == pytest.approx(0.10, abs=1e-9)


def test_compute_brier_overall_empty_returns_zero() -> None:
    assert compute_brier_overall([]) == 0.0


def test_compute_brier_overall_skipped_predictions_excluded() -> None:
    predictions = [
        _make_prediction(prediction_id="p-1", brier_contribution=0.36),
        _make_prediction(
            prediction_id="p-2",
            status=PredictionStatus.SKIPPED,
            skip_reason=SkipReason.NO_POLARITY_RESOLUTION,
            model_p=None,
            observed=None,
            polarity=None,
            polarity_source=PolaritySource.NO_POLARITY,
            brier_contribution=None,
        ),
    ]
    # Only the scored prediction (0.36) contributes; mean of one value is itself.
    assert compute_brier_overall(predictions) == pytest.approx(0.36, abs=1e-9)


def test_compute_brier_overall_all_skipped_returns_zero() -> None:
    predictions = [
        _make_prediction(
            prediction_id="p-1",
            status=PredictionStatus.SKIPPED,
            skip_reason=SkipReason.INSUFFICIENT_LAG,
            model_p=None,
            observed=None,
            polarity=None,
            polarity_source=PolaritySource.NO_POLARITY,
            brier_contribution=None,
        ),
    ]
    assert compute_brier_overall(predictions) == 0.0


# ---------------------------------------------------------------------------
# compute_brier_per_sector / compute_brier_per_class
# ---------------------------------------------------------------------------


def _seed_predictions(
    conn: duckdb.DuckDBPyConnection, predictions: list[BacktestPrediction]
) -> None:
    operations.insert_predictions_batch(conn, predictions)


def test_compute_brier_per_sector_matches_avg(conn: duckdb.DuckDBPyConnection) -> None:
    # Two sectors, multiple predictions each, plus a skipped row that must be ignored.
    predictions = [
        _make_prediction(prediction_id="p-1", sector="public_health", brier_contribution=0.10),
        _make_prediction(prediction_id="p-2", sector="public_health", brier_contribution=0.20),
        _make_prediction(prediction_id="p-3", sector="economics", brier_contribution=0.40),
        _make_prediction(prediction_id="p-4", sector="economics", brier_contribution=0.60),
        _make_prediction(
            prediction_id="p-5",
            sector="economics",
            status=PredictionStatus.SKIPPED,
            skip_reason=SkipReason.NO_POLARITY_RESOLUTION,
            model_p=None,
            observed=None,
            polarity=None,
            polarity_source=PolaritySource.NO_POLARITY,
            brier_contribution=None,
        ),
    ]
    _seed_predictions(conn, predictions)
    result = compute_brier_per_sector(conn, "run-a")
    # AVG(0.10, 0.20) = 0.15; AVG(0.40, 0.60) = 0.50; skipped row contributes nothing.
    assert result == {
        "public_health": pytest.approx(0.15, abs=1e-9),
        "economics": pytest.approx(0.50, abs=1e-9),
    }


def test_compute_brier_per_sector_empty_run(conn: duckdb.DuckDBPyConnection) -> None:
    assert compute_brier_per_sector(conn, "missing") == {}


def test_compute_brier_per_sector_isolates_run(conn: duckdb.DuckDBPyConnection) -> None:
    # Predictions on a sibling run must not bleed into the requested run's roll-up.
    predictions = [
        _make_prediction(
            prediction_id="p-1",
            run_id="run-a",
            sector="public_health",
            brier_contribution=0.10,
        ),
        _make_prediction(
            prediction_id="p-2",
            run_id="run-b",
            sector="public_health",
            brier_contribution=0.90,
        ),
    ]
    _seed_predictions(conn, predictions)
    assert compute_brier_per_sector(conn, "run-a") == {
        "public_health": pytest.approx(0.10, abs=1e-9)
    }
    assert compute_brier_per_sector(conn, "run-b") == {
        "public_health": pytest.approx(0.90, abs=1e-9)
    }


def test_compute_brier_per_class_matches_avg(conn: duckdb.DuckDBPyConnection) -> None:
    predictions = [
        _make_prediction(prediction_id="p-1", class_id="flu_h2h", brier_contribution=0.20),
        _make_prediction(prediction_id="p-2", class_id="flu_h2h", brier_contribution=0.30),
        _make_prediction(prediction_id="p-3", class_id="rate_hike", brier_contribution=0.05),
        _make_prediction(
            prediction_id="p-4",
            class_id="rate_hike",
            status=PredictionStatus.SKIPPED,
            skip_reason=SkipReason.MAPPING_NOT_FOUND,
            model_p=None,
            observed=None,
            polarity=None,
            polarity_source=PolaritySource.NO_POLARITY,
            brier_contribution=None,
        ),
    ]
    _seed_predictions(conn, predictions)
    result = compute_brier_per_class(conn, "run-a")
    assert result == {
        "flu_h2h": pytest.approx(0.25, abs=1e-9),
        "rate_hike": pytest.approx(0.05, abs=1e-9),
    }


def test_compute_brier_per_class_empty_run(conn: duckdb.DuckDBPyConnection) -> None:
    assert compute_brier_per_class(conn, "missing") == {}


# ---------------------------------------------------------------------------
# detect_zero_resolution_groups
# ---------------------------------------------------------------------------


def test_detect_zero_resolution_groups_flags_sectors_and_classes(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    # public_health has at least one scored row.
    # economics has only a skipped row -> zero scored predictions.
    # rate_hike has only a skipped row -> zero scored predictions.
    predictions = [
        _make_prediction(
            prediction_id="p-1",
            sector="public_health",
            class_id="flu_h2h",
            brier_contribution=0.10,
        ),
        _make_prediction(
            prediction_id="p-2",
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
    ]
    _seed_predictions(conn, predictions)
    zero_sectors, zero_classes = detect_zero_resolution_groups(conn, "run-a")
    assert zero_sectors == ("economics",)
    assert zero_classes == ("rate_hike",)


def test_detect_zero_resolution_groups_all_scored_returns_empty_tuples(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    predictions = [
        _make_prediction(
            prediction_id="p-1",
            sector="public_health",
            class_id="flu_h2h",
            brier_contribution=0.10,
        ),
    ]
    _seed_predictions(conn, predictions)
    zero_sectors, zero_classes = detect_zero_resolution_groups(conn, "run-a")
    assert zero_sectors == ()
    assert zero_classes == ()


def test_detect_zero_resolution_groups_empty_run(conn: duckdb.DuckDBPyConnection) -> None:
    zero_sectors, zero_classes = detect_zero_resolution_groups(conn, "missing")
    assert zero_sectors == ()
    assert zero_classes == ()


def test_detect_zero_resolution_groups_sorted_output(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    # Use sectors / classes whose alphabetical ordering differs from their
    # insertion order so we can lock determinism on the output tuples.
    predictions = [
        _make_prediction(
            prediction_id="p-1",
            sector="zeta",
            class_id="zulu",
            status=PredictionStatus.SKIPPED,
            skip_reason=SkipReason.NO_POLARITY_RESOLUTION,
            model_p=None,
            observed=None,
            polarity=None,
            polarity_source=PolaritySource.NO_POLARITY,
            brier_contribution=None,
        ),
        _make_prediction(
            prediction_id="p-2",
            sector="alpha",
            class_id="alfa",
            status=PredictionStatus.SKIPPED,
            skip_reason=SkipReason.MAPPING_NOT_FOUND,
            model_p=None,
            observed=None,
            polarity=None,
            polarity_source=PolaritySource.NO_POLARITY,
            brier_contribution=None,
        ),
    ]
    _seed_predictions(conn, predictions)
    zero_sectors, zero_classes = detect_zero_resolution_groups(conn, "run-a")
    assert zero_sectors == ("alpha", "zeta")
    assert zero_classes == ("alfa", "zulu")
