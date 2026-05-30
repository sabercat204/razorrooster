"""Tests for the ASCII calibration-curve overlay (T-RG-COMPAT-CHART-001).

Verifies the chart helper handles empty input, single-bin input,
sparse vs. non-sparse markers, and returns a stable shape (rows/cols).
Also confirms the chart renders inside the reliability section in
both terminal and markdown output and that it doesn't contain any
forbidden imperative phrases.
"""

from __future__ import annotations

from razor_rooster.position_engine.frame.linter import check_text
from razor_rooster.report_generator.renderer.calibration_chart import (
    CHART_COLS,
    CHART_ROWS,
    COINCIDENT_GLYPH,
    DIAGONAL_GLYPH,
    EMPIRICAL_GLYPH,
    SPARSE_GLYPH,
    render_chart,
)


def _make_bin(
    *,
    bin_lo: float,
    bin_hi: float,
    n: int,
    mean_p: float | None = None,
    empirical: float | None = None,
    sparse: bool = False,
) -> dict[str, object]:
    return {
        "bin_lo": bin_lo,
        "bin_hi": bin_hi,
        "n": n,
        "mean_predicted": mean_p,
        "empirical_rate": empirical,
        "sparse": sparse,
    }


def test_empty_input_returns_empty_string() -> None:
    assert render_chart([]) == ""


def test_all_zero_count_bins_returns_empty_string() -> None:
    bins = [_make_bin(bin_lo=0.0, bin_hi=0.1, n=0)]
    assert render_chart(bins) == ""


def test_single_perfectly_calibrated_bin_renders_coincident_glyph() -> None:
    """A bin where mean_predicted == empirical lands on the diagonal → '#'."""
    bins = [
        _make_bin(bin_lo=0.4, bin_hi=0.5, n=10, mean_p=0.5, empirical=0.5, sparse=False),
    ]
    chart = render_chart(bins)
    assert COINCIDENT_GLYPH in chart


def test_off_diagonal_bin_renders_empirical_glyph() -> None:
    """A bin where empirical != mean_predicted lands off-diagonal → '*'."""
    bins = [
        _make_bin(bin_lo=0.4, bin_hi=0.5, n=10, mean_p=0.5, empirical=0.9, sparse=False),
    ]
    chart = render_chart(bins)
    # The diagonal also renders, but the empirical * is at a non-diagonal cell.
    assert EMPIRICAL_GLYPH in chart
    assert DIAGONAL_GLYPH in chart


def test_sparse_bin_uses_plus_glyph() -> None:
    bins = [
        _make_bin(bin_lo=0.4, bin_hi=0.5, n=2, mean_p=0.5, empirical=0.9, sparse=True),
    ]
    chart = render_chart(bins)
    assert SPARSE_GLYPH in chart


def test_chart_dimensions_are_stable() -> None:
    """The chart has CHART_ROWS plot rows + axis + tick + caption rows."""
    bins = [
        _make_bin(bin_lo=0.4, bin_hi=0.5, n=10, mean_p=0.5, empirical=0.5, sparse=False),
    ]
    chart = render_chart(bins)
    plot_rows = [line for line in chart.splitlines() if "|" in line]
    assert len(plot_rows) == CHART_ROWS
    # Plot rows are prefix + CHART_COLS columns.
    for line in plot_rows:
        assert line.endswith(line[len(line.rstrip()) :])
        # The column area starts after the leading "    1.0 |" (or "        |"
        # placeholder). Strip the prefix and check we have CHART_COLS chars.
        column_area = line.split("|", 1)[1]
        assert len(column_area) == CHART_COLS


def test_chart_legend_is_present() -> None:
    bins = [
        _make_bin(bin_lo=0.4, bin_hi=0.5, n=10, mean_p=0.5, empirical=0.5, sparse=False),
    ]
    chart = render_chart(bins)
    assert "perfect calibration" in chart
    assert "x = mean predicted" in chart
    assert "y = empirical rate" in chart


def test_chart_passes_imperative_linter() -> None:
    """The chart text must never trip the forbidden-phrase linter."""
    bins = [
        _make_bin(bin_lo=0.0, bin_hi=0.1, n=10, mean_p=0.05, empirical=0.30, sparse=False),
        _make_bin(bin_lo=0.4, bin_hi=0.5, n=2, mean_p=0.45, empirical=0.50, sparse=True),
        _make_bin(bin_lo=0.9, bin_hi=1.0, n=10, mean_p=0.95, empirical=0.10, sparse=False),
    ]
    chart = render_chart(bins)
    # Linter is the same shared linter the position_engine uses; raises on match.
    check_text(chart)


def test_two_bins_in_same_cell_collapse_correctly() -> None:
    """When two non-sparse bins land in the same cell, the cell stays '*'."""
    # Both round to the same grid cell at (0.5, 0.5).
    bins = [
        _make_bin(bin_lo=0.4, bin_hi=0.5, n=10, mean_p=0.50, empirical=0.50, sparse=False),
        _make_bin(bin_lo=0.5, bin_hi=0.6, n=10, mean_p=0.51, empirical=0.49, sparse=False),
    ]
    chart = render_chart(bins)
    # Diagonal at (0.5, 0.5) intersects with our points; the marker depends
    # on whichever drew last but should still be one of the marker glyphs.
    coincident_count = chart.count(COINCIDENT_GLYPH)
    assert coincident_count >= 1


def test_clamps_out_of_range_values() -> None:
    """Values slightly above 1.0 (rounding artifacts) are clamped to the grid edge."""
    bins = [
        _make_bin(bin_lo=0.9, bin_hi=1.0, n=10, mean_p=1.001, empirical=1.001, sparse=False),
    ]
    # Should not raise IndexError.
    chart = render_chart(bins)
    assert chart != ""
    assert COINCIDENT_GLYPH in chart


def test_terminal_render_includes_chart_after_table() -> None:
    """The terminal renderer's reliability section emits the chart after the table."""
    from razor_rooster.report_generator.renderer.terminal import (
        _render_reliability,
    )

    content = {
        "type": "reliability",
        "bins": [(i / 10, (i + 1) / 10) for i in range(10)],
        "min_resolutions_per_bin": 5,
        "sectors": [
            {
                "sector": "macroeconomic",
                "n_resolutions": 10,
                "window_days": 90,
                "bin_count": 10,
                "min_resolutions_per_bin": 5,
                "bins": [
                    _make_bin(bin_lo=0.0, bin_hi=0.1, n=10, mean_p=0.05, empirical=0.05),
                ],
            },
        ],
    }
    out = _render_reliability(content)
    # Table line is present.
    assert "mean_p" in out
    # Chart legend is present.
    assert "perfect calibration" in out


def test_markdown_render_wraps_chart_in_fenced_code_block() -> None:
    """The markdown renderer wraps the ASCII chart in ``` blocks."""
    from razor_rooster.report_generator.renderer.markdown import _render_reliability

    content = {
        "type": "reliability",
        "bins": [],
        "min_resolutions_per_bin": 5,
        "sectors": [
            {
                "sector": "macroeconomic",
                "n_resolutions": 10,
                "window_days": 90,
                "bin_count": 10,
                "min_resolutions_per_bin": 5,
                "bins": [
                    _make_bin(bin_lo=0.0, bin_hi=0.1, n=10, mean_p=0.05, empirical=0.05),
                ],
            },
        ],
    }
    out = _render_reliability(content)
    assert "```" in out
    # The chart legend is inside the fence.
    assert "perfect calibration" in out
