"""Unit tests for :func:`rank_compare_cells` (T-CB-025; design §3.7).

Covers the delta-magnitude ranking produced by
:func:`razor_rooster.calibration_backtest.engines.compare.rank_compare_cells`:

* ``rank_by='absolute'`` orders by ``abs(delta_absolute)`` descending
  with asymmetric cells (where ``delta_absolute is None``) at the
  bottom in stable input order.
* ``rank_by='percent'`` mirrors the above but uses ``delta_percent``;
  cells with ``delta_percent is None`` (asymmetric *or* ``brier_a == 0``
  division-by-zero guard cells) drop to the bottom.
* Tie-stability: identical magnitudes preserve input order (Python's
  :func:`sorted` is stable; the helper relies on that contract).
* ``None`` handling never raises ``TypeError`` from comparison — the
  partition discriminator runs before any ``abs()``/negation.
* Unknown ``rank_by`` values raise :class:`BacktestConfigError` with a
  structured message.
"""

from __future__ import annotations

import pytest

from razor_rooster.calibration_backtest.engines.compare import rank_compare_cells
from razor_rooster.calibration_backtest.errors import BacktestConfigError
from razor_rooster.calibration_backtest.models import CompareCell, PresentIn

# ---------------------------------------------------------------------------
# Cell builders — terse helpers so each test reads as a sequence of cells.
# ---------------------------------------------------------------------------


def _both(
    sector: str,
    class_id: str,
    *,
    delta_absolute: float,
    delta_percent: float | None = None,
) -> CompareCell:
    """Build a both-present :class:`CompareCell` with the given deltas.

    ``brier_a`` and ``brier_b`` are synthesised from ``delta_absolute``
    so the :class:`CompareCell` validator (which requires non-``None``
    Brier values for ``PresentIn.BOTH``) is satisfied. The exact
    Brier choice is irrelevant to the ranking under test — only the
    delta fields matter.
    """
    brier_a = 0.20
    brier_b = brier_a + delta_absolute
    if not 0.0 <= brier_b <= 1.0:
        # Move the anchor so brier_b stays in [0, 1]; ranking is delta-driven.
        brier_a = max(0.0, -delta_absolute) + 0.10
        brier_b = brier_a + delta_absolute
    return CompareCell(
        sector=sector,
        class_id=class_id,
        brier_a=brier_a,
        brier_b=brier_b,
        delta_absolute=delta_absolute,
        delta_percent=delta_percent,
        crossed_miscalibration_threshold=False,
        present_in=PresentIn.BOTH,
        trace_diff_summary=None,
    )


def _a_only(sector: str, class_id: str, *, brier_a: float = 0.30) -> CompareCell:
    return CompareCell(
        sector=sector,
        class_id=class_id,
        brier_a=brier_a,
        brier_b=None,
        delta_absolute=None,
        delta_percent=None,
        crossed_miscalibration_threshold=None,
        present_in=PresentIn.A_ONLY,
        trace_diff_summary=None,
    )


def _b_only(sector: str, class_id: str, *, brier_b: float = 0.40) -> CompareCell:
    return CompareCell(
        sector=sector,
        class_id=class_id,
        brier_a=None,
        brier_b=brier_b,
        delta_absolute=None,
        delta_percent=None,
        crossed_miscalibration_threshold=None,
        present_in=PresentIn.B_ONLY,
        trace_diff_summary=None,
    )


def _both_zero_brier_a(sector: str, class_id: str, *, delta_absolute: float) -> CompareCell:
    """A both-present cell whose ``brier_a == 0`` triggers the percent-None guard.

    The compare engine emits ``delta_percent=None`` for these cells; the
    ranker must still treat them as bottom-tier under
    ``rank_by='percent'`` while keeping them ranked-by-magnitude under
    ``rank_by='absolute'``.
    """
    return CompareCell(
        sector=sector,
        class_id=class_id,
        brier_a=0.0,
        brier_b=delta_absolute,
        delta_absolute=delta_absolute,
        delta_percent=None,
        crossed_miscalibration_threshold=False,
        present_in=PresentIn.BOTH,
        trace_diff_summary=None,
    )


# ---------------------------------------------------------------------------
# rank_by='absolute'
# ---------------------------------------------------------------------------


def test_rank_absolute_orders_by_abs_delta_desc_asymmetric_at_bottom() -> None:
    """4 cells: 2 BOTH + 1 A_ONLY + 1 B_ONLY.

    Expected order: largest |delta_absolute| first, then next largest,
    then asymmetric cells at the bottom in input order.
    """
    big = _both("public_health", "flu_h2h", delta_absolute=0.40, delta_percent=200.0)
    small = _both("public_health", "covid_h2h", delta_absolute=0.10, delta_percent=50.0)
    a_only = _a_only("macro", "cpi_h2h")
    b_only = _b_only("macro", "jobs_h2h")
    cells = [small, a_only, big, b_only]

    ranked = rank_compare_cells(cells, rank_by="absolute")

    assert [c.class_id for c in ranked] == ["flu_h2h", "covid_h2h", "cpi_h2h", "jobs_h2h"]


def test_rank_absolute_negative_delta_uses_magnitude() -> None:
    """``abs()`` is applied so negative deltas with larger magnitude rank higher."""
    pos_small = _both("s", "a", delta_absolute=0.10, delta_percent=50.0)
    neg_big = _both("s", "b", delta_absolute=-0.30, delta_percent=-150.0)
    ranked = rank_compare_cells([pos_small, neg_big], rank_by="absolute")
    assert [c.class_id for c in ranked] == ["b", "a"]


def test_rank_absolute_tie_preserves_input_order() -> None:
    """Cells with identical ``|delta_absolute|`` keep their input order (stable sort)."""
    cell1 = _both("s", "first", delta_absolute=0.30, delta_percent=150.0)
    cell2 = _both("s", "second", delta_absolute=0.30, delta_percent=150.0)
    cell3 = _both("s", "third", delta_absolute=-0.30, delta_percent=-150.0)
    ranked = rank_compare_cells([cell1, cell2, cell3], rank_by="absolute")
    assert [c.class_id for c in ranked] == ["first", "second", "third"]


def test_rank_absolute_does_not_mutate_input() -> None:
    """``rank_compare_cells`` returns a new list; the input is untouched."""
    big = _both("s", "a", delta_absolute=0.40, delta_percent=200.0)
    small = _both("s", "b", delta_absolute=0.10, delta_percent=50.0)
    cells = [small, big]
    snapshot = list(cells)
    _ = rank_compare_cells(cells, rank_by="absolute")
    assert cells == snapshot


# ---------------------------------------------------------------------------
# rank_by='percent'
# ---------------------------------------------------------------------------


def test_rank_percent_orders_by_abs_delta_percent_desc_with_none_at_bottom() -> None:
    """``delta_percent=None`` cells (asymmetric or div-by-zero) sort to bottom."""
    big = _both("s", "flu", delta_absolute=0.40, delta_percent=200.0)
    small = _both("s", "covid", delta_absolute=0.10, delta_percent=50.0)
    a_only = _a_only("s", "cpi")
    b_only = _b_only("s", "jobs")
    cells = [small, a_only, big, b_only]

    ranked = rank_compare_cells(cells, rank_by="percent")

    assert [c.class_id for c in ranked] == ["flu", "covid", "cpi", "jobs"]


def test_rank_percent_treats_brier_a_zero_cells_as_bottom() -> None:
    """A both-present cell with ``brier_a == 0`` carries ``delta_percent=None``.

    Under ``rank_by='percent'`` it must drop to the bottom alongside
    asymmetric cells. Under ``rank_by='absolute'`` it ranks by its
    non-``None`` ``delta_absolute``.
    """
    big_pct = _both("s", "flu", delta_absolute=0.20, delta_percent=100.0)
    zero_brier_a = _both_zero_brier_a("s", "edge", delta_absolute=0.50)
    cells = [zero_brier_a, big_pct]

    ranked_pct = rank_compare_cells(cells, rank_by="percent")
    # ``big_pct`` (delta_percent=100.0) ranks above the None-percent cell.
    assert [c.class_id for c in ranked_pct] == ["flu", "edge"]

    ranked_abs = rank_compare_cells(cells, rank_by="absolute")
    # ``zero_brier_a`` (delta_absolute=0.50) ranks above the smaller delta.
    assert [c.class_id for c in ranked_abs] == ["edge", "flu"]


def test_rank_percent_tie_preserves_input_order() -> None:
    """Identical ``|delta_percent|`` magnitudes preserve input order."""
    a = _both("s", "alpha", delta_absolute=0.10, delta_percent=100.0)
    b = _both("s", "beta", delta_absolute=0.10, delta_percent=-100.0)
    ranked = rank_compare_cells([a, b], rank_by="percent")
    assert [c.class_id for c in ranked] == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# None-handling robustness
# ---------------------------------------------------------------------------


def test_rank_absolute_none_cells_do_not_raise_typeerror() -> None:
    """Sorting cells with ``delta_absolute=None`` does not raise ``TypeError``."""
    cells = [
        _a_only("s", "x"),
        _b_only("s", "y"),
        _a_only("s", "z"),
    ]
    # Smoke test: the call must not raise. The relative order is the
    # input order (all cells partition to the bottom equally).
    ranked = rank_compare_cells(cells, rank_by="absolute")
    assert [c.class_id for c in ranked] == ["x", "y", "z"]


def test_rank_percent_none_cells_do_not_raise_typeerror() -> None:
    """Sorting cells with ``delta_percent=None`` does not raise ``TypeError``."""
    cells = [
        _a_only("s", "x"),
        _both_zero_brier_a("s", "y", delta_absolute=0.10),
        _b_only("s", "z"),
    ]
    ranked = rank_compare_cells(cells, rank_by="percent")
    assert [c.class_id for c in ranked] == ["x", "y", "z"]


def test_rank_empty_list_returns_empty_list() -> None:
    """``rank_compare_cells([], rank_by=...)`` is the empty list."""
    assert rank_compare_cells([], rank_by="absolute") == []
    assert rank_compare_cells([], rank_by="percent") == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_rank_compare_cells_rejects_unknown_rank_by() -> None:
    """Any value other than ``'absolute'`` / ``'percent'`` raises."""
    cells = [_both("s", "a", delta_absolute=0.10, delta_percent=50.0)]
    with pytest.raises(BacktestConfigError, match="rank_by"):
        rank_compare_cells(cells, rank_by="bogus")  # type: ignore[arg-type]
