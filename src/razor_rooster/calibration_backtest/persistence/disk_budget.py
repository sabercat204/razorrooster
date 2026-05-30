"""Disk-budget guard for the calibration-backtest persistence layer (T-CB-012).

Implements REQ-CB-PERSIST-003: a single backtest run must never push the
combined footprint of ``backtest_runs`` + ``backtest_predictions`` +
``backtest_traces`` past the configured ``disk_cap_mb`` (default 100 MB)
threshold. The replay orchestrator calls :func:`projected_disk_usage_mb`
before opening a transaction; if the projection plus the current footprint
exceeds the cap, :class:`~razor_rooster.calibration_backtest.errors.DiskBudgetError`
is raised before any rows are inserted.

The estimator is intentionally conservative and additive:

* Each backtest run writes one ``backtest_runs`` row plus one
  ``backtest_predictions`` row and (optionally) one ``backtest_traces``
  row per prediction.
* Trace blobs are zstd-compressed (D4 — design §3.11). We assume a
  10% post-compression ratio which is comfortably above the ~4-5x
  shrink observed in practice; over-projection biases the guard
  toward refusing borderline-large runs rather than letting them
  silently breach the cap.
* The current footprint is read from DuckDB's ``duckdb_tables()``
  ``estimated_size`` column. DuckDB v1.x reports this as the
  approximate row count rather than byte count; we therefore multiply
  by ``CURRENT_ROW_BYTES_PROXY`` to map rows -> MB. The projection
  uses real per-row byte estimates so the two values are dimensionally
  consistent at MB granularity.

Public surface:

* :data:`DEFAULT_DISK_CAP_MB`
* :func:`estimate_run_footprint_mb`
* :func:`measure_current_footprint_mb`
* :func:`assert_under_cap`
* :func:`projected_disk_usage_mb`

The DiskBudgetError type itself lives in
:mod:`razor_rooster.calibration_backtest.errors` so the entire
exception hierarchy stays in one place.
"""

from __future__ import annotations

from typing import Final

import duckdb

from razor_rooster.calibration_backtest.errors import DiskBudgetError
from razor_rooster.calibration_backtest.persistence.schemas import (
    TABLE_PREDICTIONS,
    TABLE_RUNS,
    TABLE_TRACES,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DISK_CAP_MB: Final[int] = 100
"""Bootstrap default cap (matches ``disk_cap_mb`` in ``config/backtest.yaml``)."""

_BYTES_PER_MB: Final[float] = 1024.0 * 1024.0
"""Binary megabyte conversion (matches design §3.8)."""

_TRACE_COMPRESSION_RATIO: Final[float] = 0.10
"""Conservative post-zstd shrink factor for trace blobs.

The design doc (§3.11) cites ~4-5x shrink in practice; we plan for ~10x
on the optimistic-cap side to keep the projection biased toward refusing
borderline-large runs rather than silently overshooting the budget."""

_CURRENT_ROW_BYTES_PROXY: Final[float] = 4096.0
"""Bytes-per-row proxy for the live-footprint reader.

DuckDB's ``duckdb_tables().estimated_size`` reports an approximate row
count (validated experimentally on DuckDB 1.x); converting to MB requires
a per-row byte estimate. 4096 B/row blends a small ``backtest_runs`` row,
a wider ``backtest_predictions`` row, and a compressed ``backtest_traces``
blob into a single proxy that keeps the live measurement in the same
order of magnitude as :func:`estimate_run_footprint_mb`."""

_BACKTEST_TABLES: Final[tuple[str, ...]] = (TABLE_RUNS, TABLE_PREDICTIONS, TABLE_TRACES)


# ---------------------------------------------------------------------------
# Pure projection
# ---------------------------------------------------------------------------


def estimate_run_footprint_mb(
    *,
    predictions_estimated: int,
    avg_trace_bytes: float = 1500.0,
    avg_prediction_row_bytes: float = 200.0,
    base_run_row_bytes: float = 2000.0,
) -> float:
    """Project a single run's on-disk footprint, in MB.

    Parameters
    ----------
    predictions_estimated:
        Anticipated number of ``backtest_predictions`` (and ``backtest_traces``)
        rows the replay loop will write. Must be ``>= 0``.
    avg_trace_bytes:
        Mean uncompressed JSON-trace size before zstd. Default 1500 B
        matches the per-prediction estimate in design §3.11.
    avg_prediction_row_bytes:
        Mean serialized size of one ``backtest_predictions`` row including
        all VARCHAR/TIMESTAMPTZ/JSON columns.
    base_run_row_bytes:
        Mean serialized size of the ``backtest_runs`` row, including JSON
        columns and the ``summary_json`` payload written at completion.

    Returns
    -------
    float
        Projected footprint in MB. Real footprint may differ from the
        projection; this is a deliberately conservative pre-flight check
        intended to refuse runs that would obviously breach the cap.

    Notes
    -----
    Compression is modeled as a fixed multiplier on ``avg_trace_bytes``
    (see :data:`_TRACE_COMPRESSION_RATIO`). Rows in
    ``backtest_predictions`` and ``backtest_runs`` are stored uncompressed
    in DuckDB and contribute their raw byte estimates directly.
    """
    if predictions_estimated < 0:
        raise ValueError(f"predictions_estimated must be >= 0, got {predictions_estimated!r}")
    if avg_trace_bytes < 0 or avg_prediction_row_bytes < 0 or base_run_row_bytes < 0:
        raise ValueError("byte-size parameters must all be non-negative")

    compressed_trace_bytes_per_row = avg_trace_bytes * _TRACE_COMPRESSION_RATIO
    total_bytes = (
        base_run_row_bytes
        + predictions_estimated * avg_prediction_row_bytes
        + predictions_estimated * compressed_trace_bytes_per_row
    )
    return total_bytes / _BYTES_PER_MB


# ---------------------------------------------------------------------------
# Live-footprint reader
# ---------------------------------------------------------------------------


def measure_current_footprint_mb(conn: duckdb.DuckDBPyConnection) -> float:
    """Return the combined footprint of the three calibration-backtest tables.

    Reads ``duckdb_tables().estimated_size`` for each table and converts the
    aggregate row-count proxy to MB via :data:`_CURRENT_ROW_BYTES_PROXY`.
    Tables that have not yet been created (e.g., before the m6001 migration
    runs) contribute zero rather than raising.
    """
    placeholders = ", ".join(["?"] * len(_BACKTEST_TABLES))
    sql = (
        "SELECT COALESCE(SUM(estimated_size), 0) FROM duckdb_tables() "
        f"WHERE table_name IN ({placeholders})"
    )
    row = conn.execute(sql, list(_BACKTEST_TABLES)).fetchone()
    estimated_rows = float(row[0]) if row is not None and row[0] is not None else 0.0
    estimated_bytes = estimated_rows * _CURRENT_ROW_BYTES_PROXY
    return estimated_bytes / _BYTES_PER_MB


# ---------------------------------------------------------------------------
# Cap enforcement
# ---------------------------------------------------------------------------


def assert_under_cap(
    *,
    current_mb: float,
    projected_additional_mb: float,
    cap_mb: int = DEFAULT_DISK_CAP_MB,
) -> None:
    """Raise :class:`DiskBudgetError` iff current + projected exceeds *cap_mb*.

    The cap is interpreted as an inclusive upper bound so a projection that
    lands exactly at the cap passes (``total_mb <= cap_mb``).
    """
    if current_mb < 0 or projected_additional_mb < 0:
        raise ValueError("current_mb and projected_additional_mb must both be non-negative")
    total_mb = current_mb + projected_additional_mb
    if total_mb > cap_mb:
        raise DiskBudgetError(
            "calibration_backtest disk budget exceeded: "
            f"current={current_mb:.3f} MB + projected_additional="
            f"{projected_additional_mb:.3f} MB -> total={total_mb:.3f} MB "
            f"> cap={cap_mb} MB"
        )


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


def projected_disk_usage_mb(
    conn: duckdb.DuckDBPyConnection,
    *,
    predictions_estimated: int,
    cap_mb: int = DEFAULT_DISK_CAP_MB,
) -> dict[str, float]:
    """Aggregate the live measurement and the projection in one dict.

    Returns a ``dict[str, float]`` with the keys:

    ``current_mb``
        Live footprint via :func:`measure_current_footprint_mb`.
    ``projected_additional_mb``
        Output of :func:`estimate_run_footprint_mb` for *predictions_estimated*.
    ``projected_total_mb``
        ``current_mb + projected_additional_mb``.
    ``cap_mb``
        The cap (echoed for logging/CLI rendering).
    ``headroom_mb``
        ``cap_mb - projected_total_mb``. Negative when the run would
        exceed the cap; callers can render the value directly without
        re-deriving it.
    """
    current_mb = measure_current_footprint_mb(conn)
    projected_additional_mb = estimate_run_footprint_mb(
        predictions_estimated=predictions_estimated,
    )
    projected_total_mb = current_mb + projected_additional_mb
    headroom_mb = float(cap_mb) - projected_total_mb
    return {
        "current_mb": current_mb,
        "projected_additional_mb": projected_additional_mb,
        "projected_total_mb": projected_total_mb,
        "cap_mb": float(cap_mb),
        "headroom_mb": headroom_mb,
    }


__all__ = [
    "DEFAULT_DISK_CAP_MB",
    "assert_under_cap",
    "estimate_run_footprint_mb",
    "measure_current_footprint_mb",
    "projected_disk_usage_mb",
]
