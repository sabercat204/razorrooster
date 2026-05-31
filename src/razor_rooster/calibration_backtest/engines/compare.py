"""Run-vs-run cell-level diff (T-CB-024; design §3.7, REQ-CB-SCORE-005).

Implements :func:`compare_runs`, the single-round-trip aggregator that
emits one :class:`CompareCell` per ``(sector, class_id)`` cell observed
in either of two backtest runs. The engine performs a single SQL CTE
``FULL OUTER JOIN`` over ``backtest_predictions`` (filtered to
``status='scored'`` rows with non-null ``brier_contribution``) so the
aggregation parity with T-CB-023's per-sector / per-class Brier is
exact: a self-compare (``run_a_id == run_b_id``) returns
``delta_absolute = 0.0`` for every cell where ``brier_a > 0`` and
``delta_percent = 0.0`` for the same cells (``None`` when ``brier_a ==
0`` to guard against the division-by-zero edge case).

The miscalibration-threshold flag is computed in Python rather than SQL
so callers can override the threshold per-call without re-issuing the
aggregate query. The default threshold is read from
``config/backtest.yaml`` (``compare.brier_miscalibration_threshold``).
The fallback module-level constant
:data:`DEFAULT_BRIER_MISCALIBRATION_THRESHOLD` mirrors the on-disk
value so the in-Python default and the YAML never drift.

Ranking belongs to T-CB-025 (:func:`rank_compare_cells`); this module
returns cells in deterministic ``(sector, class_id)`` ASCII order
matching the SQL ``ORDER BY`` clause.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Literal

import duckdb

from razor_rooster.calibration_backtest.errors import (
    BacktestConfigError,
    BacktestPersistenceError,
)
from razor_rooster.calibration_backtest.models import CompareCell, PresentIn
from razor_rooster.calibration_backtest.persistence.schemas import TABLE_PREDICTIONS

# ---------------------------------------------------------------------------
# Threshold loader (mirrors the ``max_workers`` pattern in replay.py)
# ---------------------------------------------------------------------------

DEFAULT_BRIER_MISCALIBRATION_THRESHOLD: Final[float] = 0.25
"""Fallback threshold when ``config/backtest.yaml`` is unavailable.

Mirrors the ``compare.brier_miscalibration_threshold: 0.25`` line in
``backtest.yaml`` so the in-Python default and the on-disk config never
drift. The compare engine reads this constant when *threshold* is
omitted; T-CB-026 will fold the value into the unified
``BacktestConfig`` loader.
"""

_BACKTEST_YAML_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "config" / "backtest.yaml"
)

_THRESHOLD_KEY: Final[str] = "brier_miscalibration_threshold"
"""YAML key under the ``compare:`` section."""


def _load_default_threshold() -> float:
    """Return ``compare.brier_miscalibration_threshold`` or the module default.

    Parses the tiny ``backtest.yaml`` without pulling a dependency on
    PyYAML — the file is a flat ``key: value`` list with a single
    nested ``compare:`` section in v1, and line-aware parsing is
    sufficient. Any parse failure falls back to
    :data:`DEFAULT_BRIER_MISCALIBRATION_THRESHOLD` rather than raising;
    the compare engine is robust to a missing or malformed file by
    design (the default mirrors the on-disk value anyway).

    The full config loader lands in T-CB-026; this helper keeps the
    threshold resolution narrow and self-contained.
    """
    try:
        text = _BACKTEST_YAML_PATH.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_BRIER_MISCALIBRATION_THRESHOLD
    in_compare_section = False
    for raw_line in text.splitlines():
        # Strip trailing comments but preserve indentation so we can
        # detect when a sibling top-level key terminates the
        # ``compare:`` block.
        body = raw_line.split("#", 1)[0].rstrip()
        if not body.strip():
            continue
        is_indented = body[0] in (" ", "\t")
        stripped = body.strip()
        if not is_indented:
            in_compare_section = stripped == "compare:"
            continue
        if not in_compare_section or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        if key.strip() != _THRESHOLD_KEY:
            continue
        try:
            parsed = float(value.strip())
        except ValueError:
            return DEFAULT_BRIER_MISCALIBRATION_THRESHOLD
        if parsed < 0.0:
            return DEFAULT_BRIER_MISCALIBRATION_THRESHOLD
        return parsed
    return DEFAULT_BRIER_MISCALIBRATION_THRESHOLD


# ---------------------------------------------------------------------------
# Compare query (single round-trip CTE, design §3.7)
# ---------------------------------------------------------------------------

# The SQL mirrors the design doc exactly. The ``FILTER`` clause
# duplicates the WHERE predicate so the aggregation matches T-CB-023's
# per-sector / per-class Brier (self-compare deltas must be zero).
# DuckDB's FULL OUTER JOIN coalesces the (sector, class_id) tuple via
# COALESCE; the result set is already sorted by (sector, class_id)
# ASCII for determinism.
_COMPARE_SQL: Final[str] = f"""
WITH a AS (
    SELECT sector, class_id, AVG(brier_contribution) AS brier_a
    FROM {TABLE_PREDICTIONS}
    WHERE run_id = ?
      AND status = 'scored'
      AND brier_contribution IS NOT NULL
    GROUP BY sector, class_id
),
b AS (
    SELECT sector, class_id, AVG(brier_contribution) AS brier_b
    FROM {TABLE_PREDICTIONS}
    WHERE run_id = ?
      AND status = 'scored'
      AND brier_contribution IS NOT NULL
    GROUP BY sector, class_id
)
SELECT
    COALESCE(a.sector, b.sector)       AS sector,
    COALESCE(a.class_id, b.class_id)   AS class_id,
    a.brier_a,
    b.brier_b,
    CASE
        WHEN a.brier_a IS NOT NULL AND b.brier_b IS NOT NULL
            THEN b.brier_b - a.brier_a
    END                                AS delta_absolute,
    CASE
        WHEN a.brier_a IS NOT NULL AND b.brier_b IS NOT NULL AND a.brier_a > 0
            THEN 100.0 * (b.brier_b - a.brier_a) / a.brier_a
    END                                AS delta_percent,
    CASE
        WHEN a.brier_a IS NOT NULL AND b.brier_b IS NOT NULL THEN 'both'
        WHEN a.brier_a IS NOT NULL THEN 'a_only'
        ELSE 'b_only'
    END                                AS present_in
FROM a
FULL OUTER JOIN b USING (sector, class_id)
ORDER BY sector, class_id
""".strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compare_runs(
    conn: duckdb.DuckDBPyConnection,
    run_a_id: str,
    run_b_id: str,
    *,
    threshold: float | None = None,
) -> list[CompareCell]:
    """Compare two backtest runs cell-by-cell on ``(sector, class_id)``.

    Issues a single CTE round-trip computing ``AVG(brier_contribution)``
    over ``status='scored'`` rows for each run, full-outer-joined on
    ``(sector, class_id)``. Returns one :class:`CompareCell` per cell
    observed in either run; cells in only one run carry
    :data:`PresentIn.A_ONLY` / :data:`PresentIn.B_ONLY` and ``None``
    delta fields per REQ-CB-SCORE-005.

    The miscalibration-threshold flag is computed in Python so the
    threshold can be overridden per-call. When ``threshold is None`` the
    default is read from ``config/backtest.yaml``
    (``compare.brier_miscalibration_threshold``) with module fallback
    :data:`DEFAULT_BRIER_MISCALIBRATION_THRESHOLD`. The flag fires only
    on :data:`PresentIn.BOTH` cells; asymmetric cells carry ``None``.

    Args:
        conn: an open DuckDB connection with ``backtest_predictions``
            populated for both runs.
        run_a_id: the "A" side run identifier; appears as ``brier_a``.
        run_b_id: the "B" side run identifier; appears as ``brier_b``.
        threshold: per-call override of the miscalibration threshold;
            must be non-negative when set. ``None`` falls back to the
            YAML default.

    Returns:
        A list of :class:`CompareCell`, deterministically ordered by
        ``(sector, class_id)`` ASCII. The list is empty when both runs
        have zero scored predictions.

    Raises:
        ValueError: if ``threshold`` is negative.
        BacktestPersistenceError: if the underlying SQL fails.
    """
    if threshold is None:
        effective_threshold = _load_default_threshold()
    else:
        if threshold < 0.0:
            raise ValueError(f"compare_runs.threshold must be >= 0.0 when set, got {threshold!r}")
        effective_threshold = threshold

    try:
        rows: list[tuple[Any, ...]] = conn.execute(_COMPARE_SQL, [run_a_id, run_b_id]).fetchall()
    except duckdb.Error as exc:
        raise BacktestPersistenceError(
            f"compare_runs({run_a_id!r}, {run_b_id!r}) failed: {exc}"
        ) from exc

    cells: list[CompareCell] = []
    for row in rows:
        sector, class_id, brier_a, brier_b, delta_absolute, delta_percent, present_in_str = row
        present_in = PresentIn(present_in_str)
        crossed: bool | None
        if present_in is PresentIn.BOTH:
            # ``delta_absolute`` is non-null for BOTH rows by construction
            # of the CASE expression; the assertion narrows the type for
            # mypy while documenting the SQL contract.
            assert delta_absolute is not None
            crossed = abs(float(delta_absolute)) >= effective_threshold
        else:
            crossed = None
        cells.append(
            CompareCell(
                sector=str(sector),
                class_id=str(class_id),
                brier_a=float(brier_a) if brier_a is not None else None,
                brier_b=float(brier_b) if brier_b is not None else None,
                delta_absolute=(float(delta_absolute) if delta_absolute is not None else None),
                delta_percent=(float(delta_percent) if delta_percent is not None else None),
                crossed_miscalibration_threshold=crossed,
                present_in=present_in,
                trace_diff_summary=None,
            )
        )
    return cells


# ---------------------------------------------------------------------------
# Ranking (T-CB-025; design §3.7, REQ-CB-SCORE-005)
# ---------------------------------------------------------------------------


# Closed enumeration of supported ``rank_by`` modes. Surfaced as a public
# ``Literal`` so the CLI surface (Phase 5) can reuse the same type for its
# Click choice without redeclaring the values.
RankBy = Literal["absolute", "percent"]


def rank_compare_cells(
    cells: list[CompareCell],
    *,
    rank_by: RankBy,
) -> list[CompareCell]:
    """Return ``cells`` sorted by the requested delta magnitude (T-CB-025).

    Sort order is stable and descending by the chosen delta magnitude:

    * ``rank_by='absolute'``: primary key ``abs(delta_absolute)``
      descending. Cells with ``present_in != PresentIn.BOTH`` (where
      ``delta_absolute is None`` by construction) sort to the bottom in
      stable input order.
    * ``rank_by='percent'``: primary key ``abs(delta_percent)``
      descending. Cells with ``delta_percent is None`` (asymmetric cells
      *or* both-present cells whose ``brier_a == 0`` triggers the
      division-by-zero guard) sort to the bottom in stable input order.

    Tie-stability is preserved across both modes via Python's stable
    :func:`sorted`. Two cells with identical magnitudes retain their
    relative order from the input list.

    None handling never raises ``TypeError``: the sort key tuple uses a
    boolean "is None" discriminator as the primary axis so ``None``
    values are partitioned to the bottom before any ``abs()``/negation
    is computed. The fallback ``-0.0`` magnitude is only ever evaluated
    when the cell already qualifies for the bottom partition.

    Args:
        cells: The :class:`CompareCell` list emitted by
            :func:`compare_runs`. Mutated only via ``sorted``; the input
            list is not modified.
        rank_by: ``'absolute'`` or ``'percent'`` per design §3.7. Any
            other value raises :class:`BacktestConfigError`.

    Returns:
        A new ``list[CompareCell]`` (the input list is not mutated)
        ordered by the requested magnitude descending.

    Raises:
        BacktestConfigError: If ``rank_by`` is not one of the supported
            values.
    """
    if rank_by == "absolute":
        return sorted(cells, key=_rank_key_absolute)
    if rank_by == "percent":
        return sorted(cells, key=_rank_key_percent)
    raise BacktestConfigError(
        f"rank_compare_cells: rank_by must be 'absolute' or 'percent', got {rank_by!r}"
    )


def _rank_key_absolute(cell: CompareCell) -> tuple[bool, float]:
    """Sort key for ``rank_by='absolute'`` (T-CB-025).

    Tuple semantics:

    1. ``cell.delta_absolute is None`` — asymmetric cells (or any future
       both-present cell whose absolute delta is unset) partition to the
       bottom because ``False`` < ``True``.
    2. ``-abs(delta_absolute)`` — negation flips the natural ascending
       sort to descending magnitude. The fallback ``-0.0`` is only
       reached when the cell already qualifies for the bottom partition,
       so the value is irrelevant; we still return a numeric so the
       tuple type stays ``tuple[bool, float]`` (mypy-clean, no
       ``TypeError`` on comparison).
    """
    if cell.delta_absolute is None:
        return (True, 0.0)
    return (False, -abs(cell.delta_absolute))


def _rank_key_percent(cell: CompareCell) -> tuple[bool, float]:
    """Sort key for ``rank_by='percent'`` (T-CB-025).

    Mirrors :func:`_rank_key_absolute` but keys off
    ``cell.delta_percent``. Both asymmetric cells and both-present cells
    where ``brier_a == 0`` (division-by-zero guard) carry
    ``delta_percent=None`` and partition to the bottom.
    """
    if cell.delta_percent is None:
        return (True, 0.0)
    return (False, -abs(cell.delta_percent))


__all__ = [
    "DEFAULT_BRIER_MISCALIBRATION_THRESHOLD",
    "RankBy",
    "compare_runs",
    "rank_compare_cells",
]
