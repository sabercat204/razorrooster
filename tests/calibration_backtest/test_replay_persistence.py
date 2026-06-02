"""T-CB-019 — replay loop persistence + trace encoding wiring tests.

End-to-end coverage for the persistence-aware path of
:func:`razor_rooster.calibration_backtest.engines.replay.run_backtest`:

* Scored predictions land in ``backtest_predictions`` **and**
  ``backtest_traces`` with zstd-compressed trace blobs (REQ-CB-PERSIST-002,
  REQ-CB-RUN-005, design §3.5, §3.11).
* Skipped predictions land in ``backtest_predictions`` but **not** in
  ``backtest_traces`` (the trace BLOB is only persisted when there is a
  scored posterior to memorialise; design §3.11).
* The ``backtest_runs`` row transitions ``in_progress`` -> ``complete``
  on success and ``in_progress`` -> ``failed`` on an uncaught exception
  inside the loop body, with ``error_summary`` capturing ``str(exc)``.
* The persisted compression metadata (``compression_algorithm='zstd'``,
  ``decompressed_size_bytes``) round-trips through
  :func:`trace_codec.decode_trace` so a stored blob is decoder-symmetric
  with the in-memory trace dict.

The tests reuse the same upstream-schema fixture pattern as
``test_replay.py`` (in-memory DuckDB with the polymarket and
mispricing-detector DDL applied) and additionally apply the
calibration_backtest schema via the migration runner so the
persistence-aware code path is exercised on the same connection.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import duckdb
import pytest

from razor_rooster.calibration_backtest.engines import replay as replay_module
from razor_rooster.calibration_backtest.engines import trace_codec
from razor_rooster.calibration_backtest.engines.freezer import FrozenState
from razor_rooster.calibration_backtest.engines.replay import (
    DEFAULT_RECENT_WINDOW_DAYS,
    run_backtest,
)
from razor_rooster.calibration_backtest.models import (
    BacktestStatus,
    PredictionStatus,
    RunParameters,
    SkipReason,
)
from razor_rooster.calibration_backtest.persistence import operations
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
)
from razor_rooster.calibration_backtest.persistence.schemas import (
    TABLE_PREDICTIONS,
    TABLE_TRACES,
)
from razor_rooster.mispricing_detector.persistence.schemas import (
    CLASS_MARKET_MAPPINGS_DDL,
    COMPARISON_CYCLES_DDL,
    COMPARISON_RESOLUTIONS_DDL,
    COMPARISONS_DDL,
)
from razor_rooster.polymarket_connector.persistence.schemas import (
    POLYMARKET_RESOLUTIONS_DDL,
)
from tests.calibration_backtest.conftest import (
    insert_mapping as _insert_mapping,
)
from tests.calibration_backtest.conftest import (
    insert_resolution as _insert_resolution,
)

if TYPE_CHECKING:
    from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore

_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
"""Pinned wall-clock so the recent-window guard is deterministic."""

_FAKE_STORE: DuckDBStore = cast("DuckDBStore", object())
"""Sentinel store passed to ``run_backtest`` in tests that stub the pipeline.

These persistence tests stub :func:`evaluate_class_at_frozen_time` so the
``store`` argument is never dereferenced. The typed sentinel keeps mypy
``--strict`` honest without requiring a real ``DuckDBStore`` instance."""


# ---------------------------------------------------------------------------
# Connection fixture (upstream + calibration_backtest schemas on one conn)
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """In-memory DuckDB connection with upstream + persistence DDL applied.

    The replay loop reads from ``polymarket_resolutions`` /
    ``class_market_mappings`` (upstream) and writes to ``backtest_runs``
    / ``backtest_predictions`` / ``backtest_traces`` (persistence). For
    these integration tests we apply both schemas on the same
    connection so a single connection can be passed as both ``conn``
    and ``persistence_conn``.
    """
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(POLYMARKET_RESOLUTIONS_DDL)
        connection.execute(CLASS_MARKET_MAPPINGS_DDL)
        connection.execute(COMPARISON_CYCLES_DDL)
        connection.execute(COMPARISONS_DDL)
        connection.execute(COMPARISON_RESOLUTIONS_DDL)
        run_pending_calibration_backtest_migrations(connection)
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Seed helpers (promoted to tests/calibration_backtest/conftest.py for reuse)
# ---------------------------------------------------------------------------
#
# The ``_insert_resolution`` / ``_insert_mapping`` helpers that this file
# defined locally were promoted to the shared conftest.py during Phase 8 so
# the e2e / perf / property tests can seed the same upstream rows without
# re-defining the SQL inserts. The imports above bind the public names to
# the original ``_insert_*`` aliases so every call site in this module
# stays zero-diff.


def _make_params(
    *,
    since_ts: datetime = datetime(2025, 1, 1, tzinfo=UTC),
    until_ts: datetime = datetime(2025, 12, 31, tzinfo=UTC),
    lag_days: int = 7,
    class_ids: tuple[str, ...] = ("cls-A",),
    sectors: tuple[str, ...] = (),
    venues: tuple[str, ...] = ("polymarket",),
    allow_recent: bool = False,
) -> RunParameters:
    return RunParameters(
        since_ts=since_ts,
        until_ts=until_ts,
        lag_days=lag_days,
        class_ids=class_ids,
        sectors=sectors,
        venues=venues,
        allow_recent=allow_recent,
    )


# ---------------------------------------------------------------------------
# Pipeline stubs (no pattern_library / signal_scanner involvement)
# ---------------------------------------------------------------------------


def _stub_freeze(_conn: duckdb.DuckDBPyConnection, prediction_ts: datetime) -> FrozenState:
    return FrozenState(
        source_publication_ts_boundary=prediction_ts,
        frozen_flag=True,
        registered_sources=frozenset({"fred"}),
    )


def _stub_evaluate(
    class_id: str,
    prediction_ts: datetime,
    frozen: FrozenState,
    *,
    store: Any,
    library_version: int | None = None,
    min_support: int = 1,
    n_samples: int | None = None,
    co_occurrence_correction: float = 0.0,
) -> tuple[float, dict[str, Any]]:
    """Return a fixed posterior + a JSON-roundtrippable trace dict."""
    trace = {
        "class": {
            "class_id": class_id,
            "definition_version": 3,
        },
        "data_as_of": prediction_ts.isoformat(),
        "library_version": library_version or 1,
        "posterior": {"mean": 0.42, "ci_lower": 0.35, "ci_upper": 0.49},
    }
    return 0.42, trace


@pytest.fixture
def patched_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay_module.freezer_module, "freeze", _stub_freeze)
    monkeypatch.setattr(replay_module, "evaluate_class_at_frozen_time", _stub_evaluate)


# ---------------------------------------------------------------------------
# Scored predictions: rows in backtest_predictions AND backtest_traces
# ---------------------------------------------------------------------------


def test_scored_predictions_land_in_predictions_and_traces_tables(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """Each scored prediction inserts one row into both tables (REQ-CB-PERSIST-002)."""
    base_ts = datetime(2025, 6, 1, tzinfo=UTC)
    for index in range(2):
        condition_id = f"cond-{index}"
        _insert_resolution(
            conn,
            condition_id=condition_id,
            resolution_ts=base_ts + timedelta(days=index),
        )
        _insert_mapping(
            conn,
            mapping_id=f"m-{index}",
            class_id="cls-A",
            condition_id=condition_id,
        )
    params = _make_params(
        since_ts=base_ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=_FAKE_STORE,
        now=_NOW,
        max_workers=1,
        persistence_conn=conn,
    )

    # In-memory result still works.
    assert len(result.predictions) == 2
    assert all(p.status is PredictionStatus.SCORED for p in result.predictions)

    # Persistence: predictions + traces both have two rows for the run.
    pred_rows = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE_PREDICTIONS} WHERE run_id = ?",
        [result.run.run_id],
    ).fetchone()
    assert pred_rows is not None and int(pred_rows[0]) == 2

    trace_rows = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE_TRACES} WHERE run_id = ?",
        [result.run.run_id],
    ).fetchone()
    assert trace_rows is not None and int(trace_rows[0]) == 2

    # Each persisted prediction has a matching trace (composite PK).
    for prediction in result.predictions:
        trace = operations.fetch_trace(conn, result.run.run_id, prediction.prediction_id)
        assert trace is not None
        assert trace.compression_algorithm.value == "zstd"
        assert trace.decompressed_size_bytes > 0
        decoded = trace_codec.decode_trace(trace.trace_json_compressed)
        # The decoded payload matches the in-memory trace dict.
        in_memory = result.traces[prediction.prediction_id]
        assert dict(decoded) == in_memory


def test_trace_encode_round_trips_through_persistence(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """``trace_codec.encode`` produces the same bytes the persistence path stores."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(conn, condition_id="cond-1", resolution_ts=ts)
    _insert_mapping(conn, mapping_id="m-1", class_id="cls-A", condition_id="cond-1")
    params = _make_params(
        since_ts=ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=_FAKE_STORE,
        now=_NOW,
        max_workers=1,
        persistence_conn=conn,
    )
    assert len(result.predictions) == 1
    prediction = result.predictions[0]
    in_memory_trace = result.traces[prediction.prediction_id]

    # The new public ``encode`` helper produces canonical bytes.
    expected_blob = trace_codec.encode(in_memory_trace)
    persisted = operations.fetch_trace(conn, result.run.run_id, prediction.prediction_id)
    assert persisted is not None
    assert persisted.trace_json_compressed == expected_blob
    # Symmetric ``decode`` alias also recovers the dict.
    assert dict(trace_codec.decode(persisted.trace_json_compressed)) == in_memory_trace


# ---------------------------------------------------------------------------
# Skipped predictions: row in backtest_predictions but not in backtest_traces
# ---------------------------------------------------------------------------


def test_skipped_predictions_land_in_predictions_only(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """``invalidated`` resolution -> ``backtest_predictions`` row, no trace row."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(
        conn,
        condition_id="cond-bad",
        resolution_ts=ts,
        invalidated=True,
    )
    _insert_mapping(conn, mapping_id="m-1", class_id="cls-A", condition_id="cond-bad")
    params = _make_params(
        since_ts=ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=_FAKE_STORE,
        now=_NOW,
        max_workers=1,
        persistence_conn=conn,
    )
    assert len(result.predictions) == 1
    prediction = result.predictions[0]
    assert prediction.status is PredictionStatus.SKIPPED
    assert prediction.skip_reason is SkipReason.INVALID_RESOLUTION

    # Predictions row exists.
    persisted_predictions = operations.fetch_predictions(conn, result.run.run_id)
    assert len(persisted_predictions) == 1
    assert persisted_predictions[0].status is PredictionStatus.SKIPPED

    # No trace row for the skipped prediction.
    trace_rows = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE_TRACES} WHERE run_id = ?",
        [result.run.run_id],
    ).fetchone()
    assert trace_rows is not None and int(trace_rows[0]) == 0


def test_mixed_scored_and_skipped_predictions_persist_correctly(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """One scored + one skipped row -> two predictions, one trace."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(
        conn,
        condition_id="cond-good",
        resolution_ts=ts,
    )
    _insert_resolution(
        conn,
        condition_id="cond-bad",
        resolution_ts=ts + timedelta(days=1),
        invalidated=True,
    )
    _insert_mapping(conn, mapping_id="m-good", class_id="cls-A", condition_id="cond-good")
    _insert_mapping(conn, mapping_id="m-bad", class_id="cls-A", condition_id="cond-bad")
    params = _make_params(
        since_ts=ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=_FAKE_STORE,
        now=_NOW,
        max_workers=1,
        persistence_conn=conn,
    )
    persisted = operations.fetch_predictions(conn, result.run.run_id)
    assert len(persisted) == 2
    statuses = {p.status for p in persisted}
    assert statuses == {PredictionStatus.SCORED, PredictionStatus.SKIPPED}

    trace_rows = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE_TRACES} WHERE run_id = ?",
        [result.run.run_id],
    ).fetchone()
    assert trace_rows is not None and int(trace_rows[0]) == 1


# ---------------------------------------------------------------------------
# Status transitions: in_progress -> complete on success
# ---------------------------------------------------------------------------


def test_run_row_transitions_in_progress_to_complete_on_success(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """A successful replay leaves the run row at ``status=COMPLETE``."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(conn, condition_id="cond-1", resolution_ts=ts)
    _insert_mapping(conn, mapping_id="m-1", class_id="cls-A", condition_id="cond-1")
    params = _make_params(
        since_ts=ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=_FAKE_STORE,
        now=_NOW,
        max_workers=1,
        persistence_conn=conn,
    )
    persisted = operations.fetch_run(conn, result.run.run_id)
    assert persisted is not None
    assert persisted.status is BacktestStatus.COMPLETE
    assert persisted.completed_at is not None
    assert persisted.error_summary is None
    # Counters were promoted from the in-progress placeholder.
    assert persisted.predictions_total == 1
    assert persisted.predictions_scored == 1
    assert persisted.predictions_skipped == 0
    # The summary_json carries the counter snapshot the orchestrator
    # populates before T-CB-021's full Brier aggregation lands.
    assert persisted.summary_json is not None
    assert dict(persisted.summary_json)["predictions_scored"] == 1


def test_in_progress_run_row_visible_during_loop(
    conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The IN_PROGRESS row is committed before the inner loop fires.

    The orchestrator inserts the row before any per-prediction work; we
    observe that by stubbing :func:`evaluate_class_at_frozen_time` to
    inspect the row mid-loop. By the time the stub runs, the row must
    already exist with ``status=IN_PROGRESS``.
    """
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(conn, condition_id="cond-1", resolution_ts=ts)
    _insert_mapping(conn, mapping_id="m-1", class_id="cls-A", condition_id="cond-1")
    monkeypatch.setattr(replay_module.freezer_module, "freeze", _stub_freeze)

    seen_status: list[BacktestStatus | None] = []

    def _peek_evaluate(
        class_id: str,
        prediction_ts: datetime,
        frozen: FrozenState,
        *,
        store: Any,
        library_version: int | None = None,
        min_support: int = 1,
        n_samples: int | None = None,
        co_occurrence_correction: float = 0.0,
    ) -> tuple[float, dict[str, Any]]:
        # Read every backtest_runs row's status mid-loop.
        rows = conn.execute("SELECT status FROM backtest_runs").fetchall()
        for row in rows:
            seen_status.append(BacktestStatus(str(row[0])))
        return _stub_evaluate(
            class_id,
            prediction_ts,
            frozen,
            store=store,
            library_version=library_version,
            min_support=min_support,
            n_samples=n_samples,
            co_occurrence_correction=co_occurrence_correction,
        )

    monkeypatch.setattr(replay_module, "evaluate_class_at_frozen_time", _peek_evaluate)

    params = _make_params(
        since_ts=ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    run_backtest(
        params,
        conn=conn,
        store=_FAKE_STORE,
        now=_NOW,
        max_workers=1,
        persistence_conn=conn,
    )
    # The stub was called once and saw exactly one row at IN_PROGRESS.
    assert seen_status == [BacktestStatus.IN_PROGRESS]


# ---------------------------------------------------------------------------
# Status transitions: in_progress -> failed on uncaught exception
# ---------------------------------------------------------------------------


def test_run_row_transitions_in_progress_to_failed_on_uncaught_exception(
    conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loop-level exception transitions the run row to FAILED.

    Per-prediction exceptions are routed to ``skip_reason='exception'``
    and do not propagate; we induce a loop-level failure by stubbing
    :func:`iter_mapped_resolutions` to raise after the IN_PROGRESS row
    is committed.
    """

    def _explode(
        _conn: duckdb.DuckDBPyConnection,
        _since: datetime,
        _until: datetime,
        _venues: Any,
        _classes: Any,
    ) -> Any:
        raise RuntimeError("synthetic loop failure")

    monkeypatch.setattr(replay_module, "iter_mapped_resolutions", _explode)

    params = _make_params(
        since_ts=datetime(2025, 1, 1, tzinfo=UTC),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    with pytest.raises(RuntimeError, match="synthetic loop failure"):
        run_backtest(
            params,
            conn=conn,
            store=_FAKE_STORE,
            now=_NOW,
            max_workers=1,
            persistence_conn=conn,
        )

    # Exactly one run row exists, transitioned to FAILED with the error
    # summary captured.
    rows = conn.execute("SELECT run_id, status, error_summary FROM backtest_runs").fetchall()
    assert len(rows) == 1
    run_id, status, error_summary = rows[0]
    assert BacktestStatus(str(status)) is BacktestStatus.FAILED
    assert "synthetic loop failure" in str(error_summary)

    # The persisted row roundtrips through the typed fetcher.
    persisted = operations.fetch_run(conn, str(run_id))
    assert persisted is not None
    assert persisted.status is BacktestStatus.FAILED
    assert persisted.error_summary is not None
    assert "synthetic loop failure" in persisted.error_summary


# ---------------------------------------------------------------------------
# Persistence-conn=None path remains pure in-memory (regression guard)
# ---------------------------------------------------------------------------


def test_no_persistence_conn_writes_no_rows(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """Default ``persistence_conn=None`` keeps the loop pure in-memory."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(conn, condition_id="cond-1", resolution_ts=ts)
    _insert_mapping(conn, mapping_id="m-1", class_id="cls-A", condition_id="cond-1")
    params = _make_params(
        since_ts=ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=_FAKE_STORE,
        now=_NOW,
        max_workers=1,
    )
    assert len(result.predictions) == 1

    # No rows written to any of the three calibration_backtest tables.
    runs = conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()
    predictions = conn.execute(f"SELECT COUNT(*) FROM {TABLE_PREDICTIONS}").fetchone()
    traces = conn.execute(f"SELECT COUNT(*) FROM {TABLE_TRACES}").fetchone()
    assert runs is not None and int(runs[0]) == 0
    assert predictions is not None and int(predictions[0]) == 0
    assert traces is not None and int(traces[0]) == 0
