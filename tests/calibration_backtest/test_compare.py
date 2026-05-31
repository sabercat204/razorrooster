"""Unit tests for the calibration_backtest compare engine (T-CB-024).

Each test acquires a fresh in-memory DuckDB connection via the ``conn``
fixture; the migration runner applies m6001+m6002 so the connection
already carries the canonical schema. Tests seed two runs A and B with
overlapping and non-overlapping ``(sector, class_id)`` cells and
exercise every branch of :func:`compare_runs`:

* **Self-compare (correctness-critical):** ``compare_runs(conn, X, X)``
  yields ``delta_absolute == 0.0`` and ``delta_percent == 0.0`` for
  every cell where ``brier_a > 0``; ``delta_percent is None`` when
  ``brier_a == 0`` (division-by-zero guard, REQ-CB-SCORE-005).
* **Asymmetric cells:** cells observed in only one run carry
  :data:`PresentIn.A_ONLY` / :data:`PresentIn.B_ONLY`, ``None`` for the
  missing side, ``None`` for both delta fields, and ``None`` for the
  miscalibration-threshold flag.
* **Threshold flag:** ``crossed_miscalibration_threshold`` fires
  exactly when ``present_in == 'both'`` and ``abs(delta_absolute) >=
  threshold``; the boundary at ``0.25`` is exclusive vs inclusive.
* **Custom threshold:** the ``threshold`` keyword overrides the YAML
  default.
* **Aggregation parity (design §3.7):** the engine ignores
  ``status='skipped'`` rows and rows with ``brier_contribution IS NULL``
  so the per-cell aggregation matches T-CB-023's per-sector / per-class
  Brier exactly.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import duckdb
import pytest

from razor_rooster.calibration_backtest.engines.compare import (
    DEFAULT_BRIER_MISCALIBRATION_THRESHOLD,
    compare_runs,
)
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
    CompareCell,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
    PresentIn,
    SkipReason,
)
from razor_rooster.calibration_backtest.persistence import operations
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield an in-memory DuckDB connection with calibration_backtest schema."""
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


def _make_run(run_id: str, **overrides: Any) -> BacktestRun:
    base: dict[str, Any] = {
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
        "bin_count_per_sector": {},
        "fallback_polarity_count": 0,
        "allow_recent": False,
        "disclaimer_version": "v1",
    }
    base.update(overrides)
    return BacktestRun(**base)


def _insert_scored(
    conn: duckdb.DuckDBPyConnection,
    *,
    run_id: str,
    prediction_id: str,
    sector: str,
    class_id: str,
    brier_contribution: float,
) -> None:
    """Insert one ``status='scored'`` prediction with the given Brier value."""
    pred = BacktestPrediction(
        run_id=run_id,
        prediction_id=prediction_id,
        class_id=class_id,
        condition_id=f"cond-{prediction_id}",
        venue="polymarket",
        sector=sector,
        prediction_ts=_PRED_TS,
        resolution_ts=_RES_TS,
        model_p=0.4,
        observed=1.0,
        polarity=PolarityValue.FORWARD,
        polarity_source=PolaritySource.COMPARISON_RESOLUTIONS,
        mapping_mismatch_warning=False,
        definition_version=1,
        status=PredictionStatus.SCORED,
        skip_reason=None,
        brier_contribution=brier_contribution,
    )
    operations.insert_prediction(conn, pred)


def _insert_skipped(
    conn: duckdb.DuckDBPyConnection,
    *,
    run_id: str,
    prediction_id: str,
    sector: str,
    class_id: str,
) -> None:
    """Insert a ``status='skipped'`` prediction (must not affect aggregates)."""
    pred = BacktestPrediction(
        run_id=run_id,
        prediction_id=prediction_id,
        class_id=class_id,
        condition_id=f"cond-{prediction_id}",
        venue="polymarket",
        sector=sector,
        prediction_ts=_PRED_TS,
        resolution_ts=_RES_TS,
        model_p=None,
        observed=None,
        polarity=None,
        polarity_source=PolaritySource.NO_POLARITY,
        mapping_mismatch_warning=False,
        definition_version=1,
        status=PredictionStatus.SKIPPED,
        skip_reason=SkipReason.NO_POLARITY_RESOLUTION,
        brier_contribution=None,
    )
    operations.insert_prediction(conn, pred)


def _seed_two_runs(conn: duckdb.DuckDBPyConnection) -> tuple[str, str]:
    """Seed two runs with overlapping + asymmetric cells.

    Cells:
      * ``(public_health, flu_h2h)`` — present in both, A=0.20 B=0.50
        (delta=+0.30, crosses default 0.25 threshold).
      * ``(public_health, covid_h2h)`` — present in both, A=0.10 B=0.20
        (delta=+0.10, below default threshold).
      * ``(public_health, zero_h2h)`` — present in both, A=0.0 B=0.0
        (delta=0.0, brier_a=0 -> delta_percent=None).
      * ``(macro, cpi_h2h)`` — present in A only.
      * ``(macro, jobs_h2h)`` — present in B only.
    """
    run_a_id = "run_a"
    run_b_id = "run_b"
    operations.insert_run(conn, _make_run(run_a_id))
    operations.insert_run(conn, _make_run(run_b_id))

    # both-present cells (single observation each so AVG == that value)
    _insert_scored(
        conn,
        run_id=run_a_id,
        prediction_id="a_pred_1",
        sector="public_health",
        class_id="flu_h2h",
        brier_contribution=0.20,
    )
    _insert_scored(
        conn,
        run_id=run_b_id,
        prediction_id="b_pred_1",
        sector="public_health",
        class_id="flu_h2h",
        brier_contribution=0.50,
    )
    _insert_scored(
        conn,
        run_id=run_a_id,
        prediction_id="a_pred_2",
        sector="public_health",
        class_id="covid_h2h",
        brier_contribution=0.10,
    )
    _insert_scored(
        conn,
        run_id=run_b_id,
        prediction_id="b_pred_2",
        sector="public_health",
        class_id="covid_h2h",
        brier_contribution=0.20,
    )
    _insert_scored(
        conn,
        run_id=run_a_id,
        prediction_id="a_pred_3",
        sector="public_health",
        class_id="zero_h2h",
        brier_contribution=0.0,
    )
    _insert_scored(
        conn,
        run_id=run_b_id,
        prediction_id="b_pred_3",
        sector="public_health",
        class_id="zero_h2h",
        brier_contribution=0.0,
    )

    # A-only cell
    _insert_scored(
        conn,
        run_id=run_a_id,
        prediction_id="a_pred_4",
        sector="macro",
        class_id="cpi_h2h",
        brier_contribution=0.30,
    )
    # B-only cell
    _insert_scored(
        conn,
        run_id=run_b_id,
        prediction_id="b_pred_4",
        sector="macro",
        class_id="jobs_h2h",
        brier_contribution=0.40,
    )

    # skipped rows must not affect aggregates
    _insert_skipped(
        conn,
        run_id=run_a_id,
        prediction_id="a_skip_1",
        sector="public_health",
        class_id="flu_h2h",
    )
    _insert_skipped(
        conn,
        run_id=run_b_id,
        prediction_id="b_skip_1",
        sector="public_health",
        class_id="flu_h2h",
    )

    return run_a_id, run_b_id


def _index_by_key(cells: list[CompareCell]) -> dict[tuple[str, str], CompareCell]:
    return {(c.sector, c.class_id): c for c in cells}


# ---------------------------------------------------------------------------
# Default threshold
# ---------------------------------------------------------------------------


def test_default_threshold_constant_matches_yaml() -> None:
    """The module-level default mirrors ``backtest.yaml``'s 0.25 value."""
    assert DEFAULT_BRIER_MISCALIBRATION_THRESHOLD == 0.25


# ---------------------------------------------------------------------------
# Self-compare (correctness-critical: REQ-CB-SCORE-005)
# ---------------------------------------------------------------------------


def test_self_compare_yields_zero_deltas(conn: duckdb.DuckDBPyConnection) -> None:
    """``compare_runs(conn, X, X)`` returns zero deltas for every cell.

    Per design §3.7 the aggregation parity with T-CB-023 means a
    self-compare must be a no-op: ``delta_absolute == 0.0`` for all
    cells, ``delta_percent == 0.0`` for cells where ``brier_a > 0``,
    and ``delta_percent is None`` for the ``brier_a == 0`` edge.
    """
    run_a_id, _ = _seed_two_runs(conn)
    cells = compare_runs(conn, run_a_id, run_a_id)

    assert len(cells) == 4  # cells observed in run A: 3 public_health + 1 macro
    for cell in cells:
        assert cell.present_in is PresentIn.BOTH
        assert cell.brier_a is not None
        assert cell.brier_b is not None
        assert cell.brier_a == cell.brier_b
        assert cell.delta_absolute == 0.0
        assert cell.crossed_miscalibration_threshold is False
        if cell.brier_a == 0.0:
            # division-by-zero guard
            assert cell.delta_percent is None
        else:
            assert cell.delta_percent == 0.0


def test_self_compare_no_scored_rows(conn: duckdb.DuckDBPyConnection) -> None:
    """A run with no scored rows yields an empty cell list on self-compare."""
    run_id = "empty"
    operations.insert_run(conn, _make_run(run_id))
    cells = compare_runs(conn, run_id, run_id)
    assert cells == []


# ---------------------------------------------------------------------------
# Asymmetric cells (REQ-CB-SCORE-005)
# ---------------------------------------------------------------------------


def test_asymmetric_cells_carry_correct_present_in(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Cells in only one run carry ``A_ONLY`` / ``B_ONLY`` and ``None`` deltas."""
    run_a_id, run_b_id = _seed_two_runs(conn)
    cells = compare_runs(conn, run_a_id, run_b_id)
    by_key = _index_by_key(cells)

    a_only = by_key[("macro", "cpi_h2h")]
    assert a_only.present_in is PresentIn.A_ONLY
    assert a_only.brier_a == pytest.approx(0.30)
    assert a_only.brier_b is None
    assert a_only.delta_absolute is None
    assert a_only.delta_percent is None
    assert a_only.crossed_miscalibration_threshold is None

    b_only = by_key[("macro", "jobs_h2h")]
    assert b_only.present_in is PresentIn.B_ONLY
    assert b_only.brier_a is None
    assert b_only.brier_b == pytest.approx(0.40)
    assert b_only.delta_absolute is None
    assert b_only.delta_percent is None
    assert b_only.crossed_miscalibration_threshold is None


def test_asymmetric_cells_never_cross_threshold(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The miscalibration flag is always ``None`` for asymmetric cells.

    Even an extreme threshold override must not surface a boolean for
    an A-only or B-only cell — the flag is undefined when one side has
    no comparable score.
    """
    run_a_id, run_b_id = _seed_two_runs(conn)
    cells = compare_runs(conn, run_a_id, run_b_id, threshold=0.0)
    asymmetric = [c for c in cells if c.present_in is not PresentIn.BOTH]
    assert asymmetric, "fixture seeds two asymmetric cells"
    for cell in asymmetric:
        assert cell.crossed_miscalibration_threshold is None


# ---------------------------------------------------------------------------
# Both-present cells: deltas, percent, threshold flag
# ---------------------------------------------------------------------------


def test_both_present_cells_compute_deltas(conn: duckdb.DuckDBPyConnection) -> None:
    """Delta values are ``brier_b - brier_a`` and ``100 * (b - a) / a``."""
    run_a_id, run_b_id = _seed_two_runs(conn)
    cells = compare_runs(conn, run_a_id, run_b_id)
    by_key = _index_by_key(cells)

    flu = by_key[("public_health", "flu_h2h")]
    assert flu.present_in is PresentIn.BOTH
    assert flu.brier_a == pytest.approx(0.20)
    assert flu.brier_b == pytest.approx(0.50)
    assert flu.delta_absolute == pytest.approx(0.30)
    assert flu.delta_percent == pytest.approx(150.0)

    covid = by_key[("public_health", "covid_h2h")]
    assert covid.present_in is PresentIn.BOTH
    assert covid.brier_a == pytest.approx(0.10)
    assert covid.brier_b == pytest.approx(0.20)
    assert covid.delta_absolute == pytest.approx(0.10)
    assert covid.delta_percent == pytest.approx(100.0)


def test_threshold_flag_at_default_boundary(conn: duckdb.DuckDBPyConnection) -> None:
    """At the default 0.25 threshold: 0.30 crosses; 0.10 does not.

    The flag uses ``>=`` so an exact-0.25 delta crosses (inclusive
    boundary) — exercised in :func:`test_threshold_flag_inclusive_boundary`.
    """
    run_a_id, run_b_id = _seed_two_runs(conn)
    cells = compare_runs(conn, run_a_id, run_b_id)
    by_key = _index_by_key(cells)

    assert by_key[("public_health", "flu_h2h")].crossed_miscalibration_threshold is True
    assert by_key[("public_health", "covid_h2h")].crossed_miscalibration_threshold is False


def test_threshold_flag_inclusive_boundary(conn: duckdb.DuckDBPyConnection) -> None:
    """At default 0.25 threshold: delta=0.25 crosses; delta=0.24 does not.

    Uses brier values whose IEEE-754 subtraction is exact (``0.0`` and
    ``0.25``; ``0.5`` and ``0.26``) so the boundary is unambiguous.
    """
    # delta = 0.25 -> crosses inclusive boundary
    run_a_id = "run_a_eq"
    run_b_id = "run_b_eq"
    operations.insert_run(conn, _make_run(run_a_id))
    operations.insert_run(conn, _make_run(run_b_id))
    _insert_scored(
        conn,
        run_id=run_a_id,
        prediction_id="a1",
        sector="s",
        class_id="c",
        brier_contribution=0.0,
    )
    _insert_scored(
        conn,
        run_id=run_b_id,
        prediction_id="b1",
        sector="s",
        class_id="c",
        brier_contribution=0.25,
    )
    cells = compare_runs(conn, run_a_id, run_b_id)
    assert len(cells) == 1
    assert cells[0].delta_absolute == pytest.approx(0.25)
    assert cells[0].crossed_miscalibration_threshold is True

    # delta = 0.24 -> below 0.25 threshold -> False
    operations.insert_run(conn, _make_run("run_c_below"))
    operations.insert_run(conn, _make_run("run_d_below"))
    _insert_scored(
        conn,
        run_id="run_c_below",
        prediction_id="c1",
        sector="s",
        class_id="c",
        brier_contribution=0.50,
    )
    _insert_scored(
        conn,
        run_id="run_d_below",
        prediction_id="d1",
        sector="s",
        class_id="c",
        brier_contribution=0.26,
    )
    cells_below = compare_runs(conn, "run_c_below", "run_d_below")
    assert len(cells_below) == 1
    assert cells_below[0].delta_absolute == pytest.approx(-0.24)
    # abs(-0.24) = 0.24 < 0.25 -> False
    assert cells_below[0].crossed_miscalibration_threshold is False


def test_threshold_flag_negative_delta(conn: duckdb.DuckDBPyConnection) -> None:
    """``abs(delta_absolute)`` is used so negative deltas can cross."""
    run_a_id = "run_a"
    run_b_id = "run_b"
    operations.insert_run(conn, _make_run(run_a_id))
    operations.insert_run(conn, _make_run(run_b_id))
    _insert_scored(
        conn,
        run_id=run_a_id,
        prediction_id="a1",
        sector="s",
        class_id="c",
        brier_contribution=0.50,
    )
    _insert_scored(
        conn,
        run_id=run_b_id,
        prediction_id="b1",
        sector="s",
        class_id="c",
        brier_contribution=0.10,
    )
    cells = compare_runs(conn, run_a_id, run_b_id)
    assert cells[0].delta_absolute == pytest.approx(-0.40)
    assert cells[0].crossed_miscalibration_threshold is True


# ---------------------------------------------------------------------------
# Division-by-zero guard
# ---------------------------------------------------------------------------


def test_delta_percent_none_when_brier_a_zero(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``delta_percent`` is ``None`` when ``brier_a == 0.0``.

    The SQL ``CASE`` only emits the percent expression when ``brier_a >
    0``; otherwise the value is ``NULL`` -> ``None``. ``delta_absolute``
    remains computable.
    """
    run_a_id = "run_a"
    run_b_id = "run_b"
    operations.insert_run(conn, _make_run(run_a_id))
    operations.insert_run(conn, _make_run(run_b_id))
    _insert_scored(
        conn,
        run_id=run_a_id,
        prediction_id="a1",
        sector="s",
        class_id="c",
        brier_contribution=0.0,
    )
    _insert_scored(
        conn,
        run_id=run_b_id,
        prediction_id="b1",
        sector="s",
        class_id="c",
        brier_contribution=0.10,
    )
    cells = compare_runs(conn, run_a_id, run_b_id)
    assert len(cells) == 1
    assert cells[0].brier_a == 0.0
    assert cells[0].delta_absolute == pytest.approx(0.10)
    assert cells[0].delta_percent is None


# ---------------------------------------------------------------------------
# Custom threshold override
# ---------------------------------------------------------------------------


def test_threshold_override_flips_flag(conn: duckdb.DuckDBPyConnection) -> None:
    """The ``threshold`` kwarg overrides the YAML default."""
    run_a_id, run_b_id = _seed_two_runs(conn)
    cells_default = compare_runs(conn, run_a_id, run_b_id)
    by_key_default = _index_by_key(cells_default)
    # default 0.25: covid delta=0.10 -> False
    assert by_key_default[("public_health", "covid_h2h")].crossed_miscalibration_threshold is False

    cells_loose = compare_runs(conn, run_a_id, run_b_id, threshold=0.05)
    by_key_loose = _index_by_key(cells_loose)
    # threshold=0.05: covid delta=0.10 -> True
    assert by_key_loose[("public_health", "covid_h2h")].crossed_miscalibration_threshold is True


def test_threshold_zero_flips_all_both_present(conn: duckdb.DuckDBPyConnection) -> None:
    """``threshold=0.0`` flips every BOTH cell with non-zero delta to True."""
    run_a_id, run_b_id = _seed_two_runs(conn)
    cells = compare_runs(conn, run_a_id, run_b_id, threshold=0.0)
    for cell in cells:
        if cell.present_in is PresentIn.BOTH:
            assert cell.delta_absolute is not None
            expected = abs(cell.delta_absolute) >= 0.0
            assert cell.crossed_miscalibration_threshold is expected


def test_negative_threshold_rejected(conn: duckdb.DuckDBPyConnection) -> None:
    run_a_id, run_b_id = _seed_two_runs(conn)
    with pytest.raises(ValueError, match="threshold"):
        compare_runs(conn, run_a_id, run_b_id, threshold=-0.01)


# ---------------------------------------------------------------------------
# Aggregation parity (skipped + null brier_contribution rows are excluded)
# ---------------------------------------------------------------------------


def test_skipped_rows_excluded_from_aggregation(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``status='skipped'`` rows must not affect ``AVG(brier_contribution)``.

    The fixture seeds one skipped row per cell so the aggregate would
    drift if the engine forgot the WHERE filter. Asserting the
    deterministic Brier values from the fixture is sufficient.
    """
    run_a_id, run_b_id = _seed_two_runs(conn)
    cells = compare_runs(conn, run_a_id, run_b_id)
    flu = _index_by_key(cells)[("public_health", "flu_h2h")]
    # If skipped rows leaked into AVG (e.g. counted as 0), brier_a/b
    # would be 0.10/0.25 instead of 0.20/0.50.
    assert flu.brier_a == pytest.approx(0.20)
    assert flu.brier_b == pytest.approx(0.50)


def test_aggregation_averages_multiple_predictions(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``AVG(brier_contribution)`` averages multiple scored rows per cell."""
    run_a_id = "run_a"
    run_b_id = "run_b"
    operations.insert_run(conn, _make_run(run_a_id))
    operations.insert_run(conn, _make_run(run_b_id))
    # A: AVG(0.10, 0.30) = 0.20
    _insert_scored(
        conn,
        run_id=run_a_id,
        prediction_id="a1",
        sector="s",
        class_id="c",
        brier_contribution=0.10,
    )
    _insert_scored(
        conn,
        run_id=run_a_id,
        prediction_id="a2",
        sector="s",
        class_id="c",
        brier_contribution=0.30,
    )
    # B: AVG(0.40, 0.60) = 0.50
    _insert_scored(
        conn,
        run_id=run_b_id,
        prediction_id="b1",
        sector="s",
        class_id="c",
        brier_contribution=0.40,
    )
    _insert_scored(
        conn,
        run_id=run_b_id,
        prediction_id="b2",
        sector="s",
        class_id="c",
        brier_contribution=0.60,
    )
    cells = compare_runs(conn, run_a_id, run_b_id)
    assert len(cells) == 1
    assert cells[0].brier_a == pytest.approx(0.20)
    assert cells[0].brier_b == pytest.approx(0.50)
    assert cells[0].delta_absolute == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# Determinism: ordering by (sector, class_id) ASCII
# ---------------------------------------------------------------------------


def test_cells_ordered_by_sector_then_class_id(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Cells are returned in deterministic ``(sector, class_id)`` order."""
    run_a_id, run_b_id = _seed_two_runs(conn)
    cells = compare_runs(conn, run_a_id, run_b_id)
    keys = [(c.sector, c.class_id) for c in cells]
    assert keys == sorted(keys)


def test_compare_returns_list_type(conn: duckdb.DuckDBPyConnection) -> None:
    """The contract is ``list[CompareCell]`` (not tuple), per signature."""
    run_a_id, run_b_id = _seed_two_runs(conn)
    cells = compare_runs(conn, run_a_id, run_b_id)
    assert isinstance(cells, list)
    for cell in cells:
        assert isinstance(cell, CompareCell)
