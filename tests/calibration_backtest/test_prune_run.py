"""Unit tests for :func:`operations.prune_run` (T-CB-032 helper).

``prune_run`` is the lone destructive helper exposed by
``persistence.operations`` — used by the ``calibration-backtest prune``
CLI subcommand to expunge obsolete runs after a library bump or
disk-pressure event (design §3.9). The tests below pin the four
properties the CLI relies on:

1. **Cascade**: a single call deletes the parent ``backtest_runs`` row
   plus every descendant ``backtest_predictions`` and
   ``backtest_traces`` row sharing its ``run_id`` (FKs are not
   declared, so the ordering is implemented in user-space).
2. **Counters**: the returned ``{'traces': N, 'predictions': N,
   'runs': N}`` dict reflects the actual row counts removed; the CLI
   surfaces these in its summary output.
3. **Atomicity**: a failure injected mid-transaction (after the
   children have been deleted but before the parent row is removed)
   rolls back every delete, so the database is left in its pre-call
   state with no orphan trace / prediction rows.
4. **Idempotency**: calling ``prune_run`` with a ``run_id`` that does
   not exist is a silent no-op (every count is zero, no error is
   raised), matching the CLI's per-row dispatch over a ``--before``
   filter where multiple operators may concurrently target the same
   stale window.
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
    BacktestTrace,
    CompressionAlgorithm,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
)
from razor_rooster.calibration_backtest.persistence import operations
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
)
from razor_rooster.calibration_backtest.persistence.schemas import (
    TABLE_PREDICTIONS,
    TABLE_RUNS,
    TABLE_TRACES,
)

# ---------------------------------------------------------------------------
# Fixtures and factories (mirror test_persistence_operations.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield an in-memory DuckDB connection with all migrations applied."""
    connection = duckdb.connect(":memory:")
    try:
        run_pending_calibration_backtest_migrations(connection)
        yield connection
    finally:
        connection.close()


_SINCE = datetime(2024, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2024, 6, 1, tzinfo=UTC)
_STARTED = datetime(2024, 6, 1, tzinfo=UTC)
_PRED_TS = datetime(2024, 1, 8, tzinfo=UTC)
_RES_TS = datetime(2024, 1, 15, tzinfo=UTC)


def _make_run(**overrides: Any) -> BacktestRun:
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
    base.update(overrides)
    return BacktestRun(**base)


def _make_prediction(**overrides: Any) -> BacktestPrediction:
    base: dict[str, Any] = {
        "run_id": "abc123",
        "prediction_id": "pred-001",
        "class_id": "flu_h2h",
        "condition_id": "cond-1",
        "venue": "polymarket",
        "sector": "public_health",
        "prediction_ts": _PRED_TS,
        "resolution_ts": _RES_TS,
        "model_p": 0.4,
        "observed": 1.0,
        "polarity": PolarityValue.INVERTED,
        "polarity_source": PolaritySource.COMPARISON_RESOLUTIONS,
        "mapping_mismatch_warning": False,
        "definition_version": 1,
        "status": PredictionStatus.SCORED,
        "skip_reason": None,
        "brier_contribution": 0.36,
    }
    base.update(overrides)
    return BacktestPrediction(**base)


def _make_trace(**overrides: Any) -> BacktestTrace:
    base: dict[str, Any] = {
        "run_id": "abc123",
        "prediction_id": "pred-001",
        "trace_json_compressed": b"\x28\xb5\x2f\xfd\x00\x01\x02\x03",
        "decompressed_size_bytes": 1024,
        "compression_algorithm": CompressionAlgorithm.ZSTD,
    }
    base.update(overrides)
    return BacktestTrace(**base)


def _seed_run_with_descendants(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    n_predictions: int = 3,
) -> None:
    """Insert a run plus *n_predictions* predictions and matching traces."""
    operations.insert_run(connection, _make_run(run_id=run_id))
    predictions = [
        _make_prediction(run_id=run_id, prediction_id=f"pred-{i:03d}")
        for i in range(1, n_predictions + 1)
    ]
    operations.insert_predictions_batch(connection, predictions)
    for prediction in predictions:
        operations.insert_trace(
            connection,
            _make_trace(run_id=run_id, prediction_id=prediction.prediction_id),
        )


def _row_counts(connection: duckdb.DuckDBPyConnection, run_id: str) -> tuple[int, int, int]:
    """Return ``(runs, predictions, traces)`` counts for *run_id*."""
    runs_row = connection.execute(
        f"SELECT COUNT(*) FROM {TABLE_RUNS} WHERE run_id = ?",
        [run_id],
    ).fetchone()
    preds_row = connection.execute(
        f"SELECT COUNT(*) FROM {TABLE_PREDICTIONS} WHERE run_id = ?",
        [run_id],
    ).fetchone()
    traces_row = connection.execute(
        f"SELECT COUNT(*) FROM {TABLE_TRACES} WHERE run_id = ?",
        [run_id],
    ).fetchone()
    assert runs_row is not None
    assert preds_row is not None
    assert traces_row is not None
    return int(runs_row[0]), int(preds_row[0]), int(traces_row[0])


# ---------------------------------------------------------------------------
# Cascade — all three tables purged for the targeted run_id
# ---------------------------------------------------------------------------


def test_prune_run_deletes_all_three_tables(conn: duckdb.DuckDBPyConnection) -> None:
    _seed_run_with_descendants(conn, "abc123", n_predictions=3)
    # Sanity: we wrote what we expect.
    assert _row_counts(conn, "abc123") == (1, 3, 3)

    operations.prune_run(conn, "abc123")

    assert _row_counts(conn, "abc123") == (0, 0, 0)


def test_prune_run_only_targets_named_run_id(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A second run sharing the database is left intact."""
    _seed_run_with_descendants(conn, "victim", n_predictions=2)
    _seed_run_with_descendants(conn, "survivor", n_predictions=4)

    operations.prune_run(conn, "victim")

    assert _row_counts(conn, "victim") == (0, 0, 0)
    assert _row_counts(conn, "survivor") == (1, 4, 4)


# ---------------------------------------------------------------------------
# Counter dict — reports actual row counts removed
# ---------------------------------------------------------------------------


def test_prune_run_returns_correct_row_counts(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _seed_run_with_descendants(conn, "abc123", n_predictions=5)

    counts = operations.prune_run(conn, "abc123")

    assert counts == {"traces": 5, "predictions": 5, "runs": 1}


def test_prune_run_returns_dict_keys_in_canonical_set(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The dict shape is part of the CLI contract (T-CB-032 summary line)."""
    _seed_run_with_descendants(conn, "abc123", n_predictions=1)

    counts = operations.prune_run(conn, "abc123")

    assert set(counts.keys()) == {"traces", "predictions", "runs"}
    assert all(isinstance(v, int) for v in counts.values())


# ---------------------------------------------------------------------------
# Atomicity — mid-flight failure leaves no orphan rows
# ---------------------------------------------------------------------------


def test_prune_run_atomic_on_failure(conn: duckdb.DuckDBPyConnection) -> None:
    """Inject a mid-transaction error and assert no rows are removed."""
    _seed_run_with_descendants(conn, "abc123", n_predictions=3)
    pre_counts = _row_counts(conn, "abc123")
    assert pre_counts == (1, 3, 3)

    # Patch ``conn.execute`` so the THIRD DELETE (against backtest_runs) raises.
    # This places the failure AFTER the trace + prediction deletes have been
    # issued, exercising the rollback path most likely to leave orphans if the
    # transaction boundary is wrong.
    real_execute = conn.execute
    delete_runs_fragment = f"DELETE FROM {TABLE_RUNS} WHERE run_id = ?"

    def faulty_execute(sql: str, *args: object, **kwargs: object) -> Any:
        if sql == delete_runs_fragment:
            raise duckdb.Error("injected failure during DELETE FROM backtest_runs")
        return real_execute(sql, *args, **kwargs)

    # ``DuckDBPyConnection`` is a C type, so monkeypatch by wrapping the
    # bound method onto the conn proxy used by ``prune_run`` via ``setattr``.
    # We instead patch on a thin shim object that forwards to ``conn`` so we
    # can still issue the BEGIN / ROLLBACK statements during the test.
    class _Shim:
        def execute(self, sql: str, *args: object, **kwargs: object) -> Any:
            return faulty_execute(sql, *args, **kwargs)

        def __getattr__(self, name: str) -> object:
            return getattr(conn, name)

    shim = _Shim()
    with pytest.raises(BacktestPersistenceError, match="prune_run"):
        operations.prune_run(shim, "abc123")  # type: ignore[arg-type]

    # No orphan rows: the rollback restored the pre-call state exactly.
    assert _row_counts(conn, "abc123") == pre_counts


def test_prune_run_atomic_preserves_other_runs_on_failure(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A failed prune of one run does not affect a second run's rows."""
    _seed_run_with_descendants(conn, "victim", n_predictions=2)
    _seed_run_with_descendants(conn, "survivor", n_predictions=3)

    real_execute = conn.execute
    delete_runs_fragment = f"DELETE FROM {TABLE_RUNS} WHERE run_id = ?"

    def faulty_execute(sql: str, *args: object, **kwargs: object) -> Any:
        if sql == delete_runs_fragment:
            raise duckdb.Error("injected failure during DELETE FROM backtest_runs")
        return real_execute(sql, *args, **kwargs)

    class _Shim:
        def execute(self, sql: str, *args: object, **kwargs: object) -> Any:
            return faulty_execute(sql, *args, **kwargs)

        def __getattr__(self, name: str) -> object:
            return getattr(conn, name)

    shim = _Shim()
    with pytest.raises(BacktestPersistenceError):
        operations.prune_run(shim, "victim")  # type: ignore[arg-type]

    assert _row_counts(conn, "victim") == (1, 2, 2)
    assert _row_counts(conn, "survivor") == (1, 3, 3)


# ---------------------------------------------------------------------------
# Idempotency — pruning a missing run_id is a silent no-op
# ---------------------------------------------------------------------------


def test_prune_run_idempotent_on_missing(conn: duckdb.DuckDBPyConnection) -> None:
    """Targeting a non-existent run_id returns zeros and does not raise."""
    counts = operations.prune_run(conn, "no-such-run")
    assert counts == {"traces": 0, "predictions": 0, "runs": 0}


def test_prune_run_repeated_call_returns_zeros(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Second prune of the same run_id returns zeros after the first succeeds."""
    _seed_run_with_descendants(conn, "abc123", n_predictions=2)

    first = operations.prune_run(conn, "abc123")
    second = operations.prune_run(conn, "abc123")

    assert first == {"traces": 2, "predictions": 2, "runs": 1}
    assert second == {"traces": 0, "predictions": 0, "runs": 0}
    assert _row_counts(conn, "abc123") == (0, 0, 0)
