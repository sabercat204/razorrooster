"""Unit tests for calibration_backtest persistence operations (T-CB-010).

Each test acquires a fresh in-memory DuckDB connection via the ``conn``
fixture; the migration runner applies m6001 + m6002 so the connection
already carries the canonical schema. Tests exercise the public
operations surface end-to-end:

* Round-trip of :class:`BacktestRun` / :class:`BacktestPrediction` /
  :class:`BacktestTrace` through insert + fetch.
* The append-only contract (REQ-CB-PERSIST-001): ``backtest_runs`` rows
  may only transition ``in_progress`` -> ``complete`` / ``failed``;
  every other transition raises :class:`BacktestPersistenceError`.
* The idempotent-cache primitives (REQ-CB-RUN-004): :func:`fetch_run_status`
  returns the current status, :func:`run_exists` is a thin existence
  predicate.
* JSON column determinism via the canonical ``sort_keys=True`` encoder.
* A static check that no ``delete_*`` / ``drop_*`` / ``remove_*`` helpers
  are exposed by the operations module — append-only is a public
  contract, not a coding suggestion.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
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
_COMPLETED = datetime(2024, 6, 1, 0, 5, 0, tzinfo=UTC)
_PRED_TS = datetime(2024, 1, 8, tzinfo=UTC)
_RES_TS = datetime(2024, 1, 15, tzinfo=UTC)


def _run_kwargs(**overrides: Any) -> dict[str, Any]:
    """Return canonical BacktestRun kwargs with optional overrides applied."""
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
    return base


def _make_run(**overrides: Any) -> BacktestRun:
    return BacktestRun(**_run_kwargs(**overrides))


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


# ---------------------------------------------------------------------------
# insert_run / fetch_run / run_exists / fetch_run_status
# ---------------------------------------------------------------------------


def test_insert_run_then_fetch_round_trips(conn: duckdb.DuckDBPyConnection) -> None:
    run = _make_run()
    operations.insert_run(conn, run)
    fetched = operations.fetch_run(conn, run.run_id)
    assert fetched is not None
    assert fetched.run_id == run.run_id
    assert fetched.lag_days == run.lag_days
    assert fetched.class_ids == run.class_ids
    assert fetched.sectors == run.sectors
    assert fetched.venues == run.venues
    assert fetched.bin_count_per_sector == run.bin_count_per_sector
    assert fetched.status is BacktestStatus.IN_PROGRESS
    assert fetched.allow_recent is False
    assert fetched.disclaimer_version == "v1"


def test_insert_run_with_jsons(conn: duckdb.DuckDBPyConnection) -> None:
    run = _make_run(
        class_ids=("a", "b", "c"),
        sectors=("s1", "s2"),
        venues=("polymarket", "kalshi"),
        bin_count_per_sector={"s1": 5, "s2": 7},
    )
    operations.insert_run(conn, run)
    fetched = operations.fetch_run(conn, run.run_id)
    assert fetched is not None
    assert fetched.class_ids == ("a", "b", "c")
    assert fetched.sectors == ("s1", "s2")
    assert fetched.venues == ("polymarket", "kalshi")
    assert fetched.bin_count_per_sector == {"s1": 5, "s2": 7}


def test_insert_run_duplicate_run_id_raises(conn: duckdb.DuckDBPyConnection) -> None:
    run = _make_run()
    operations.insert_run(conn, run)
    with pytest.raises(BacktestPersistenceError, match="abc123"):
        operations.insert_run(conn, run)


def test_run_exists_true_after_insert(conn: duckdb.DuckDBPyConnection) -> None:
    run = _make_run()
    operations.insert_run(conn, run)
    assert operations.run_exists(conn, run.run_id) is True


def test_run_exists_false_for_missing(conn: duckdb.DuckDBPyConnection) -> None:
    assert operations.run_exists(conn, "missing") is False


def test_fetch_run_returns_none_for_missing(conn: duckdb.DuckDBPyConnection) -> None:
    assert operations.fetch_run(conn, "missing") is None


def test_fetch_run_status_returns_status(conn: duckdb.DuckDBPyConnection) -> None:
    operations.insert_run(conn, _make_run())
    assert operations.fetch_run_status(conn, "abc123") is BacktestStatus.IN_PROGRESS


def test_fetch_run_status_returns_none_for_missing(conn: duckdb.DuckDBPyConnection) -> None:
    assert operations.fetch_run_status(conn, "missing") is None


# ---------------------------------------------------------------------------
# update_run_status
# ---------------------------------------------------------------------------


def test_update_run_status_in_progress_to_complete(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    operations.insert_run(conn, _make_run())
    summary: dict[str, Any] = {"per_sector_brier": {"public_health": 0.18}}
    operations.update_run_status(
        conn,
        "abc123",
        BacktestStatus.COMPLETE,
        completed_at=_COMPLETED,
        summary_json=summary,
        predictions_total=10,
        predictions_scored=8,
        predictions_skipped=2,
        overall_brier=0.21,
        fallback_polarity_count=1,
    )
    fetched = operations.fetch_run(conn, "abc123")
    assert fetched is not None
    assert fetched.status is BacktestStatus.COMPLETE
    assert fetched.completed_at == _COMPLETED
    assert fetched.predictions_total == 10
    assert fetched.predictions_scored == 8
    assert fetched.predictions_skipped == 2
    assert fetched.overall_brier == pytest.approx(0.21)
    assert fetched.fallback_polarity_count == 1
    assert isinstance(fetched.summary_json, Mapping)
    assert dict(fetched.summary_json) == summary


def test_update_run_status_in_progress_to_failed_with_error_summary(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    operations.insert_run(conn, _make_run())
    operations.update_run_status(
        conn,
        "abc123",
        BacktestStatus.FAILED,
        completed_at=_COMPLETED,
        error_summary="boom",
    )
    fetched = operations.fetch_run(conn, "abc123")
    assert fetched is not None
    assert fetched.status is BacktestStatus.FAILED
    assert fetched.error_summary == "boom"
    assert fetched.completed_at == _COMPLETED


def test_update_run_status_disallowed_transition_raises(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    operations.insert_run(conn, _make_run())
    operations.update_run_status(
        conn,
        "abc123",
        BacktestStatus.COMPLETE,
        completed_at=_COMPLETED,
    )
    # Re-completing a complete run is forbidden.
    with pytest.raises(BacktestPersistenceError, match="disallowed transition"):
        operations.update_run_status(
            conn,
            "abc123",
            BacktestStatus.COMPLETE,
            completed_at=_COMPLETED + timedelta(minutes=1),
        )


def test_update_run_status_self_transition_raises(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    operations.insert_run(conn, _make_run())
    # in_progress -> in_progress is not a legal transition.
    with pytest.raises(BacktestPersistenceError, match="disallowed transition"):
        operations.update_run_status(
            conn,
            "abc123",
            BacktestStatus.IN_PROGRESS,
        )


def test_update_run_status_failed_to_complete_raises(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    operations.insert_run(conn, _make_run())
    operations.update_run_status(
        conn,
        "abc123",
        BacktestStatus.FAILED,
        completed_at=_COMPLETED,
        error_summary="boom",
    )
    with pytest.raises(BacktestPersistenceError, match="disallowed transition"):
        operations.update_run_status(
            conn,
            "abc123",
            BacktestStatus.COMPLETE,
            completed_at=_COMPLETED,
        )


def test_update_run_status_missing_run_raises(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(BacktestPersistenceError, match="does not exist"):
        operations.update_run_status(
            conn,
            "missing",
            BacktestStatus.COMPLETE,
            completed_at=_COMPLETED,
        )


def test_update_run_status_preserves_unset_fields(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Optional kwargs default to ``None`` and leave columns alone."""
    initial = _make_run(
        predictions_total=5,
        predictions_scored=4,
        predictions_skipped=1,
        bin_count_per_sector={"public_health": 5},
    )
    operations.insert_run(conn, initial)
    # Mutate to ``complete`` without providing counters.
    operations.update_run_status(conn, "abc123", BacktestStatus.COMPLETE)
    fetched = operations.fetch_run(conn, "abc123")
    assert fetched is not None
    assert fetched.predictions_total == 5
    assert fetched.predictions_scored == 4
    assert fetched.predictions_skipped == 1


# ---------------------------------------------------------------------------
# insert_prediction / insert_predictions_batch / fetch_predictions
# ---------------------------------------------------------------------------


def test_insert_prediction(conn: duckdb.DuckDBPyConnection) -> None:
    operations.insert_run(conn, _make_run())
    prediction = _make_prediction()
    operations.insert_prediction(conn, prediction)
    fetched = operations.fetch_predictions(conn, "abc123")
    assert len(fetched) == 1
    assert fetched[0].prediction_id == "pred-001"
    assert fetched[0].polarity is PolarityValue.INVERTED
    assert fetched[0].polarity_source is PolaritySource.COMPARISON_RESOLUTIONS
    assert fetched[0].status is PredictionStatus.SCORED
    assert fetched[0].skip_reason is None
    assert fetched[0].mapping_mismatch_warning is False


def test_insert_prediction_skipped_with_reason(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    operations.insert_run(conn, _make_run())
    prediction = _make_prediction(
        prediction_id="pred-002",
        model_p=None,
        observed=None,
        brier_contribution=None,
        polarity=None,
        polarity_source=PolaritySource.CURRENT_MAPPING_FALLBACK,
        mapping_mismatch_warning=True,
        status=PredictionStatus.SKIPPED,
        skip_reason=SkipReason.INVALID_RESOLUTION,
    )
    operations.insert_prediction(conn, prediction)
    fetched = operations.fetch_predictions(conn, "abc123")
    assert len(fetched) == 1
    assert fetched[0].status is PredictionStatus.SKIPPED
    assert fetched[0].skip_reason is SkipReason.INVALID_RESOLUTION
    assert fetched[0].polarity is None
    assert fetched[0].mapping_mismatch_warning is True


def test_insert_predictions_batch_inserts_all(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    operations.insert_run(conn, _make_run())
    predictions = [_make_prediction(prediction_id=f"pred-{i:03d}") for i in range(1, 6)]
    operations.insert_predictions_batch(conn, predictions)
    fetched = operations.fetch_predictions(conn, "abc123")
    assert len(fetched) == 5
    assert [p.prediction_id for p in fetched] == [
        "pred-001",
        "pred-002",
        "pred-003",
        "pred-004",
        "pred-005",
    ]


def test_insert_predictions_batch_empty_no_op(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    operations.insert_run(conn, _make_run())
    operations.insert_predictions_batch(conn, [])
    fetched = operations.fetch_predictions(conn, "abc123")
    assert fetched == ()


def test_insert_predictions_batch_duplicate_pk_raises(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    operations.insert_run(conn, _make_run())
    predictions = [
        _make_prediction(prediction_id="dup"),
        _make_prediction(prediction_id="dup"),
    ]
    with pytest.raises(BacktestPersistenceError):
        operations.insert_predictions_batch(conn, predictions)


def test_fetch_predictions_for_missing_run_returns_empty(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    assert operations.fetch_predictions(conn, "no-such-run") == ()


def test_fetch_predictions_returns_in_order(conn: duckdb.DuckDBPyConnection) -> None:
    operations.insert_run(conn, _make_run())
    # Insert out of natural order; expect ordering by prediction_id ASC.
    operations.insert_prediction(conn, _make_prediction(prediction_id="pred-c"))
    operations.insert_prediction(conn, _make_prediction(prediction_id="pred-a"))
    operations.insert_prediction(conn, _make_prediction(prediction_id="pred-b"))
    fetched = operations.fetch_predictions(conn, "abc123")
    assert [p.prediction_id for p in fetched] == ["pred-a", "pred-b", "pred-c"]


# ---------------------------------------------------------------------------
# insert_trace / fetch_trace
# ---------------------------------------------------------------------------


def test_insert_trace_with_blob(conn: duckdb.DuckDBPyConnection) -> None:
    operations.insert_run(conn, _make_run())
    operations.insert_prediction(conn, _make_prediction())
    blob = b"\x28\xb5\x2f\xfd" + b"\x00" * 100
    trace = _make_trace(trace_json_compressed=blob, decompressed_size_bytes=4096)
    operations.insert_trace(conn, trace)
    fetched = operations.fetch_trace(conn, "abc123", "pred-001")
    assert fetched is not None
    assert fetched.trace_json_compressed == blob
    assert fetched.decompressed_size_bytes == 4096
    assert fetched.compression_algorithm is CompressionAlgorithm.ZSTD


def test_fetch_trace_round_trips_blob(conn: duckdb.DuckDBPyConnection) -> None:
    operations.insert_run(conn, _make_run())
    operations.insert_prediction(conn, _make_prediction())
    payload = bytes(range(256))
    trace = _make_trace(trace_json_compressed=payload, decompressed_size_bytes=2048)
    operations.insert_trace(conn, trace)
    fetched = operations.fetch_trace(conn, "abc123", "pred-001")
    assert fetched is not None
    assert fetched.trace_json_compressed == payload


def test_fetch_trace_missing_returns_none(conn: duckdb.DuckDBPyConnection) -> None:
    assert operations.fetch_trace(conn, "abc123", "pred-001") is None


def test_insert_trace_duplicate_pk_raises(conn: duckdb.DuckDBPyConnection) -> None:
    operations.insert_run(conn, _make_run())
    operations.insert_prediction(conn, _make_prediction())
    operations.insert_trace(conn, _make_trace())
    with pytest.raises(BacktestPersistenceError):
        operations.insert_trace(conn, _make_trace())


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


def test_list_runs_sorted_by_started_at_desc(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    runs = [
        _make_run(run_id="run-old", started_at=_STARTED),
        _make_run(run_id="run-mid", started_at=_STARTED + timedelta(hours=1)),
        _make_run(run_id="run-new", started_at=_STARTED + timedelta(hours=2)),
    ]
    for r in runs:
        operations.insert_run(conn, r)
    listed = operations.list_runs(conn)
    assert [r.run_id for r in listed] == ["run-new", "run-mid", "run-old"]


def test_list_runs_pagination(conn: duckdb.DuckDBPyConnection) -> None:
    for i in range(5):
        operations.insert_run(
            conn,
            _make_run(
                run_id=f"run-{i}",
                started_at=_STARTED + timedelta(hours=i),
            ),
        )
    listed = operations.list_runs(conn, limit=2)
    assert len(listed) == 2
    assert listed[0].run_id == "run-4"
    assert listed[1].run_id == "run-3"


def test_list_runs_filtered_by_since(conn: duckdb.DuckDBPyConnection) -> None:
    for i in range(3):
        operations.insert_run(
            conn,
            _make_run(
                run_id=f"run-{i}",
                started_at=_STARTED + timedelta(hours=i),
            ),
        )
    # since = STARTED + 1h excludes ``run-0``.
    listed = operations.list_runs(conn, since=_STARTED + timedelta(hours=1))
    assert {r.run_id for r in listed} == {"run-1", "run-2"}


def test_list_runs_filtered_by_until(conn: duckdb.DuckDBPyConnection) -> None:
    for i in range(3):
        operations.insert_run(
            conn,
            _make_run(
                run_id=f"run-{i}",
                started_at=_STARTED + timedelta(hours=i),
            ),
        )
    # until = STARTED + 1h excludes ``run-1`` and ``run-2`` (exclusive upper).
    listed = operations.list_runs(conn, until=_STARTED + timedelta(hours=1))
    assert {r.run_id for r in listed} == {"run-0"}


def test_list_runs_empty_when_no_rows(conn: duckdb.DuckDBPyConnection) -> None:
    assert operations.list_runs(conn) == ()


def test_list_runs_negative_limit_raises(conn: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(BacktestPersistenceError, match="limit"):
        operations.list_runs(conn, limit=-1)


def test_list_runs_zero_limit_returns_empty(conn: duckdb.DuckDBPyConnection) -> None:
    operations.insert_run(conn, _make_run())
    assert operations.list_runs(conn, limit=0) == ()


# ---------------------------------------------------------------------------
# Append-only contract — no deletion / drop / removal helpers
# ---------------------------------------------------------------------------


def test_append_only_contract_no_delete_helpers() -> None:
    """The operations module must not export deletion-shaped helpers.

    REQ-CB-PERSIST-001 forbids destructive mutations — pruning happens
    by deleting whole rows via ad-hoc maintenance, not via this module.
    """
    forbidden_prefixes = ("delete_", "drop_", "remove_", "truncate_", "purge_")
    exported: tuple[str, ...] = tuple(operations.__all__)
    offenders = [name for name in exported if name.lower().startswith(forbidden_prefixes)]
    assert not offenders, f"forbidden helper(s) exported: {offenders}"


# ---------------------------------------------------------------------------
# JSON determinism
# ---------------------------------------------------------------------------


def test_json_round_trip_for_complex_summary(conn: duckdb.DuckDBPyConnection) -> None:
    summary: dict[str, Any] = {
        "per_sector_brier": {"sector_b": 0.31, "sector_a": 0.18},
        "per_class_brier": {"class_z": 0.4, "class_a": 0.1},
        "reliability_per_sector": {
            "sector_a": {
                "bin_count": 2,
                "bins": [
                    {"lower_p": 0.0, "upper_p": 0.5, "count": 3},
                    {"lower_p": 0.5, "upper_p": 1.0, "count": 7},
                ],
            }
        },
        "zero_resolutions_sectors": [],
        "zero_resolutions_classes": ["class_q"],
    }
    operations.insert_run(conn, _make_run())
    operations.update_run_status(
        conn,
        "abc123",
        BacktestStatus.COMPLETE,
        completed_at=_COMPLETED,
        summary_json=summary,
    )
    fetched = operations.fetch_run(conn, "abc123")
    assert fetched is not None
    assert isinstance(fetched.summary_json, Mapping)
    # Deep equality survives the canonical-JSON round trip.
    assert dict(fetched.summary_json) == summary


def test_json_columns_are_canonical_sorted(conn: duckdb.DuckDBPyConnection) -> None:
    """The JSON encoder uses ``sort_keys=True`` so the on-disk text is stable."""
    operations.insert_run(
        conn,
        _make_run(
            class_ids=("z", "a", "m"),
            bin_count_per_sector={"sector_z": 5, "sector_a": 3},
        ),
    )
    # Pull the raw text; ``json.dumps(sort_keys=True)`` re-orders sectors alphabetically.
    row = conn.execute(
        "SELECT class_ids_json, bin_count_per_sector_json FROM backtest_runs WHERE run_id = ?",
        ["abc123"],
    ).fetchone()
    assert row is not None
    # ``class_ids_json`` is a list — preserve insertion order; the bin map
    # is a dict and must be sorted by key.
    assert row[0] == '["z","a","m"]'
    assert row[1] == '{"sector_a":3,"sector_z":5}'
