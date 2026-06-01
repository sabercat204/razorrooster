"""Calibration-backtest persistence operations (T-CB-010; design §3.5, §3.8).

Implements idempotent insert and update helpers plus the cached-summary fast
path used by the replay orchestrator (REQ-CB-RUN-004) and append-only
contract (REQ-CB-PERSIST-001).

Design contracts honoured here:

* **Append-only**: ``backtest_runs`` rows transition through ``in_progress``
  -> (``complete`` | ``failed``) exactly once. :func:`update_run_status` is
  the only mutator and rejects any other transition. Predictions and
  traces are inserted exactly once per ``(run_id, prediction_id)`` and
  the underlying ``PRIMARY KEY`` constraint enforces uniqueness.
* **Idempotent run cache**: :func:`fetch_run_status` lets the orchestrator
  short-circuit a re-run when a complete row already exists for the same
  ``run_id`` (REQ-CB-RUN-004).
* **JSON determinism**: every JSON column is serialised with
  ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` so the
  on-disk byte sequence is reproducible across runs and platforms.
* **Connection injection**: callers pass an open ``duckdb.DuckDBPyConnection``
  obtained from :class:`~razor_rooster.data_ingest.persistence.duckdb_store.DuckDBStore`.
  This module never opens or owns connections; tests pass an in-memory
  connection with both migrations applied.

Exceptions raised by the underlying DuckDB driver are wrapped in
:class:`~razor_rooster.calibration_backtest.errors.BacktestPersistenceError`
so callers can catch one type irrespective of driver.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

import duckdb

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
    ScoreSummary,
    SkipReason,
)
from razor_rooster.calibration_backtest.persistence.schemas import (
    TABLE_PREDICTIONS,
    TABLE_RUNS,
    TABLE_TRACES,
)

# ---------------------------------------------------------------------------
# Allowed status transitions on backtest_runs (REQ-CB-PERSIST-001)
# ---------------------------------------------------------------------------

_ALLOWED_TRANSITIONS: Final[frozenset[tuple[BacktestStatus, BacktestStatus]]] = frozenset(
    {
        (BacktestStatus.IN_PROGRESS, BacktestStatus.COMPLETE),
        (BacktestStatus.IN_PROGRESS, BacktestStatus.FAILED),
    }
)
"""Closed set of legal status transitions for ``backtest_runs.status``.

Once a row reaches ``complete`` or ``failed`` it is frozen — append-only per
REQ-CB-PERSIST-001."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _dumps_canonical(value: Mapping[str, Any] | Sequence[Any] | None) -> str | None:
    """Serialise *value* to canonical JSON or return ``None`` for ``None``.

    ``sort_keys=True`` and the compact separator tuple guarantee a stable
    byte representation across platforms / Python builds, mirroring
    :mod:`razor_rooster.calibration_backtest.run_id`.
    """
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    """JSON encoder fallback for :class:`datetime` and similar."""
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serialisable")


def _loads_optional(value: Any) -> Any:
    """Deserialise a JSON string from DuckDB.

    DuckDB returns JSON columns as ``str``; when the column is nullable and
    the row stored ``NULL`` the driver returns Python ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    # DuckDB occasionally returns parsed values directly; pass through.
    return value


def _wrap_db_error(action: str, exc: BaseException) -> BacktestPersistenceError:
    """Convert a DuckDB driver exception to a typed subsystem error."""
    return BacktestPersistenceError(f"{action}: {exc}")


# ---------------------------------------------------------------------------
# backtest_runs — insert + update + fetch
# ---------------------------------------------------------------------------


_RUN_INSERT_SQL: Final[str] = (
    f"INSERT INTO {TABLE_RUNS} ("
    "run_id, since_ts, until_ts, lag_days, "
    "class_ids_json, sectors_json, venues_json, "
    "library_version, system_revision, "
    "started_at, completed_at, status, error_summary, "
    "predictions_total, predictions_scored, predictions_skipped, "
    "overall_brier, summary_json, "
    "bin_count_global, bin_count_per_sector_json, "
    "fallback_polarity_count, allow_recent, disclaimer_version"
    ") VALUES ("
    "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
    ")"
)


_RUN_SELECT_COLUMNS: Final[str] = (
    "run_id, since_ts, until_ts, lag_days, "
    "class_ids_json, sectors_json, venues_json, "
    "library_version, system_revision, "
    "started_at, completed_at, status, error_summary, "
    "predictions_total, predictions_scored, predictions_skipped, "
    "overall_brier, summary_json, "
    "bin_count_global, bin_count_per_sector_json, "
    "fallback_polarity_count, allow_recent, disclaimer_version"
)


def insert_run(conn: duckdb.DuckDBPyConnection, run: BacktestRun) -> None:
    """Insert a single ``backtest_runs`` row.

    Raises :class:`BacktestPersistenceError` if the row already exists
    (PK violation) or any other DB-level failure occurs. Callers that
    want idempotent semantics should consult :func:`fetch_run_status`
    first (REQ-CB-RUN-004).
    """
    params: list[Any] = [
        run.run_id,
        run.since_ts,
        run.until_ts,
        run.lag_days,
        _dumps_canonical(list(run.class_ids)),
        _dumps_canonical(list(run.sectors)),
        _dumps_canonical(list(run.venues)),
        run.library_version,
        run.system_revision,
        run.started_at,
        run.completed_at,
        str(run.status),
        run.error_summary,
        run.predictions_total,
        run.predictions_scored,
        run.predictions_skipped,
        run.overall_brier,
        _dumps_canonical(run.summary_json),
        run.bin_count_global,
        _dumps_canonical(dict(run.bin_count_per_sector)),
        run.fallback_polarity_count,
        run.allow_recent,
        run.disclaimer_version,
    ]
    try:
        conn.execute(_RUN_INSERT_SQL, params)
    except duckdb.Error as exc:
        raise _wrap_db_error(f"insert_run({run.run_id!r}) failed", exc) from exc


def update_run_status(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    status: BacktestStatus,
    *,
    completed_at: datetime | None = None,
    error_summary: str | None = None,
    summary_json: Mapping[str, Any] | None = None,
    predictions_total: int | None = None,
    predictions_scored: int | None = None,
    predictions_skipped: int | None = None,
    overall_brier: float | None = None,
    fallback_polarity_count: int | None = None,
) -> None:
    """Apply the lone allowed mutation on ``backtest_runs``.

    Only ``in_progress`` -> ``complete`` and ``in_progress`` -> ``failed``
    are accepted; any other transition (including no-op self-transitions)
    raises :class:`BacktestPersistenceError`. The current status is read
    first inside the same connection so the transition check is honest
    even on connections without explicit transactions.

    Optional fields default to ``None`` and leave the corresponding
    columns untouched — callers patch only the counters they have.
    """
    current = fetch_run_status(conn, run_id)
    if current is None:
        raise BacktestPersistenceError(f"update_run_status({run_id!r}): run does not exist")
    if (current, status) not in _ALLOWED_TRANSITIONS:
        raise BacktestPersistenceError(
            f"update_run_status({run_id!r}): disallowed transition {current!s} -> {status!s}"
        )

    set_clauses: list[str] = ["status = ?"]
    params: list[Any] = [str(status)]
    if completed_at is not None:
        set_clauses.append("completed_at = ?")
        params.append(completed_at)
    if error_summary is not None:
        set_clauses.append("error_summary = ?")
        params.append(error_summary)
    if summary_json is not None:
        set_clauses.append("summary_json = ?")
        params.append(_dumps_canonical(summary_json))
    if predictions_total is not None:
        set_clauses.append("predictions_total = ?")
        params.append(predictions_total)
    if predictions_scored is not None:
        set_clauses.append("predictions_scored = ?")
        params.append(predictions_scored)
    if predictions_skipped is not None:
        set_clauses.append("predictions_skipped = ?")
        params.append(predictions_skipped)
    if overall_brier is not None:
        set_clauses.append("overall_brier = ?")
        params.append(overall_brier)
    if fallback_polarity_count is not None:
        set_clauses.append("fallback_polarity_count = ?")
        params.append(fallback_polarity_count)

    sql = f"UPDATE {TABLE_RUNS} SET {', '.join(set_clauses)} WHERE run_id = ? AND status = ?"
    params.append(run_id)
    params.append(str(current))

    try:
        conn.execute(sql, params)
    except duckdb.Error as exc:
        raise _wrap_db_error(f"update_run_status({run_id!r}, {status!s}) failed", exc) from exc


def complete_run(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    status: BacktestStatus,
    completed_at: datetime,
    summary_json: Mapping[str, Any] | None = None,
    error_summary: str | None = None,
    predictions_total: int | None = None,
    predictions_scored: int | None = None,
    predictions_skipped: int | None = None,
    overall_brier: float | None = None,
    fallback_polarity_count: int | None = None,
) -> None:
    """Apply the terminal ``in_progress -> complete | failed`` transition.

    Thin wrapper over :func:`update_run_status` that pins the call shape
    used by the replay loop (T-CB-019, design §3.5): the orchestrator
    invokes ``complete_run(run_id, summary_json, status='complete')``
    on the success path and ``complete_run(run_id, status='failed',
    error_summary=str(exc))`` on the uncaught-exception path.

    The accepted statuses are restricted to the two terminal values
    (:data:`BacktestStatus.COMPLETE` and :data:`BacktestStatus.FAILED`);
    any other value raises :class:`BacktestPersistenceError` before
    touching the row. Optional counter / summary fields default to
    ``None`` and leave the corresponding columns untouched, matching
    :func:`update_run_status` semantics.
    """
    if status not in (BacktestStatus.COMPLETE, BacktestStatus.FAILED):
        raise BacktestPersistenceError(
            f"complete_run({run_id!r}): status must be COMPLETE or FAILED, got {status!s}"
        )
    update_run_status(
        conn,
        run_id,
        status,
        completed_at=completed_at,
        error_summary=error_summary,
        summary_json=summary_json,
        predictions_total=predictions_total,
        predictions_scored=predictions_scored,
        predictions_skipped=predictions_skipped,
        overall_brier=overall_brier,
        fallback_polarity_count=fallback_polarity_count,
    )


def persist_score_summary(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    summary: ScoreSummary,
    *,
    completed_at: datetime,
    predictions_total: int | None = None,
    predictions_scored: int | None = None,
    predictions_skipped: int | None = None,
) -> None:
    """Apply the success-path completion using a :class:`ScoreSummary`.

    Thin convenience wrapper around :func:`complete_run` that funnels a
    :class:`ScoreSummary` (T-CB-023) into the canonical
    ``backtest_runs.summary_json`` payload alongside the aggregate
    counters carried on the same row (``overall_brier``,
    ``fallback_polarity_count``). The row is transitioned
    ``in_progress -> complete``.

    The summary mapping is produced by :meth:`ScoreSummary.as_mapping`,
    which sorts every dict so :func:`_dumps_canonical` (which also runs
    ``sort_keys=True``) emits a byte-identical payload regardless of
    insertion order — preserving the determinism gate locked at
    REQ-CB-PERSIST-001.

    Optional counters default to ``None`` and leave the corresponding
    columns untouched, matching :func:`complete_run` semantics so
    callers can patch only the values they hold.
    """
    complete_run(
        conn,
        run_id,
        status=BacktestStatus.COMPLETE,
        completed_at=completed_at,
        summary_json=summary.as_mapping(),
        predictions_total=predictions_total,
        predictions_scored=predictions_scored,
        predictions_skipped=predictions_skipped,
        overall_brier=summary.overall_brier,
        fallback_polarity_count=summary.fallback_polarity_count,
    )


def fetch_run(conn: duckdb.DuckDBPyConnection, run_id: str) -> BacktestRun | None:
    """Return the ``backtest_runs`` row for *run_id* hydrated as :class:`BacktestRun`.

    Returns ``None`` if no row exists.
    """
    try:
        row = conn.execute(
            f"SELECT {_RUN_SELECT_COLUMNS} FROM {TABLE_RUNS} WHERE run_id = ? LIMIT 1",
            [run_id],
        ).fetchone()
    except duckdb.Error as exc:
        raise _wrap_db_error(f"fetch_run({run_id!r}) failed", exc) from exc
    if row is None:
        return None
    return _row_to_run(row)


def run_exists(conn: duckdb.DuckDBPyConnection, run_id: str) -> bool:
    """Return ``True`` iff a ``backtest_runs`` row with *run_id* exists."""
    try:
        row = conn.execute(
            f"SELECT 1 FROM {TABLE_RUNS} WHERE run_id = ? LIMIT 1",
            [run_id],
        ).fetchone()
    except duckdb.Error as exc:
        raise _wrap_db_error(f"run_exists({run_id!r}) failed", exc) from exc
    return row is not None


def fetch_run_status(conn: duckdb.DuckDBPyConnection, run_id: str) -> BacktestStatus | None:
    """Return the current ``status`` for *run_id*, or ``None`` if absent.

    The replay orchestrator calls this before deciding whether to start a
    fresh run (REQ-CB-RUN-004): a ``COMPLETE`` row short-circuits replay
    and returns the cached summary; ``IN_PROGRESS`` or ``FAILED`` requires
    operator action.
    """
    try:
        row = conn.execute(
            f"SELECT status FROM {TABLE_RUNS} WHERE run_id = ? LIMIT 1",
            [run_id],
        ).fetchone()
    except duckdb.Error as exc:
        raise _wrap_db_error(f"fetch_run_status({run_id!r}) failed", exc) from exc
    if row is None:
        return None
    return BacktestStatus(str(row[0]))


def list_runs(
    conn: duckdb.DuckDBPyConnection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[BacktestRun, ...]:
    """Return ``backtest_runs`` rows ordered by ``started_at DESC``.

    *since* and *until* filter on ``started_at`` (inclusive lower, exclusive
    upper bound) so callers can scope to a recent window. *limit* caps the
    result size; a value of ``0`` yields no rows. *offset* skips that many
    rows from the start of the ordered result so callers can paginate
    (`limit=N`, `offset=k*N` returns page ``k``). Negative *limit* or
    *offset* values raise :class:`BacktestPersistenceError`.
    """
    if limit < 0:
        raise BacktestPersistenceError(f"list_runs: limit must be >= 0, got {limit!r}")
    if offset < 0:
        raise BacktestPersistenceError(f"list_runs: offset must be >= 0, got {offset!r}")
    sql = f"SELECT {_RUN_SELECT_COLUMNS} FROM {TABLE_RUNS}"
    where: list[str] = []
    params: list[Any] = []
    if since is not None:
        where.append("started_at >= ?")
        params.append(since)
    if until is not None:
        where.append("started_at < ?")
        params.append(until)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
    params.append(int(limit))
    params.append(int(offset))
    try:
        rows = conn.execute(sql, params).fetchall()
    except duckdb.Error as exc:
        raise _wrap_db_error("list_runs failed", exc) from exc
    return tuple(_row_to_run(row) for row in rows)


# ---------------------------------------------------------------------------
# backtest_predictions — insert (single + batch) + fetch
# ---------------------------------------------------------------------------


_PREDICTION_INSERT_SQL: Final[str] = (
    f"INSERT INTO {TABLE_PREDICTIONS} ("
    "run_id, prediction_id, class_id, condition_id, venue, sector, "
    "prediction_ts, resolution_ts, model_p, observed, "
    "polarity, polarity_source, mapping_mismatch_warning, "
    "definition_version, status, skip_reason, brier_contribution"
    ") VALUES ("
    "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
    ")"
)


_PREDICTION_SELECT_COLUMNS: Final[str] = (
    "run_id, prediction_id, class_id, condition_id, venue, sector, "
    "prediction_ts, resolution_ts, model_p, observed, "
    "polarity, polarity_source, mapping_mismatch_warning, "
    "definition_version, status, skip_reason, brier_contribution"
)


def _prediction_params(prediction: BacktestPrediction) -> list[Any]:
    """Convert a :class:`BacktestPrediction` to positional parameters."""
    return [
        prediction.run_id,
        prediction.prediction_id,
        prediction.class_id,
        prediction.condition_id,
        prediction.venue,
        prediction.sector,
        prediction.prediction_ts,
        prediction.resolution_ts,
        prediction.model_p,
        prediction.observed,
        str(prediction.polarity) if prediction.polarity is not None else None,
        str(prediction.polarity_source),
        prediction.mapping_mismatch_warning,
        prediction.definition_version,
        str(prediction.status),
        str(prediction.skip_reason) if prediction.skip_reason is not None else None,
        prediction.brier_contribution,
    ]


def insert_prediction(conn: duckdb.DuckDBPyConnection, prediction: BacktestPrediction) -> None:
    """Insert a single ``backtest_predictions`` row."""
    try:
        conn.execute(_PREDICTION_INSERT_SQL, _prediction_params(prediction))
    except duckdb.Error as exc:
        raise _wrap_db_error(
            f"insert_prediction({prediction.run_id!r}, {prediction.prediction_id!r}) failed",
            exc,
        ) from exc


def insert_predictions_batch(
    conn: duckdb.DuckDBPyConnection,
    predictions: Sequence[BacktestPrediction],
) -> None:
    """Bulk-insert a sequence of predictions via ``executemany``.

    No-op when *predictions* is empty so callers do not need to short-circuit.
    """
    if not predictions:
        return
    rows = [_prediction_params(p) for p in predictions]
    try:
        conn.executemany(_PREDICTION_INSERT_SQL, rows)
    except duckdb.Error as exc:
        run_ids = sorted({p.run_id for p in predictions})
        raise _wrap_db_error(
            f"insert_predictions_batch(run_ids={run_ids!r}, n={len(predictions)}) failed",
            exc,
        ) from exc


def fetch_predictions(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> tuple[BacktestPrediction, ...]:
    """Return all predictions for *run_id* ordered by ``prediction_id``.

    Empty tuple when no predictions exist or the run is absent.
    """
    try:
        rows = conn.execute(
            f"SELECT {_PREDICTION_SELECT_COLUMNS} FROM {TABLE_PREDICTIONS} "
            f"WHERE run_id = ? ORDER BY prediction_id ASC",
            [run_id],
        ).fetchall()
    except duckdb.Error as exc:
        raise _wrap_db_error(f"fetch_predictions({run_id!r}) failed", exc) from exc
    return tuple(_row_to_prediction(row) for row in rows)


def _build_prediction_filter_clauses(
    run_id: str,
    status: PredictionStatus | None,
    skip_reason: SkipReason | None,
) -> tuple[str, list[Any]]:
    """Compose the shared ``WHERE`` clause used by list/count predictions.

    Returns ``(where_sql, params)`` where ``where_sql`` either is empty or
    starts with ``" WHERE "``. Enum filters are stringified via ``str(...)``
    to mirror :func:`_prediction_params`, which persists ``status`` and
    ``skip_reason`` as their canonical string values.
    """
    where: list[str] = ["run_id = ?"]
    params: list[Any] = [run_id]
    if status is not None:
        where.append("status = ?")
        params.append(str(status))
    if skip_reason is not None:
        where.append("skip_reason = ?")
        params.append(str(skip_reason))
    return " WHERE " + " AND ".join(where), params


def list_predictions(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    status: PredictionStatus | None = None,
    skip_reason: SkipReason | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[BacktestPrediction, ...]:
    """Return a paginated, optionally filtered slice of predictions for *run_id*.

    The result is ordered by ``prediction_id ASC`` so pagination is
    deterministic across calls (matching :func:`fetch_predictions`).
    *status* and *skip_reason* narrow the result via additional ``WHERE``
    clauses; passing ``None`` (the default) leaves the corresponding
    column unfiltered. *limit* caps the page size and *offset* skips that
    many rows from the start of the ordered result. A ``limit`` of ``0``
    yields an empty tuple. Negative *limit* or *offset* values raise
    :class:`BacktestPersistenceError`.
    """
    if limit < 0:
        raise BacktestPersistenceError(f"list_predictions: limit must be >= 0, got {limit!r}")
    if offset < 0:
        raise BacktestPersistenceError(f"list_predictions: offset must be >= 0, got {offset!r}")
    where_sql, params = _build_prediction_filter_clauses(run_id, status, skip_reason)
    sql = (
        f"SELECT {_PREDICTION_SELECT_COLUMNS} FROM {TABLE_PREDICTIONS}"
        f"{where_sql} ORDER BY prediction_id ASC LIMIT ? OFFSET ?"
    )
    params.append(int(limit))
    params.append(int(offset))
    try:
        rows = conn.execute(sql, params).fetchall()
    except duckdb.Error as exc:
        raise _wrap_db_error(f"list_predictions({run_id!r}) failed", exc) from exc
    return tuple(_row_to_prediction(row) for row in rows)


def count_predictions(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    status: PredictionStatus | None = None,
    skip_reason: SkipReason | None = None,
) -> int:
    """Return the number of predictions for *run_id* matching the filters.

    Mirrors the filtering surface of :func:`list_predictions` so callers
    can render "Page N of M" alongside a paged table without rebuilding
    the query themselves. Returns ``0`` when no predictions match (or the
    run is absent).
    """
    where_sql, params = _build_prediction_filter_clauses(run_id, status, skip_reason)
    sql = f"SELECT COUNT(*) FROM {TABLE_PREDICTIONS}{where_sql}"
    try:
        row = conn.execute(sql, params).fetchone()
    except duckdb.Error as exc:
        raise _wrap_db_error(f"count_predictions({run_id!r}) failed", exc) from exc
    if row is None:
        return 0
    return int(row[0])


# ---------------------------------------------------------------------------
# backtest_traces — insert + fetch
# ---------------------------------------------------------------------------


_TRACE_INSERT_SQL: Final[str] = (
    f"INSERT INTO {TABLE_TRACES} ("
    "run_id, prediction_id, trace_json_compressed, "
    "compression_algorithm, decompressed_size_bytes"
    ") VALUES (?, ?, ?, ?, ?)"
)


def insert_trace(conn: duckdb.DuckDBPyConnection, trace: BacktestTrace) -> None:
    """Insert a ``backtest_traces`` row containing the compressed payload."""
    params: list[Any] = [
        trace.run_id,
        trace.prediction_id,
        trace.trace_json_compressed,
        str(trace.compression_algorithm),
        trace.decompressed_size_bytes,
    ]
    try:
        conn.execute(_TRACE_INSERT_SQL, params)
    except duckdb.Error as exc:
        raise _wrap_db_error(
            f"insert_trace({trace.run_id!r}, {trace.prediction_id!r}) failed",
            exc,
        ) from exc


def fetch_trace(
    conn: duckdb.DuckDBPyConnection, run_id: str, prediction_id: str
) -> BacktestTrace | None:
    """Return the trace for ``(run_id, prediction_id)`` or ``None`` if absent."""
    try:
        row = conn.execute(
            f"SELECT run_id, prediction_id, trace_json_compressed, "
            f"compression_algorithm, decompressed_size_bytes "
            f"FROM {TABLE_TRACES} WHERE run_id = ? AND prediction_id = ? LIMIT 1",
            [run_id, prediction_id],
        ).fetchone()
    except duckdb.Error as exc:
        raise _wrap_db_error(f"fetch_trace({run_id!r}, {prediction_id!r}) failed", exc) from exc
    if row is None:
        return None
    return BacktestTrace(
        run_id=str(row[0]),
        prediction_id=str(row[1]),
        trace_json_compressed=bytes(row[2]),
        compression_algorithm=CompressionAlgorithm(str(row[3])),
        decompressed_size_bytes=int(row[4]),
    )


# ---------------------------------------------------------------------------
# Maintenance — prune (transactional, dependency-ordered)
# ---------------------------------------------------------------------------
#
# REQ-CB-PERSIST-001 forbids destructive mutations from the **scoring**
# code path (status transitions, prediction inserts, etc.) — every row
# the orchestrator writes is append-only. ``prune_run`` is the lone
# operator-driven maintenance helper used by the ``calibration-backtest
# prune`` CLI subcommand to expunge obsolete runs after a library bump
# or disk-pressure event (design §3.9).
#
# Schemas.py intentionally OMITS foreign keys — DuckDB's FK support is
# limited and the project's exemplar persistence layers all rely on
# application-level integrity. As a consequence, "cascade delete" must
# be implemented in user-space: this function issues three DELETE
# statements in dependency order (children first), wrapped in a single
# transaction so a mid-flight failure either commits all three or
# leaves the database completely untouched (no orphan trace /
# prediction rows pointing at a vanished ``backtest_runs`` row).
#
# DuckDB's ``with conn:`` context manager closes the connection on
# exit (rather than committing), so the transaction boundary is
# expressed via explicit ``BEGIN TRANSACTION`` / ``COMMIT`` /
# ``ROLLBACK`` mirroring ``data_ingest.persistence.migrations``.


_PRUNE_TABLES_ORDERED: Final[tuple[str, ...]] = (
    TABLE_TRACES,
    TABLE_PREDICTIONS,
    TABLE_RUNS,
)
"""Child-first ordering used by :func:`prune_run`.

Traces and predictions hold ``run_id`` references, so they must be
deleted before the parent row in ``backtest_runs`` to honour the
"no orphan rows" invariant even on databases that do enforce FKs."""


def prune_run(conn: duckdb.DuckDBPyConnection, run_id: str) -> dict[str, int]:
    """Delete *run_id* and its descendants in dependency order.

    Issues three ``DELETE`` statements wrapped in a single transaction
    (children first):

    1. ``DELETE FROM backtest_traces       WHERE run_id = ?``
    2. ``DELETE FROM backtest_predictions  WHERE run_id = ?``
    3. ``DELETE FROM backtest_runs         WHERE run_id = ?``

    On the success path the transaction is committed and the row-count
    dict ``{'traces': N, 'predictions': N, 'runs': N}`` is returned so
    the CLI can print a deterministic summary. On any DuckDB driver
    error the transaction is rolled back and the failure is re-raised
    as :class:`BacktestPersistenceError` — the database is left in its
    pre-call state with no orphan trace / prediction rows.

    Calling :func:`prune_run` with a *run_id* that does not exist is
    intentionally a silent no-op: every count is ``0``, no error is
    raised. This matches the idempotent-prune contract used by the
    CLI's ``--before ISO --confirm`` flow (T-CB-032), where multiple
    operators may concurrently target the same stale window.
    """
    counts: dict[str, int] = {"traces": 0, "predictions": 0, "runs": 0}
    try:
        conn.execute("BEGIN TRANSACTION")
        try:
            traces_deleted = conn.execute(
                f"DELETE FROM {TABLE_TRACES} WHERE run_id = ?",
                [run_id],
            ).fetchone()
            counts["traces"] = int(traces_deleted[0]) if traces_deleted is not None else 0
            predictions_deleted = conn.execute(
                f"DELETE FROM {TABLE_PREDICTIONS} WHERE run_id = ?",
                [run_id],
            ).fetchone()
            counts["predictions"] = (
                int(predictions_deleted[0]) if predictions_deleted is not None else 0
            )
            runs_deleted = conn.execute(
                f"DELETE FROM {TABLE_RUNS} WHERE run_id = ?",
                [run_id],
            ).fetchone()
            counts["runs"] = int(runs_deleted[0]) if runs_deleted is not None else 0
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    except duckdb.Error as exc:
        raise _wrap_db_error(f"prune_run({run_id!r}) failed", exc) from exc
    return counts


# ---------------------------------------------------------------------------
# Row reconstruction helpers
# ---------------------------------------------------------------------------


def _row_to_run(row: tuple[Any, ...]) -> BacktestRun:
    class_ids = _loads_optional(row[4]) or []
    sectors = _loads_optional(row[5]) or []
    venues = _loads_optional(row[6]) or []
    summary_json = _loads_optional(row[17])
    bin_count_per_sector = _loads_optional(row[19]) or {}
    return BacktestRun(
        run_id=str(row[0]),
        since_ts=row[1],
        until_ts=row[2],
        lag_days=int(row[3]),
        class_ids=tuple(str(c) for c in class_ids),
        sectors=tuple(str(s) for s in sectors),
        venues=tuple(str(v) for v in venues),
        library_version=int(row[7]),
        system_revision=str(row[8]),
        started_at=row[9],
        completed_at=row[10],
        status=BacktestStatus(str(row[11])),
        error_summary=(str(row[12]) if row[12] is not None else None),
        predictions_total=int(row[13]),
        predictions_scored=int(row[14]),
        predictions_skipped=int(row[15]),
        overall_brier=(float(row[16]) if row[16] is not None else None),
        summary_json=(summary_json if isinstance(summary_json, Mapping) else None),
        bin_count_global=int(row[18]),
        bin_count_per_sector={str(k): int(v) for k, v in dict(bin_count_per_sector).items()},
        fallback_polarity_count=int(row[20]),
        allow_recent=bool(row[21]),
        disclaimer_version=str(row[22]),
    )


def _row_to_prediction(row: tuple[Any, ...]) -> BacktestPrediction:
    polarity_raw = row[10]
    skip_reason_raw = row[15]
    return BacktestPrediction(
        run_id=str(row[0]),
        prediction_id=str(row[1]),
        class_id=str(row[2]),
        condition_id=str(row[3]),
        venue=str(row[4]),
        sector=str(row[5]),
        prediction_ts=row[6],
        resolution_ts=row[7],
        model_p=(float(row[8]) if row[8] is not None else None),
        observed=(float(row[9]) if row[9] is not None else None),
        polarity=(PolarityValue(str(polarity_raw)) if polarity_raw is not None else None),
        polarity_source=PolaritySource(str(row[11])),
        mapping_mismatch_warning=bool(row[12]),
        definition_version=int(row[13]),
        status=PredictionStatus(str(row[14])),
        skip_reason=(SkipReason(str(skip_reason_raw)) if skip_reason_raw is not None else None),
        brier_contribution=(float(row[16]) if row[16] is not None else None),
    )


__all__ = [
    "complete_run",
    "count_predictions",
    "fetch_predictions",
    "fetch_run",
    "fetch_run_status",
    "fetch_trace",
    "insert_prediction",
    "insert_predictions_batch",
    "insert_run",
    "insert_trace",
    "list_predictions",
    "list_runs",
    "persist_score_summary",
    "prune_run",
    "run_exists",
    "update_run_status",
]
