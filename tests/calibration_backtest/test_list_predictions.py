"""Unit tests for :func:`list_predictions` / :func:`count_predictions` (T-CB-038).

These helpers back the GUI's per-run predictions table (Phase 6,
T-CB-038) and the surrounding "Page N of M" pagination footer. The
contract under test:

* Pagination is deterministic (``ORDER BY prediction_id ASC``).
* Optional ``status`` and ``skip_reason`` enum filters narrow the
  result set without rewriting the underlying SQL — both helpers
  share the same filter surface so a count and a page line up.
* Validation rejects negative ``limit`` / ``offset`` early via
  :class:`BacktestPersistenceError`.
* :func:`count_predictions` returns the unpaginated row count (so
  callers can render the total page count alongside a single page of
  rows fetched through :func:`list_predictions`).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import duckdb
import pytest

from razor_rooster.calibration_backtest.errors import BacktestPersistenceError
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
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
    """Yield an in-memory DuckDB connection with both migrations applied."""
    connection = duckdb.connect(":memory:")
    try:
        run_pending_calibration_backtest_migrations(connection)
        yield connection
    finally:
        connection.close()


_SINCE = datetime(2024, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2024, 6, 1, tzinfo=UTC)
_STARTED = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
_PRED_TS = datetime(2024, 1, 8, tzinfo=UTC)
_RES_TS = datetime(2024, 1, 15, tzinfo=UTC)


def _make_run(run_id: str = "abc123") -> BacktestRun:
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
        bin_count_per_sector={"public_health": 5},
        fallback_polarity_count=0,
        allow_recent=False,
        disclaimer_version="v1",
    )


def _scored(prediction_id: str, *, run_id: str = "abc123") -> BacktestPrediction:
    return BacktestPrediction(
        run_id=run_id,
        prediction_id=prediction_id,
        class_id="flu_h2h",
        condition_id="cond-1",
        venue="polymarket",
        sector="public_health",
        prediction_ts=_PRED_TS,
        resolution_ts=_RES_TS,
        model_p=0.4,
        observed=1.0,
        polarity=PolarityValue.INVERTED,
        polarity_source=PolaritySource.COMPARISON_RESOLUTIONS,
        mapping_mismatch_warning=False,
        definition_version=1,
        status=PredictionStatus.SCORED,
        skip_reason=None,
        brier_contribution=0.36,
    )


def _skipped(
    prediction_id: str,
    *,
    skip_reason: SkipReason,
    run_id: str = "abc123",
) -> BacktestPrediction:
    return BacktestPrediction(
        run_id=run_id,
        prediction_id=prediction_id,
        class_id="flu_h2h",
        condition_id="cond-1",
        venue="polymarket",
        sector="public_health",
        prediction_ts=_PRED_TS,
        resolution_ts=_RES_TS,
        model_p=None,
        observed=None,
        polarity=None,
        polarity_source=PolaritySource.CURRENT_MAPPING_FALLBACK,
        mapping_mismatch_warning=True,
        definition_version=1,
        status=PredictionStatus.SKIPPED,
        skip_reason=skip_reason,
        brier_contribution=None,
    )


def _seed(conn: duckdb.DuckDBPyConnection) -> list[BacktestPrediction]:
    """Seed a deterministic mix of scored + skipped predictions.

    Returns the full set sorted by ``prediction_id`` so tests can
    cross-check pagination slices against a known ordering.
    """
    operations.insert_run(conn, _make_run())
    predictions: list[BacktestPrediction] = [
        _scored("pred-001"),
        _scored("pred-002"),
        _scored("pred-003"),
        _scored("pred-004"),
        _skipped("pred-005", skip_reason=SkipReason.INVALID_RESOLUTION),
        _skipped("pred-006", skip_reason=SkipReason.INVALID_RESOLUTION),
        _skipped("pred-007", skip_reason=SkipReason.MAPPING_NOT_FOUND),
    ]
    operations.insert_predictions_batch(conn, predictions)
    return sorted(predictions, key=lambda p: p.prediction_id)


# ---------------------------------------------------------------------------
# list_predictions: basic + pagination + ordering
# ---------------------------------------------------------------------------


def test_list_predictions_basic_returns_all_for_run(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    seeded = _seed(conn)
    rows = operations.list_predictions(conn, "abc123", limit=100)
    assert [p.prediction_id for p in rows] == [p.prediction_id for p in seeded]


def test_list_predictions_orders_by_prediction_id_ascending(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    operations.insert_run(conn, _make_run())
    operations.insert_prediction(conn, _scored("pred-c"))
    operations.insert_prediction(conn, _scored("pred-a"))
    operations.insert_prediction(conn, _scored("pred-b"))

    rows = operations.list_predictions(conn, "abc123", limit=10)
    assert [p.prediction_id for p in rows] == ["pred-a", "pred-b", "pred-c"]


def test_list_predictions_pagination_boundary(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    seeded = _seed(conn)
    page_size = 3

    page0 = operations.list_predictions(conn, "abc123", limit=page_size, offset=0)
    page1 = operations.list_predictions(conn, "abc123", limit=page_size, offset=page_size)
    page2 = operations.list_predictions(conn, "abc123", limit=page_size, offset=page_size * 2)
    page_past_end = operations.list_predictions(
        conn, "abc123", limit=page_size, offset=page_size * 3
    )

    assert [p.prediction_id for p in page0] == [p.prediction_id for p in seeded[0:3]]
    assert [p.prediction_id for p in page1] == [p.prediction_id for p in seeded[3:6]]
    assert [p.prediction_id for p in page2] == [p.prediction_id for p in seeded[6:7]]
    assert page_past_end == ()


def test_list_predictions_limit_zero_returns_empty(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)
    assert operations.list_predictions(conn, "abc123", limit=0) == ()


def test_list_predictions_for_missing_run_returns_empty(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    assert operations.list_predictions(conn, "no-such-run") == ()


# ---------------------------------------------------------------------------
# list_predictions: filters
# ---------------------------------------------------------------------------


def test_list_predictions_status_filter_scored(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)
    rows = operations.list_predictions(conn, "abc123", status=PredictionStatus.SCORED, limit=100)
    assert [p.prediction_id for p in rows] == [
        "pred-001",
        "pred-002",
        "pred-003",
        "pred-004",
    ]
    assert all(p.status is PredictionStatus.SCORED for p in rows)


def test_list_predictions_status_filter_skipped(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)
    rows = operations.list_predictions(conn, "abc123", status=PredictionStatus.SKIPPED, limit=100)
    assert [p.prediction_id for p in rows] == ["pred-005", "pred-006", "pred-007"]
    assert all(p.status is PredictionStatus.SKIPPED for p in rows)


def test_list_predictions_skip_reason_filter(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)
    rows = operations.list_predictions(
        conn,
        "abc123",
        skip_reason=SkipReason.INVALID_RESOLUTION,
        limit=100,
    )
    assert [p.prediction_id for p in rows] == ["pred-005", "pred-006"]
    assert all(p.skip_reason is SkipReason.INVALID_RESOLUTION for p in rows)


def test_list_predictions_combined_status_and_skip_reason(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)
    rows = operations.list_predictions(
        conn,
        "abc123",
        status=PredictionStatus.SKIPPED,
        skip_reason=SkipReason.MAPPING_NOT_FOUND,
        limit=100,
    )
    assert [p.prediction_id for p in rows] == ["pred-007"]


def test_list_predictions_combined_filter_with_no_match_returns_empty(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)
    # SCORED predictions never carry a skip_reason, so this combination
    # is structurally empty even though both individual filters match
    # rows in isolation.
    rows = operations.list_predictions(
        conn,
        "abc123",
        status=PredictionStatus.SCORED,
        skip_reason=SkipReason.INVALID_RESOLUTION,
        limit=100,
    )
    assert rows == ()


def test_list_predictions_only_returns_rows_for_requested_run(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)
    operations.insert_run(conn, _make_run(run_id="other"))
    operations.insert_prediction(conn, _scored("pred-001", run_id="other"))

    rows = operations.list_predictions(conn, "other", limit=100)
    assert [p.run_id for p in rows] == ["other"]
    assert [p.prediction_id for p in rows] == ["pred-001"]


# ---------------------------------------------------------------------------
# list_predictions: validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_limit", [-1, -10])
def test_list_predictions_rejects_negative_limit(
    conn: duckdb.DuckDBPyConnection, bad_limit: int
) -> None:
    with pytest.raises(BacktestPersistenceError, match="limit must be >= 0"):
        operations.list_predictions(conn, "abc123", limit=bad_limit)


@pytest.mark.parametrize("bad_offset", [-1, -100])
def test_list_predictions_rejects_negative_offset(
    conn: duckdb.DuckDBPyConnection, bad_offset: int
) -> None:
    with pytest.raises(BacktestPersistenceError, match="offset must be >= 0"):
        operations.list_predictions(conn, "abc123", offset=bad_offset)


# ---------------------------------------------------------------------------
# count_predictions
# ---------------------------------------------------------------------------


def test_count_predictions_matches_unpaginated_total(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    seeded = _seed(conn)
    assert operations.count_predictions(conn, "abc123") == len(seeded)


def test_count_predictions_matches_filtered_list_length(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed(conn)

    expectations: list[tuple[dict[str, Any], int]] = [
        ({"status": PredictionStatus.SCORED}, 4),
        ({"status": PredictionStatus.SKIPPED}, 3),
        ({"skip_reason": SkipReason.INVALID_RESOLUTION}, 2),
        ({"skip_reason": SkipReason.MAPPING_NOT_FOUND}, 1),
        (
            {
                "status": PredictionStatus.SKIPPED,
                "skip_reason": SkipReason.MAPPING_NOT_FOUND,
            },
            1,
        ),
        (
            {
                "status": PredictionStatus.SCORED,
                "skip_reason": SkipReason.INVALID_RESOLUTION,
            },
            0,
        ),
    ]
    for filters, expected in expectations:
        listed = operations.list_predictions(conn, "abc123", limit=100, **filters)
        counted = operations.count_predictions(conn, "abc123", **filters)
        assert counted == expected, filters
        assert counted == len(listed), filters


def test_count_predictions_for_missing_run_returns_zero(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    assert operations.count_predictions(conn, "no-such-run") == 0


def test_count_predictions_independent_of_pagination(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    seeded = _seed(conn)
    # Fetching a single page must not affect the total.
    page = operations.list_predictions(conn, "abc123", limit=2, offset=2)
    assert len(page) == 2
    assert operations.count_predictions(conn, "abc123") == len(seeded)
