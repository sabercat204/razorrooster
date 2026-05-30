"""Unit tests for the calibration-backtest disk-budget guard (T-CB-012).

Covers REQ-CB-PERSIST-003: the projection helper, the live footprint
reader, the cap-enforcement assert, and the convenience wrapper that
returns all four numbers in one dict. Each test acquires an in-memory
DuckDB connection with both calibration-backtest migrations applied —
identical to the fixture used by ``test_persistence_operations`` (T-CB-010).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import duckdb
import pytest

from razor_rooster.calibration_backtest.errors import DiskBudgetError
from razor_rooster.calibration_backtest.models import (
    BacktestRun,
    BacktestStatus,
)
from razor_rooster.calibration_backtest.persistence import operations
from razor_rooster.calibration_backtest.persistence.disk_budget import (
    DEFAULT_DISK_CAP_MB,
    assert_under_cap,
    estimate_run_footprint_mb,
    measure_current_footprint_mb,
    projected_disk_usage_mb,
)
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """In-memory DuckDB connection with m6001 + m6002 applied."""
    connection = duckdb.connect(":memory:")
    try:
        run_pending_calibration_backtest_migrations(connection)
        yield connection
    finally:
        connection.close()


_SINCE = datetime(2024, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2024, 6, 1, tzinfo=UTC)
_STARTED = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)


def _make_run(run_id: str = "abc123") -> BacktestRun:
    """Minimal :class:`BacktestRun` factory matching the schema CHECK constraints."""
    kwargs: dict[str, Any] = {
        "run_id": run_id,
        "since_ts": _SINCE,
        "until_ts": _UNTIL,
        "lag_days": 7,
        "class_ids": ("flu_h2h",),
        "sectors": ("public_health",),
        "venues": ("polymarket",),
        "library_version": 1,
        "system_revision": "deadbeef",
        "started_at": _STARTED,
        "completed_at": None,
        "status": BacktestStatus.IN_PROGRESS,
        "error_summary": None,
        "predictions_total": 0,
        "predictions_scored": 0,
        "predictions_skipped": 0,
        "overall_brier": None,
        "summary_json": None,
        "bin_count_global": 10,
        "bin_count_per_sector": {"public_health": 5},
        "fallback_polarity_count": 0,
        "allow_recent": False,
        "disclaimer_version": "v1",
    }
    return BacktestRun(**kwargs)


# ---------------------------------------------------------------------------
# estimate_run_footprint_mb
# ---------------------------------------------------------------------------


def test_estimate_run_footprint_zero_predictions_returns_base_only() -> None:
    base = 4096.0
    result = estimate_run_footprint_mb(
        predictions_estimated=0,
        base_run_row_bytes=base,
    )
    expected_mb = base / (1024.0 * 1024.0)
    assert result == pytest.approx(expected_mb)


def test_estimate_run_footprint_scales_linearly() -> None:
    base = 1024.0
    one_k = estimate_run_footprint_mb(
        predictions_estimated=1000,
        base_run_row_bytes=base,
    )
    two_k = estimate_run_footprint_mb(
        predictions_estimated=2000,
        base_run_row_bytes=base,
    )
    additional_one_k = one_k - (base / (1024.0 * 1024.0))
    additional_two_k = two_k - (base / (1024.0 * 1024.0))
    # 2000 predictions should add ~2x the per-prediction cost of 1000.
    assert additional_two_k == pytest.approx(2.0 * additional_one_k)


def test_estimate_run_footprint_uses_compression_assumption() -> None:
    """Doubling avg_trace_bytes only adds ~10% post-compression contribution."""
    small_trace = estimate_run_footprint_mb(
        predictions_estimated=1000,
        avg_trace_bytes=1000.0,
        avg_prediction_row_bytes=0.0,
        base_run_row_bytes=0.0,
    )
    large_trace = estimate_run_footprint_mb(
        predictions_estimated=1000,
        avg_trace_bytes=10000.0,
        avg_prediction_row_bytes=0.0,
        base_run_row_bytes=0.0,
    )
    # Each prediction's trace contributes avg_trace_bytes * 0.10:
    # 1000 preds * 1000 B * 0.10 = 100_000 B -> 100_000 / (1024*1024) MB
    expected_small = (1000 * 1000.0 * 0.10) / (1024.0 * 1024.0)
    expected_large = (1000 * 10000.0 * 0.10) / (1024.0 * 1024.0)
    assert small_trace == pytest.approx(expected_small)
    assert large_trace == pytest.approx(expected_large)
    assert large_trace == pytest.approx(10.0 * small_trace)


def test_estimate_run_footprint_rejects_negative_predictions() -> None:
    with pytest.raises(ValueError, match="predictions_estimated"):
        estimate_run_footprint_mb(predictions_estimated=-1)


def test_estimate_run_footprint_rejects_negative_byte_params() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        estimate_run_footprint_mb(
            predictions_estimated=10,
            avg_trace_bytes=-1.0,
        )


# ---------------------------------------------------------------------------
# measure_current_footprint_mb
# ---------------------------------------------------------------------------


def test_measure_current_footprint_empty_db(conn: duckdb.DuckDBPyConnection) -> None:
    result = measure_current_footprint_mb(conn)
    assert result == pytest.approx(0.0, abs=1e-6)


def test_measure_current_footprint_after_inserts_grows(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    before = measure_current_footprint_mb(conn)
    # Insert 100 runs to drive estimated_size > 0.
    for index in range(100):
        operations.insert_run(conn, _make_run(run_id=f"run-{index:04d}"))
    after = measure_current_footprint_mb(conn)
    assert after > before


# ---------------------------------------------------------------------------
# assert_under_cap
# ---------------------------------------------------------------------------


def test_assert_under_cap_passes_when_within_limit() -> None:
    # Should not raise.
    assert_under_cap(current_mb=10.0, projected_additional_mb=20.0, cap_mb=100)


def test_assert_under_cap_raises_when_exceeded() -> None:
    with pytest.raises(DiskBudgetError) as exc_info:
        assert_under_cap(
            current_mb=80.0,
            projected_additional_mb=50.0,
            cap_mb=100,
        )
    message = str(exc_info.value)
    assert "100" in message
    assert "80" in message or "130" in message


def test_assert_under_cap_at_exact_cap_passes() -> None:
    """Cap is interpreted as inclusive (total <= cap_mb is allowed)."""
    assert_under_cap(current_mb=40.0, projected_additional_mb=60.0, cap_mb=100)


def test_assert_under_cap_default_cap_is_100() -> None:
    assert DEFAULT_DISK_CAP_MB == 100
    # Below the default cap -> passes silently.
    assert_under_cap(current_mb=1.0, projected_additional_mb=1.0)
    with pytest.raises(DiskBudgetError):
        assert_under_cap(current_mb=99.0, projected_additional_mb=2.0)


def test_assert_under_cap_rejects_negative_inputs() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        assert_under_cap(current_mb=-1.0, projected_additional_mb=1.0)


# ---------------------------------------------------------------------------
# projected_disk_usage_mb
# ---------------------------------------------------------------------------


def test_projected_disk_usage_returns_dict_with_all_keys(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    payload = projected_disk_usage_mb(conn, predictions_estimated=100)
    assert set(payload.keys()) == {
        "current_mb",
        "projected_additional_mb",
        "projected_total_mb",
        "cap_mb",
        "headroom_mb",
    }
    for value in payload.values():
        assert isinstance(value, float)


def test_projected_disk_usage_headroom_positive_when_under(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    payload = projected_disk_usage_mb(
        conn,
        predictions_estimated=100,
        cap_mb=DEFAULT_DISK_CAP_MB,
    )
    assert payload["headroom_mb"] > 0
    assert payload["projected_total_mb"] == pytest.approx(
        payload["current_mb"] + payload["projected_additional_mb"]
    )
    assert payload["cap_mb"] == float(DEFAULT_DISK_CAP_MB)


def test_projected_disk_usage_headroom_negative_when_over(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    # Pick a prediction count large enough that the projection alone breaches
    # any reasonable cap. With avg_trace_bytes=1500 and 200 B per prediction
    # row, each prediction contributes ~350 B. 10 million predictions ->
    # ~3.3 GB, comfortably above the 100 MB cap.
    payload = projected_disk_usage_mb(
        conn,
        predictions_estimated=10_000_000,
        cap_mb=DEFAULT_DISK_CAP_MB,
    )
    assert payload["headroom_mb"] < 0
    assert payload["projected_total_mb"] > payload["cap_mb"]
