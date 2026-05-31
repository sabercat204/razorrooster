"""Brier score arithmetic + reliability binning for calibration_backtest.

This module implements the closed-form Brier aggregations (T-CB-021) and
the reliability-diagram binning (T-CB-022; design §3.6) used by the
calibration_backtest scoring pipeline. The two surfaces are colocated
because both consume scored ``backtest_predictions`` rows and feed
:class:`ScoreSummary` (T-CB-023). The arithmetic here is intentionally
narrow: it covers the overall, per-sector, and per-class Brier roll-ups,
the detection of sectors and classes that produced zero scoreable
resolutions, and the per-sector reliability bins. Aggregate summary
assembly and persistence land in T-CB-023.

The reliability convention mirrors ``report_generator.engines.section_
assemblers.reliability._equal_width_bins`` bit-for-bit so the
calibration_backtest surface produces the same edges the daily report
renders: ``width = 1.0 / bin_count``; edges rounded to four decimals
via ``round(i * width, 4)``; bins are half-open ``[lo, hi)`` except the
last bin which is closed at ``1.0`` — a prediction of exactly ``1.0``
lands in the top bin, never off the end. Empty bins emit a
:class:`ReliabilityBin` with ``count=0`` and both
``mean_predicted_p=None`` and ``empirical_rate=None``. The sibling
private symbol ``_equal_width_bins`` in ``report_generator`` is **not**
imported here in production code (it has an underscore prefix and there
is no compatible public surface — see the T-CB-022 scout amendment);
the parity test in ``tests/calibration_backtest/test_reliability.py``
imports it for correctness verification only.

Surfaces exposed:

* :func:`compute_brier_overall` consumes an in-memory iterable of
  :class:`BacktestPrediction` rows. It filters to ``status='scored'`` rows
  carrying a non-``None`` ``brier_contribution`` and returns the mean of
  those contributions. An empty input list returns an explicit ``0.0`` so
  callers do not need to special-case empty backtests at every site (the
  zero-resolution counter helpers below capture the empty case
  separately for the operator-facing summary).
* :func:`compute_brier_per_sector` /
  :func:`compute_brier_per_class` execute a single ``SELECT … AVG …
  GROUP BY`` against ``backtest_predictions`` so the aggregation runs
  inside DuckDB. The amendment landed in T-CB-023 (2026-05-31) fixes
  ``AVG(brier_contribution)`` as the canonical aggregation function so the
  T-CB-024 compare engine's self-compare produces all-zero deltas.
* :func:`detect_zero_resolution_groups` returns the set of sectors and
  classes that appear in ``backtest_predictions`` for the run but have zero
  rows with ``status='scored' AND brier_contribution IS NOT NULL``. The
  result drives ``ScoreSummary.zero_resolutions_sectors`` and
  ``ScoreSummary.zero_resolutions_classes`` (REQ-CB-RENDER-002).

All SQL is parameterised with positional ``?`` placeholders to mirror the
existing persistence layer's binding style; DuckDB driver errors are
wrapped in :class:`BacktestPersistenceError` so callers can catch a single
exception type irrespective of the underlying driver. The module performs
no schema mutations and opens no transactions.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import duckdb

from razor_rooster.calibration_backtest.errors import (
    BacktestConfigError,
    BacktestPersistenceError,
)
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    PolaritySource,
    PredictionStatus,
    ReliabilityBin,
    ReliabilityDiagram,
    RunParameters,
    ScoreSummary,
)

logger = logging.getLogger(__name__)

# Module-level default mirrors report_generator's ``DEFAULT_BIN_COUNT``
# (10 equal-width bins covering ``[0.0, 1.0]``) so calibration_backtest's
# in-memory diagrams align with the daily-report rendering when no
# operator override applies (T-CB-022; design §3.6).
DEFAULT_BIN_COUNT: Final[int] = 10

# Edge rounding precision. report_generator's ``_equal_width_bins`` uses
# ``round(i * width, 4)``; we mirror that exactly to keep the determinism
# gate (T-CB-027) green and the bit-equality parity test passing.
_EDGE_DECIMALS: Final[int] = 4


_BRIER_PER_SECTOR_SQL: Final[str] = (
    "SELECT sector, AVG(brier_contribution) "
    "FROM backtest_predictions "
    "WHERE run_id = ? "
    "  AND status = 'scored' "
    "  AND brier_contribution IS NOT NULL "
    "GROUP BY sector"
)


_BRIER_PER_CLASS_SQL: Final[str] = (
    "SELECT class_id, AVG(brier_contribution) "
    "FROM backtest_predictions "
    "WHERE run_id = ? "
    "  AND status = 'scored' "
    "  AND brier_contribution IS NOT NULL "
    "GROUP BY class_id"
)


_DISTINCT_SECTORS_SQL: Final[str] = (
    "SELECT DISTINCT sector FROM backtest_predictions WHERE run_id = ?"
)


_DISTINCT_CLASSES_SQL: Final[str] = (
    "SELECT DISTINCT class_id FROM backtest_predictions WHERE run_id = ?"
)


_SCORED_SECTORS_SQL: Final[str] = (
    "SELECT DISTINCT sector "
    "FROM backtest_predictions "
    "WHERE run_id = ? "
    "  AND status = 'scored' "
    "  AND brier_contribution IS NOT NULL"
)


_SCORED_CLASSES_SQL: Final[str] = (
    "SELECT DISTINCT class_id "
    "FROM backtest_predictions "
    "WHERE run_id = ? "
    "  AND status = 'scored' "
    "  AND brier_contribution IS NOT NULL"
)


def compute_brier_overall(predictions: Iterable[BacktestPrediction]) -> float:
    """Return the mean Brier contribution over scored predictions.

    Filters *predictions* to rows with ``status == PredictionStatus.SCORED``
    and a non-``None`` ``brier_contribution``, then returns
    ``sum(brier_contribution) / count`` over that filtered set. Returns an
    explicit ``0.0`` when the filtered set is empty so callers can rely on
    a numeric return type irrespective of input cardinality (REQ-CB-SCORE-001,
    design §3.6).
    """
    contributions: list[float] = [
        prediction.brier_contribution
        for prediction in predictions
        if prediction.status is PredictionStatus.SCORED
        and prediction.brier_contribution is not None
    ]
    if not contributions:
        return 0.0
    return sum(contributions) / len(contributions)


def compute_brier_per_sector(conn: duckdb.DuckDBPyConnection, run_id: str) -> dict[str, float]:
    """Return ``{sector: AVG(brier_contribution)}`` for the run.

    Executes a single ``GROUP BY sector`` over ``backtest_predictions``
    filtered to ``status='scored' AND brier_contribution IS NOT NULL``. The
    aggregation function is fixed at ``AVG`` to match the canonical
    aggregation called out in the T-CB-023 amendment so the compare engine
    (T-CB-024) sees zero deltas on a self-compare.
    """
    try:
        rows = conn.execute(_BRIER_PER_SECTOR_SQL, [run_id]).fetchall()
    except duckdb.Error as exc:
        raise BacktestPersistenceError(
            f"compute_brier_per_sector({run_id!r}) failed: {exc}"
        ) from exc
    return {sector: float(brier) for sector, brier in rows if brier is not None}


def compute_brier_per_class(conn: duckdb.DuckDBPyConnection, run_id: str) -> dict[str, float]:
    """Return ``{class_id: AVG(brier_contribution)}`` for the run.

    Mirrors :func:`compute_brier_per_sector` but groups by ``class_id``.
    Same canonical-aggregation contract applies.
    """
    try:
        rows = conn.execute(_BRIER_PER_CLASS_SQL, [run_id]).fetchall()
    except duckdb.Error as exc:
        raise BacktestPersistenceError(
            f"compute_brier_per_class({run_id!r}) failed: {exc}"
        ) from exc
    return {class_id: float(brier) for class_id, brier in rows if brier is not None}


def detect_zero_resolution_groups(
    conn: duckdb.DuckDBPyConnection, run_id: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return sectors and classes with zero scored predictions for the run.

    Returns ``(zero_resolution_sectors, zero_resolution_classes)`` where
    each tuple is the sorted set of sector / class identifiers that appear
    in ``backtest_predictions`` for *run_id* but contribute zero rows to
    the scored aggregation (``status='scored' AND brier_contribution IS NOT
    NULL``). Sorted output is required for deterministic downstream JSON
    serialization (REQ-CB-RENDER-002).
    """
    try:
        all_sector_rows = conn.execute(_DISTINCT_SECTORS_SQL, [run_id]).fetchall()
        scored_sector_rows = conn.execute(_SCORED_SECTORS_SQL, [run_id]).fetchall()
        all_class_rows = conn.execute(_DISTINCT_CLASSES_SQL, [run_id]).fetchall()
        scored_class_rows = conn.execute(_SCORED_CLASSES_SQL, [run_id]).fetchall()
    except duckdb.Error as exc:
        raise BacktestPersistenceError(
            f"detect_zero_resolution_groups({run_id!r}) failed: {exc}"
        ) from exc
    all_sectors: set[str] = {row[0] for row in all_sector_rows}
    scored_sectors: set[str] = {row[0] for row in scored_sector_rows}
    all_classes: set[str] = {row[0] for row in all_class_rows}
    scored_classes: set[str] = {row[0] for row in scored_class_rows}
    zero_sectors = tuple(sorted(all_sectors - scored_sectors))
    zero_classes = tuple(sorted(all_classes - scored_classes))
    return zero_sectors, zero_classes


_RELIABILITY_PER_SECTOR_SQL: Final[str] = (
    "SELECT sector, model_p, observed "
    "FROM backtest_predictions "
    "WHERE run_id = ? "
    "  AND status = 'scored' "
    "  AND brier_contribution IS NOT NULL "
    "  AND model_p IS NOT NULL "
    "  AND observed IS NOT NULL"
)


def compute_bins(
    pairs: Sequence[tuple[float, int]],
    *,
    bin_count: int,
) -> ReliabilityDiagram:
    """Compute a reliability diagram from ``(model_p, observed)`` pairs.

    The convention mirrors ``report_generator.engines.section_assemblers.
    reliability._equal_width_bins`` exactly: equal-width bins covering
    ``[0.0, 1.0]`` with edges rounded to four decimals; bins are
    half-open ``[lo, hi)`` except the last bin which is closed at
    ``1.0`` so a prediction of exactly ``1.0`` lands in the top bin
    instead of falling off the end.

    Empty bins emit a :class:`ReliabilityBin` with ``count=0`` and both
    ``mean_predicted_p`` and ``empirical_rate`` set to ``None`` so the
    renderer can flag sparse cells without a separate sentinel
    enumeration (calibration_backtest's :class:`ReliabilityBin` model
    deliberately does not carry report_generator's ``sparse`` flag —
    sparsity is a rendering concern, not a model invariant).

    :param pairs: Sequence of ``(model_p, observed)`` tuples. ``model_p``
        is a probability in ``[0.0, 1.0]``; ``observed`` is ``0`` or
        ``1``. Pairs with ``model_p`` outside ``[0.0, 1.0]`` raise
        :class:`BacktestConfigError`.
    :param bin_count: Number of equal-width bins. Must be ``>= 2``;
        otherwise :class:`BacktestConfigError` is raised before the
        :class:`ReliabilityDiagram` is constructed (the loader clamps to
        ``[2, 50]`` silently — calibration_backtest must guard
        explicitly per design §3.6).
    :returns: :class:`ReliabilityDiagram` with exactly ``bin_count``
        :class:`ReliabilityBin` entries.
    :raises BacktestConfigError: If ``bin_count < 2`` or any
        ``model_p`` falls outside ``[0.0, 1.0]``.
    """
    if bin_count < 2:
        raise BacktestConfigError(f"compute_bins requires bin_count >= 2, got {bin_count!r}")

    edges = _equal_width_edges(bin_count)

    # Bucket the pairs by bin index. The top-bin inclusive rule is
    # encoded in :func:`_assign_bin_index` so the index assignment and
    # the rounded edge tuples remain joint invariants — a prediction of
    # exactly ``1.0`` lands in bin ``bin_count - 1``, and a prediction
    # of ``0.9999`` with ``bin_count=10`` lands in bin 9 because
    # ``0.9 <= 0.9999 < 1.0`` after the rounded edges are applied.
    bucketed: list[list[tuple[float, int]]] = [[] for _ in range(bin_count)]
    for model_p, observed in pairs:
        index = _assign_bin_index(model_p, edges=edges, bin_count=bin_count)
        bucketed[index].append((model_p, observed))

    bins: list[ReliabilityBin] = []
    for index, (lower_p, upper_p) in enumerate(edges):
        bin_obs = bucketed[index]
        count = len(bin_obs)
        if count == 0:
            bins.append(
                ReliabilityBin(
                    lower_p=lower_p,
                    upper_p=upper_p,
                    count=0,
                    mean_predicted_p=None,
                    empirical_rate=None,
                )
            )
            continue
        mean_predicted_p = sum(p for p, _ in bin_obs) / count
        empirical_rate = sum(o for _, o in bin_obs) / count
        bins.append(
            ReliabilityBin(
                lower_p=lower_p,
                upper_p=upper_p,
                count=count,
                mean_predicted_p=mean_predicted_p,
                empirical_rate=empirical_rate,
            )
        )

    return ReliabilityDiagram(bin_count=bin_count, bins=tuple(bins))


def compute_reliability_diagrams_per_sector(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    bin_count_global: int,
    bin_count_per_sector: Mapping[str, int],
) -> dict[str, ReliabilityDiagram]:
    """Return a per-sector reliability diagram for one backtest run.

    Queries ``backtest_predictions`` for the supplied ``run_id``, filters
    to ``status='scored' AND brier_contribution IS NOT NULL`` (the same
    aggregation predicate :func:`compute_brier_per_sector` uses, so
    T-CB-023 wires Brier and reliability over an identical row set),
    groups rows by ``sector``, and calls :func:`compute_bins` per sector
    with ``bin_count_per_sector.get(sector, bin_count_global)``.

    :param conn: Open DuckDB connection with the calibration_backtest
        schema applied.
    :param run_id: Run identifier whose scored predictions to bucket.
    :param bin_count_global: Default bin count when no per-sector
        override applies. Must be ``>= 2``.
    :param bin_count_per_sector: Per-sector overrides. Each value must
        be ``>= 2``.
    :returns: Mapping from sector name to its
        :class:`ReliabilityDiagram`. Sectors with no scored predictions
        are omitted from the result so the renderer does not paint
        empty diagrams (REQ-CB-RENDER-002).
    :raises BacktestConfigError: If ``bin_count_global < 2`` or any
        per-sector override is ``< 2``.
    :raises BacktestPersistenceError: If the underlying DuckDB query
        fails.
    """
    if bin_count_global < 2:
        raise BacktestConfigError(
            "compute_reliability_diagrams_per_sector requires "
            f"bin_count_global >= 2, got {bin_count_global!r}"
        )
    for sector, count in bin_count_per_sector.items():
        if count < 2:
            raise BacktestConfigError(
                "compute_reliability_diagrams_per_sector requires "
                f"bin_count_per_sector[{sector!r}] >= 2, got {count!r}"
            )

    try:
        rows = conn.execute(_RELIABILITY_PER_SECTOR_SQL, [run_id]).fetchall()
    except duckdb.Error as exc:
        raise BacktestPersistenceError(
            f"compute_reliability_diagrams_per_sector({run_id!r}) failed: {exc}"
        ) from exc

    by_sector: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for sector, model_p, observed in rows:
        by_sector[str(sector)].append((float(model_p), int(observed)))

    diagrams: dict[str, ReliabilityDiagram] = {}
    for sector, sector_pairs in by_sector.items():
        sector_bin_count = bin_count_per_sector.get(sector, bin_count_global)
        diagrams[sector] = compute_bins(sector_pairs, bin_count=sector_bin_count)
    return diagrams


# -- internals --------------------------------------------------------------


def _equal_width_edges(bin_count: int) -> list[tuple[float, float]]:
    """Build ``bin_count`` equal-width bin edges covering ``[0.0, 1.0]``.

    Mirrors ``report_generator.engines.section_assemblers.reliability.
    _equal_width_bins`` exactly: ``width = 1.0 / bin_count``; each edge
    rounded to four decimals via ``round(i * width, 4)``. Bins are
    half-open ``[lo, hi)`` with the last bin closed at ``1.0``; the
    inclusive-top-bin rule is enforced by :func:`_assign_bin_index`,
    not by the edge tuple itself.
    """
    width = 1.0 / bin_count
    return [
        (
            round(i * width, _EDGE_DECIMALS),
            round((i + 1) * width, _EDGE_DECIMALS),
        )
        for i in range(bin_count)
    ]


def _assign_bin_index(
    model_p: float,
    *,
    edges: list[tuple[float, float]],
    bin_count: int,
) -> int:
    """Return the bin index for ``model_p`` under the half-open rule.

    The rule mirrors ``report_generator``'s
    ``_compute_bin_summaries`` predicate
    ``(bin_lo <= p < bin_hi) or (is_top_bin and p == bin_hi)``. We walk
    the edges in order rather than computing
    ``int(model_p * bin_count)`` because the rounded edges (four
    decimals) and the half-open rule are joint invariants — the index
    must match the edge tuple exactly, not a parallel arithmetic that
    could drift on float-noise.
    """
    last_index = bin_count - 1
    for index, (lower_p, upper_p) in enumerate(edges):
        is_top_bin = index == last_index
        if lower_p <= model_p < upper_p:
            return index
        if is_top_bin and model_p == upper_p:
            return index
    # ``model_p`` outside ``[0.0, 1.0]`` is a model-validation failure
    # upstream; we surface a structured diagnostic here rather than
    # letting the loop fall off the end and raising an opaque error.
    raise BacktestConfigError(
        f"compute_bins received model_p={model_p!r} outside [0.0, 1.0]; "
        "every (model_p, observed) pair must satisfy 0.0 <= model_p <= 1.0"
    )


# ---------------------------------------------------------------------------
# Aggregate-summary assembly (T-CB-023; design §3.6)
# ---------------------------------------------------------------------------


_BRIER_OVERALL_SQL: Final[str] = (
    "SELECT AVG(brier_contribution) "
    "FROM backtest_predictions "
    "WHERE run_id = ? "
    "  AND status = 'scored' "
    "  AND brier_contribution IS NOT NULL"
)


_FALLBACK_POLARITY_SQL: Final[str] = (
    "SELECT "
    "  SUM(CASE WHEN polarity_source = ? THEN 1 ELSE 0 END) AS fallback_count, "
    "  COUNT(*) AS scored_count "
    "FROM backtest_predictions "
    "WHERE run_id = ? "
    "  AND status = 'scored'"
)


def aggregate_run_summary(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    bin_count_global: int,
    bin_count_per_sector: Mapping[str, int],
) -> ScoreSummary:
    """Assemble a :class:`ScoreSummary` for a completed backtest run.

    Orchestrates the canonical Brier aggregations
    (:func:`compute_brier_per_sector`, :func:`compute_brier_per_class`),
    the per-sector reliability diagrams
    (:func:`compute_reliability_diagrams_per_sector`), the
    zero-scored-resolution counters
    (:func:`detect_zero_resolution_groups`), and the
    fallback-polarity provenance counts derived from
    ``backtest_predictions.polarity_source``.

    The aggregation function for ``per_sector_brier`` and
    ``per_class_brier`` is ``AVG(brier_contribution)`` over rows with
    ``status='scored' AND brier_contribution IS NOT NULL`` — the
    canonical aggregation locked by the T-CB-023 amendment so the
    T-CB-024 compare engine produces zero deltas on a self-compare.

    ``fallback_polarity_rate`` is reported as ``fallback_count /
    scored_count`` so the operator-facing render can flag runs whose
    aggregate polarity provenance leaned on
    :data:`PolaritySource.CURRENT_MAPPING_FALLBACK` for more than the
    five-percent advisory threshold (design §3.4). When ``scored_count
    == 0`` the rate falls back to ``0.0`` so the
    :class:`ScoreSummary` validator sees a value in ``[0.0, 1.0]``.
    """
    if bin_count_global < 2:
        raise BacktestConfigError(
            f"aggregate_run_summary requires bin_count_global >= 2, got {bin_count_global!r}"
        )

    try:
        overall_row = conn.execute(_BRIER_OVERALL_SQL, [run_id]).fetchone()
    except duckdb.Error as exc:
        raise BacktestPersistenceError(
            f"aggregate_run_summary({run_id!r}) failed at overall brier query: {exc}"
        ) from exc
    overall_brier = (
        float(overall_row[0]) if overall_row is not None and overall_row[0] is not None else 0.0
    )

    per_sector = compute_brier_per_sector(conn, run_id)
    per_class = compute_brier_per_class(conn, run_id)
    zero_sectors, zero_classes = detect_zero_resolution_groups(conn, run_id)
    diagrams = compute_reliability_diagrams_per_sector(
        conn,
        run_id,
        bin_count_global=bin_count_global,
        bin_count_per_sector=bin_count_per_sector,
    )

    try:
        fallback_row = conn.execute(
            _FALLBACK_POLARITY_SQL,
            [str(PolaritySource.CURRENT_MAPPING_FALLBACK), run_id],
        ).fetchone()
    except duckdb.Error as exc:
        raise BacktestPersistenceError(
            f"aggregate_run_summary({run_id!r}) failed at fallback polarity query: {exc}"
        ) from exc
    fallback_count = (
        int(fallback_row[0]) if fallback_row is not None and fallback_row[0] is not None else 0
    )
    scored_count = (
        int(fallback_row[1]) if fallback_row is not None and fallback_row[1] is not None else 0
    )
    fallback_rate = (fallback_count / scored_count) if scored_count > 0 else 0.0

    return ScoreSummary(
        overall_brier=overall_brier,
        per_sector_brier=per_sector,
        per_class_brier=per_class,
        reliability_diagrams=diagrams,
        zero_resolutions_sectors=zero_sectors,
        zero_resolutions_classes=zero_classes,
        fallback_polarity_count=fallback_count,
        fallback_polarity_rate=fallback_rate,
    )


# ---------------------------------------------------------------------------
# Bin-count resolution (T-CB-026; design §3.6, REQ-CB-SCORE-004)
# ---------------------------------------------------------------------------


# Workspace-relative location of report_generator's config file. The
# loader's own ``DEFAULT_CONFIG_PATH`` is a *relative* :class:`Path`,
# which silently returns all defaults when the CLI runs from a working
# directory other than the workspace root (T-CB-026 scout amendment).
# Resolving against the package root removes the CWD-dependence: the
# package lives at ``src/razor_rooster/calibration_backtest/engines/``,
# so the workspace root is four ``parent`` hops up.
_WORKSPACE_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
DEFAULT_REPORT_CONFIG_PATH: Final[Path] = _WORKSPACE_ROOT / "config" / "report.yaml"
"""Absolute path to ``config/report.yaml`` resolved from the package root.

T-CB-026 scout amendment: ``report_generator.config.loader`` exposes a
``DEFAULT_CONFIG_PATH`` (relative ``Path('config') / 'report.yaml'``)
which silently returns defaults when the CLI runs from a directory
other than the workspace root. calibration_backtest computes the
absolute path here so the resolution is CWD-independent.
"""


def resolve_bin_counts(
    params: RunParameters,
    *,
    config_path: Path | None = None,
) -> tuple[int, dict[str, int]]:
    """Resolve the global and per-sector reliability-bin counts for a run.

    Resolution order (highest priority first, T-CB-026 deliverable):

    1. CLI overrides on :class:`RunParameters` —
       ``params.bin_count`` (global) and
       ``params.bin_count_per_sector`` (per-sector).
    2. ``config/report.yaml`` per-sector via
       ``cfg.thresholds.reliability_bin_count_for_sector(sector)``. The
       helper already implements the per-sector → global → default(10)
       fallback chain; we layer the CLI overrides on top.
    3. ``config/report.yaml`` global via
       ``cfg.thresholds.reliability_bin_count`` (covered by the helper
       above).
    4. Module default :data:`DEFAULT_BIN_COUNT` (10).

    The function returns ``(bin_count_global, bin_count_per_sector)``
    where ``bin_count_per_sector`` is a dict mapping sector identifiers
    in ``params.sectors`` to their resolved bin count. Sectors that
    fall through to the global default are *omitted* from the dict so
    downstream consumers can detect "no override" without comparing to
    the global value.

    The path resolution is deliberately **absolute** to dodge the
    ``DEFAULT_CONFIG_PATH`` CWD bug noted in the T-CB-026 scout
    amendment. When ``config_path`` is omitted we use
    :data:`DEFAULT_REPORT_CONFIG_PATH`; when supplied, the caller may
    pass either an absolute path (preferred) or a relative path which
    the underlying ``Path.exists`` check resolves against the current
    working directory.

    Validation: every resolved bin count is ``>= 2``; otherwise the
    function raises :class:`BacktestConfigError`. The
    ``report_generator`` loader silently clamps to ``[2, 50]``, but
    calibration_backtest must guard explicitly because the
    :class:`ReliabilityDiagram` model rejects ``< 2`` (and a CLI
    override could surface a value of 1).

    Args:
        params: The run-parameters tuple. CLI overrides on this
            instance take precedence.
        config_path: Optional override for the report.yaml location.
            Defaults to :data:`DEFAULT_REPORT_CONFIG_PATH`.

    Returns:
        ``(bin_count_global, bin_count_per_sector)``.
        ``bin_count_per_sector`` only contains entries that override
        the global value.

    Raises:
        BacktestConfigError: If any resolved bin count is below ``2``.
    """
    target_path = config_path if config_path is not None else DEFAULT_REPORT_CONFIG_PATH
    cfg = _load_report_config(target_path)

    # Global bin count: CLI override beats report.yaml beats module default.
    if params.bin_count is not None:
        global_bin_count = params.bin_count
    else:
        global_bin_count = cfg.thresholds.reliability_bin_count

    if global_bin_count < 2:
        raise BacktestConfigError(
            f"resolve_bin_counts: bin_count_global must be >= 2, got {global_bin_count!r}"
        )

    # Per-sector overrides: CLI map beats the loader helper.
    per_sector_resolved: dict[str, int] = {}
    for sector in params.sectors:
        if sector in params.bin_count_per_sector:
            sector_count = params.bin_count_per_sector[sector]
        else:
            sector_count = cfg.thresholds.reliability_bin_count_for_sector(sector)
        if sector_count < 2:
            raise BacktestConfigError(
                f"resolve_bin_counts: bin_count_per_sector[{sector!r}] must be >= 2, "
                f"got {sector_count!r}"
            )
        if sector_count != global_bin_count:
            per_sector_resolved[sector] = sector_count

    # Also surface CLI per-sector overrides for sectors not listed in
    # ``params.sectors`` (an operator could pass ``--bin-count-per-sector``
    # without a matching ``--sector`` filter; we want the override to
    # still take effect for that sector when scored predictions exist).
    for sector, override in params.bin_count_per_sector.items():
        if sector in per_sector_resolved:
            continue
        if override < 2:
            raise BacktestConfigError(
                f"resolve_bin_counts: bin_count_per_sector[{sector!r}] must be >= 2, "
                f"got {override!r}"
            )
        if override != global_bin_count:
            per_sector_resolved[sector] = override

    return global_bin_count, per_sector_resolved


def _load_report_config(path: Path) -> Any:
    """Load report_generator's ``ReportConfig`` from *path*, logging on miss.

    The loader (``report_generator.config.loader.load_config``) does
    NOT raise on a missing file — it returns the default
    :class:`ReportConfig`. T-CB-026 requires we distinguish "loaded"
    from "defaulted" so operators can see when their overrides are
    silently ignored. The ``Any`` return type avoids importing
    ``ReportConfig`` at module scope (its transitive import surface is
    heavier than the resolver needs).
    """
    # Local import: keep the import surface narrow at module scope so
    # tests for resolve_bin_counts don't drag the full report_generator
    # transitive graph through the import system.
    from razor_rooster.report_generator.config.loader import load_config

    if not path.exists():
        logger.warning(
            "calibration_backtest.resolve_bin_counts: report config %s not found; "
            "falling back to ReportConfig defaults",
            path,
        )
    else:
        logger.info(
            "calibration_backtest.resolve_bin_counts: loaded report config from %s",
            path,
        )
    return load_config(path=path)


__all__ = [
    "DEFAULT_BIN_COUNT",
    "DEFAULT_REPORT_CONFIG_PATH",
    "aggregate_run_summary",
    "compute_bins",
    "compute_brier_overall",
    "compute_brier_per_class",
    "compute_brier_per_sector",
    "compute_reliability_diagrams_per_sector",
    "detect_zero_resolution_groups",
    "resolve_bin_counts",
]
