"""Unit tests for calibration_backtest reliability binning (T-CB-022).

These tests pin the equal-width binning convention shared with
``report_generator.engines.section_assemblers.reliability``. The parity
test imports the **private** ``_equal_width_bins`` helper from
report_generator solely to assert bit-for-bit edge equality across the
two subsystems; production calibration_backtest code never reaches
across the package boundary (the helper is underscore-private and there
is no compatible public surface — see the T-CB-022 scout amendment).

Coverage:

* Parity: edges produced by :func:`compute_bins` match
  ``report_generator._equal_width_bins`` exactly for ``bin_count`` in
  ``{2, 5, 10, 20}``.
* Top-bin boundary: with ``bin_count=10``, predictions ``0.0``, ``0.1``,
  ``0.5``, ``0.9999``, and ``1.0`` land in bins ``0``, ``1``, ``5``,
  ``9``, ``9`` respectively (the inclusive-top-bin rule).
* 4-decimal rounding: ``bin_count=3`` produces edges
  ``[0.0, 0.3333, 0.6667, 1.0]`` rather than float-noise values like
  ``0.30000000000000004``.
* Empty bins: with ``pairs=[(0.05, 0), (0.95, 1)]`` and ``bin_count=10``
  the diagram carries exactly 10 bins, with bins ``1..8`` reporting
  ``count=0``, ``mean_predicted_p=None``, ``empirical_rate=None``.
* Validation: ``bin_count=1`` raises :class:`BacktestConfigError`
  before the diagram is constructed.
* Hand-computed reliability: 4 predictions in 2 bins yield expected
  ``mean_predicted_p`` and ``empirical_rate`` within ``1e-9``.
"""

from __future__ import annotations

import pytest

from razor_rooster.calibration_backtest.engines.scoring import compute_bins
from razor_rooster.calibration_backtest.errors import BacktestConfigError
from razor_rooster.calibration_backtest.models import ReliabilityDiagram

# Private import — TEST ONLY. Production calibration_backtest code does
# NOT import from report_generator (the helper is underscore-private and
# there is no compatible public surface). The parity assertion below is
# the sole purpose of this import.
from razor_rooster.report_generator.engines.section_assemblers.reliability import (
    _equal_width_bins as _rg_equal_width_bins,
)


@pytest.mark.parametrize("bin_count", [2, 5, 10, 20])
def test_parity_with_report_generator_equal_width_bins(bin_count: int) -> None:
    """Bin edges match report_generator's ``_equal_width_bins`` exactly.

    This is the regression-critical parity test called out in the T-CB-022
    scout amendment: any drift between the two subsystems would cause the
    daily report's reliability diagrams and the calibration_backtest
    summary to disagree on bin boundaries.
    """
    diagram = compute_bins([], bin_count=bin_count)
    cb_edges = [(b.lower_p, b.upper_p) for b in diagram.bins]
    rg_edges = _rg_equal_width_bins(bin_count)
    assert cb_edges == rg_edges


def test_top_bin_boundary_at_bin_count_10() -> None:
    """Predictions on bin boundaries land in the expected bins.

    With ``bin_count=10`` and edges ``[0.0, 0.1, 0.2, ..., 1.0]``:

    * ``0.0`` lands in bin 0 (``0.0 <= 0.0 < 0.1``).
    * ``0.1`` lands in bin 1 (half-open boundary; ``0.1 <= 0.1 < 0.2``).
    * ``0.5`` lands in bin 5 (``0.5 <= 0.5 < 0.6``).
    * ``0.9999`` lands in bin 9 (``0.9 <= 0.9999 < 1.0``).
    * ``1.0`` lands in bin 9 (the inclusive-top-bin rule).
    """
    pairs = [
        (0.0, 0),
        (0.1, 1),
        (0.5, 0),
        (0.9999, 1),
        (1.0, 1),
    ]
    diagram = compute_bins(pairs, bin_count=10)
    counts = [b.count for b in diagram.bins]
    # bin 0: 0.0 → 1, bin 1: 0.1 → 1, bin 5: 0.5 → 1, bin 9: 0.9999 + 1.0 → 2.
    expected_counts = [1, 1, 0, 0, 0, 1, 0, 0, 0, 2]
    assert counts == expected_counts
    # bin 9 specifically should carry both 0.9999 and 1.0.
    top_bin = diagram.bins[9]
    assert top_bin.count == 2
    assert top_bin.mean_predicted_p is not None
    assert top_bin.mean_predicted_p == pytest.approx((0.9999 + 1.0) / 2)
    assert top_bin.empirical_rate == pytest.approx(1.0)


def test_4_decimal_rounding_at_bin_count_3() -> None:
    """``bin_count=3`` produces 4-decimal-rounded edges, not float noise.

    Without ``round(..., 4)`` the third edge would be
    ``0.30000000000000004 + 0.3 = 0.6000000000000001``; the rounding
    convention pins it at ``0.6667`` to match report_generator.
    """
    diagram = compute_bins([], bin_count=3)
    edges = [(b.lower_p, b.upper_p) for b in diagram.bins]
    assert edges == [(0.0, 0.3333), (0.3333, 0.6667), (0.6667, 1.0)]


def test_empty_bins_emit_count_zero_and_none() -> None:
    """Empty bins carry ``count=0`` and ``None`` mean / empirical rates."""
    pairs = [(0.05, 0), (0.95, 1)]
    diagram = compute_bins(pairs, bin_count=10)
    assert isinstance(diagram, ReliabilityDiagram)
    assert len(diagram.bins) == 10
    # Bin 0 (0.0..0.1): one prediction at 0.05.
    assert diagram.bins[0].count == 1
    assert diagram.bins[0].mean_predicted_p == pytest.approx(0.05)
    assert diagram.bins[0].empirical_rate == pytest.approx(0.0)
    # Bins 1..8: empty.
    for index in range(1, 9):
        empty_bin = diagram.bins[index]
        assert empty_bin.count == 0, f"bin {index} should be empty"
        assert empty_bin.mean_predicted_p is None
        assert empty_bin.empirical_rate is None
    # Bin 9 (0.9..1.0): one prediction at 0.95.
    assert diagram.bins[9].count == 1
    assert diagram.bins[9].mean_predicted_p == pytest.approx(0.95)
    assert diagram.bins[9].empirical_rate == pytest.approx(1.0)


def test_bin_count_one_raises_backtest_config_error() -> None:
    """``bin_count < 2`` raises :class:`BacktestConfigError`.

    The loader clamps to ``[2, 50]`` silently — calibration_backtest
    must guard explicitly so a passthrough caller cannot construct a
    degenerate one-bin diagram (T-CB-022 deliverables, design §3.6).
    """
    with pytest.raises(BacktestConfigError, match="bin_count >= 2"):
        compute_bins([(0.5, 1)], bin_count=1)


def test_bin_count_zero_raises_backtest_config_error() -> None:
    """``bin_count == 0`` raises :class:`BacktestConfigError`."""
    with pytest.raises(BacktestConfigError, match="bin_count >= 2"):
        compute_bins([], bin_count=0)


def test_negative_bin_count_raises_backtest_config_error() -> None:
    """Negative ``bin_count`` raises :class:`BacktestConfigError`."""
    with pytest.raises(BacktestConfigError, match="bin_count >= 2"):
        compute_bins([], bin_count=-3)


def test_hand_computed_reliability_two_bins() -> None:
    """Hand-computed reference: 4 predictions in 2 bins.

    With ``bin_count=2`` the edges are ``[(0.0, 0.5), (0.5, 1.0)]``.
    Predictions:

    * ``(0.1, 0)`` and ``(0.4, 1)`` fall in bin 0; mean_p = 0.25,
      empirical = 0.5.
    * ``(0.6, 1)`` and ``(0.9, 1)`` fall in bin 1; mean_p = 0.75,
      empirical = 1.0.
    """
    pairs = [
        (0.1, 0),
        (0.4, 1),
        (0.6, 1),
        (0.9, 1),
    ]
    diagram = compute_bins(pairs, bin_count=2)
    assert len(diagram.bins) == 2
    bin_0 = diagram.bins[0]
    assert bin_0.count == 2
    assert bin_0.mean_predicted_p is not None
    assert bin_0.empirical_rate is not None
    assert abs(bin_0.mean_predicted_p - 0.25) < 1e-9
    assert abs(bin_0.empirical_rate - 0.5) < 1e-9
    bin_1 = diagram.bins[1]
    assert bin_1.count == 2
    assert bin_1.mean_predicted_p is not None
    assert bin_1.empirical_rate is not None
    assert abs(bin_1.mean_predicted_p - 0.75) < 1e-9
    assert abs(bin_1.empirical_rate - 1.0) < 1e-9


def test_compute_bins_returns_exactly_bin_count_bins() -> None:
    """The returned diagram's bin count matches the requested bin_count."""
    for bin_count in (2, 3, 5, 10, 20, 50):
        diagram = compute_bins([], bin_count=bin_count)
        assert len(diagram.bins) == bin_count
        assert diagram.bin_count == bin_count


def test_compute_bins_p_outside_unit_interval_raises() -> None:
    """``model_p`` outside ``[0.0, 1.0]`` surfaces a structured error."""
    with pytest.raises(BacktestConfigError, match="outside"):
        compute_bins([(1.5, 1)], bin_count=10)
    with pytest.raises(BacktestConfigError, match="outside"):
        compute_bins([(-0.1, 0)], bin_count=10)
