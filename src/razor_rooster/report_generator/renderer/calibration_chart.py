"""ASCII calibration-chart helper (T-RG-COMPAT-CHART-001 v0.40.0).

A small ASCII overlay showing the perfect-calibration diagonal and
the empirical curve so operators can see at a glance whether the
model is biased high or low across the probability range.

Shared by both the terminal and markdown renderers. Markdown wraps
the chart in a fenced code block so monospace alignment is
preserved across most Markdown viewers.

Output dimensions: 11 rows x 21 cols (each cell is 0.1 tall and
0.05 wide). Glyphs:

- ``.`` (middle dot) — diagonal (perfect calibration)
- ``*`` — non-sparse bin observed at (mean_predicted, empirical_rate)
- ``+`` — sparse bin (same coordinates, different glyph so the
  operator's eye distinguishes them)
- ``#`` — empirical bin lands exactly on the diagonal cell
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

DIAGONAL_GLYPH = "."  # ASCII (avoids unicode round-trip issues in tests)
EMPIRICAL_GLYPH = "*"
SPARSE_GLYPH = "+"
COINCIDENT_GLYPH = "#"

CHART_ROWS = 11  # y axis: 0.0 to 1.0 in 0.1 steps
CHART_COLS = 21  # x axis: 0.0 to 1.0 in 0.05 steps


def render_chart(bin_entries: Iterable[Mapping[str, Any]]) -> str:
    """Render the per-sector calibration chart as a multi-line ASCII string.

    Returns an empty string when no bin has data; the caller should
    suppress the chart in that case.
    """
    points: list[tuple[float, float, bool]] = []
    for bin_entry in bin_entries:
        n = int(bin_entry.get("n", 0))
        if n == 0:
            continue
        mean_p = bin_entry.get("mean_predicted")
        empirical = bin_entry.get("empirical_rate")
        if mean_p is None or empirical is None:
            continue
        points.append((float(mean_p), float(empirical), bool(bin_entry.get("sparse", False))))
    if not points:
        return ""

    grid: list[list[str]] = [[" " for _ in range(CHART_COLS)] for _ in range(CHART_ROWS)]

    # Perfect-calibration diagonal first; bin markers can overwrite
    # where they coincide.
    for c in range(CHART_COLS):
        x = c / (CHART_COLS - 1)
        r = round(x * (CHART_ROWS - 1))
        grid[CHART_ROWS - 1 - r][c] = DIAGONAL_GLYPH

    for mean_p, empirical, sparse in points:
        c = round(mean_p * (CHART_COLS - 1))
        r = round(empirical * (CHART_ROWS - 1))
        c = max(0, min(CHART_COLS - 1, c))
        r = max(0, min(CHART_ROWS - 1, r))
        row_index = CHART_ROWS - 1 - r
        existing = grid[row_index][c]
        marker = SPARSE_GLYPH if sparse else EMPIRICAL_GLYPH
        if existing in (EMPIRICAL_GLYPH, SPARSE_GLYPH):
            # Two points landed in the same cell; keep the
            # non-sparse marker if any.
            if existing == EMPIRICAL_GLYPH or marker == EMPIRICAL_GLYPH:
                grid[row_index][c] = EMPIRICAL_GLYPH
            else:
                grid[row_index][c] = SPARSE_GLYPH
        elif existing == DIAGONAL_GLYPH:
            grid[row_index][c] = COINCIDENT_GLYPH
        else:
            grid[row_index][c] = marker

    plot_lines: list[str] = []
    for row_index in range(CHART_ROWS):
        prefix = "        |"
        if row_index == 0:
            prefix = "    1.0 |"
        elif row_index == CHART_ROWS - 1:
            prefix = "    0.0 |"
        elif row_index == CHART_ROWS // 2:
            prefix = "    0.5 |"
        plot_lines.append(prefix + "".join(grid[row_index]))
    plot_lines.append("        +" + "-" * CHART_COLS)
    # Tick label row: "0.0" left-aligned, "1.0" right-aligned over the axis.
    label_row = "0.0" + " " * (CHART_COLS - 6) + "1.0"
    plot_lines.append("         " + label_row)
    plot_lines.append(
        "         (x = mean predicted; y = empirical rate; "
        ". = perfect calibration; * = bin; + = sparse; # = both)"
    )
    return "\n".join(plot_lines)


__all__ = [
    "CHART_COLS",
    "CHART_ROWS",
    "COINCIDENT_GLYPH",
    "DIAGONAL_GLYPH",
    "EMPIRICAL_GLYPH",
    "SPARSE_GLYPH",
    "render_chart",
]
