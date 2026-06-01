"""Native SVG rendering for calibration_backtest reliability diagrams (T-CB-029).

Scout amendment (2026-05-31): ``report_generator`` does **not** expose
an SVG helper anywhere — its only chart surface is
``render_chart()``, an 11x21 ASCII grid wrapped in ``<pre>`` for the
terminal/markdown/html daily report. The CALIBRATION_BACKTEST design's
"imports report_generator.engines.section_assemblers.reliability to
produce bit-equal SVG diagrams" is unimplementable as written.
calibration_backtest therefore renders reliability diagrams as SVG
natively here. Bit-equality with ``report_generator`` (REQ-CB-SCORE-004
/ P-CB-4) applies to the bin-tuple **inputs** — already parity-locked
to ``report_generator._equal_width_bins`` by T-CB-022 — not to the
rendered SVG bytes.

Output shape:

* ``<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 W H" ...>``
  with explicit ``viewBox`` so embedding is size-stable across
  containers.
* Two ``<line>`` axes drawn at the inner padding rectangle
  (``[padding, W - padding]`` x ``[padding, H - padding]``).
* One ``<line>`` for the y=x perfect-calibration reference, from
  ``(pad, H - pad)`` to ``(W - pad, pad)`` (SVG y is top-down so the
  diagonal climbs as x increases).
* One ``<rect>`` per **non-empty** bin sized to its ``empirical_rate``
  (height) and to the bin's ``[lower_p, upper_p]`` width.
* One ``<circle>`` marker per non-empty bin at
  ``(mean_predicted_p, empirical_rate)`` so the operator can compare
  the mean predicted probability against the empirical resolution rate
  at a glance.
* Empty/sparse bins (``count == 0``) are emitted as dashed
  ``<rect class="sparse" .../>`` placeholders covering the bin width
  with zero height — they signal "no data" without contributing a
  visible bar.
* Inline ``<style>`` so embedded SVG renders correctly when cut/pasted
  standalone.

Operator-supplied strings (sector name / axis labels) are HTML-escaped
via :func:`html.escape` before embedding in ``<text>`` nodes; the
upstream model also rejects sector names that fail the dataclass
validation, but the escape protects against an HTML smuggler getting
through anyway. Numeric values are safe by construction.
"""

from __future__ import annotations

import html as _html_module
from typing import Final

from razor_rooster.calibration_backtest.errors import BacktestConfigError
from razor_rooster.calibration_backtest.models import ReliabilityDiagram

_SVG_XMLNS: Final[str] = "http://www.w3.org/2000/svg"

_INLINE_STYLE: Final[str] = (
    ".axis { stroke: #1a1a1a; stroke-width: 1; fill: none; }"
    " .reference { stroke: #6b6b6b; stroke-width: 1; stroke-dasharray: 4,3; fill: none; }"
    " .bar { fill: #cce0f5; stroke: #0066aa; stroke-width: 1; }"
    " .marker { fill: #aa4400; stroke: #aa4400; stroke-width: 1; }"
    " .sparse { fill: none; stroke: #b0b0b0; stroke-width: 1; stroke-dasharray: 3,3; }"
    " .label { font: 10px sans-serif; fill: #1a1a1a; }"
)


def render_reliability_svg(
    diagram: ReliabilityDiagram,
    *,
    width: int = 320,
    height: int = 320,
    padding: int = 32,
    sector_label: str | None = None,
) -> str:
    """Render ``diagram`` as a self-contained SVG string.

    Args:
        diagram: A :class:`ReliabilityDiagram` produced by the scoring
            engine (T-CB-022). Bins are assumed to cover ``[0.0, 1.0]``
            in stored order.
        width: SVG canvas width in user units. Must be > 2 * padding.
        height: SVG canvas height in user units. Must be > 2 * padding.
        padding: Inner padding around the plot area in user units.
        sector_label: Optional operator-supplied label rendered above
            the diagram. ``None`` suppresses the label. The string is
            HTML-escaped before embedding.

    Returns:
        A complete ``<svg ...>...</svg>`` string with inline ``<style>``,
        explicit ``viewBox``, and bin-by-bin rectangles + markers. The
        string is ready to embed inline in HTML or save to a file.

    Raises:
        BacktestConfigError: if ``width`` / ``height`` / ``padding`` are
            not strictly positive or leave no usable plot area.
    """

    if width <= 0:
        raise BacktestConfigError(f"render_reliability_svg.width must be > 0, got {width!r}")
    if height <= 0:
        raise BacktestConfigError(f"render_reliability_svg.height must be > 0, got {height!r}")
    if padding < 0:
        raise BacktestConfigError(f"render_reliability_svg.padding must be >= 0, got {padding!r}")
    inner_w = width - 2 * padding
    inner_h = height - 2 * padding
    if inner_w <= 0 or inner_h <= 0:
        raise BacktestConfigError(
            "render_reliability_svg: padding leaves no usable plot area "
            f"(width={width!r}, height={height!r}, padding={padding!r})"
        )

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="{_SVG_XMLNS}" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img">'
    )
    parts.append(f"<style>{_INLINE_STYLE}</style>")

    # Optional sector label (HTML-escaped).
    if sector_label is not None:
        safe_label = _html_module.escape(sector_label, quote=True)
        parts.append(
            f'<text class="label" x="{padding}" y="{max(0, padding - 8)}">{safe_label}</text>'
        )

    # Axes: bottom (x) and left (y) of the inner padding rectangle.
    plot_left = padding
    plot_right = width - padding
    plot_top = padding
    plot_bottom = height - padding
    parts.append(
        f'<line class="axis" x1="{plot_left}" y1="{plot_bottom}" '
        f'x2="{plot_right}" y2="{plot_bottom}" />'
    )
    parts.append(
        f'<line class="axis" x1="{plot_left}" y1="{plot_top}" '
        f'x2="{plot_left}" y2="{plot_bottom}" />'
    )

    # Perfect-calibration reference line: y=x diagonal in user space.
    parts.append(
        f'<line class="reference" x1="{plot_left}" y1="{plot_bottom}" '
        f'x2="{plot_right}" y2="{plot_top}" />'
    )

    # Per-bin rectangles + circle markers.
    for bin_ in diagram.bins:
        bin_x = plot_left + bin_.lower_p * inner_w
        bin_w = (bin_.upper_p - bin_.lower_p) * inner_w
        if bin_.count == 0 or bin_.empirical_rate is None or bin_.mean_predicted_p is None:
            # Sparse / empty bin: dashed outline at the baseline so the
            # reader sees the bin exists but carried no data.
            parts.append(
                f'<rect class="sparse" x="{_fmt(bin_x)}" y="{_fmt(plot_bottom - 1)}" '
                f'width="{_fmt(bin_w)}" height="1" />'
            )
            continue
        bar_h = bin_.empirical_rate * inner_h
        bar_y = plot_bottom - bar_h
        parts.append(
            f'<rect class="bar" x="{_fmt(bin_x)}" y="{_fmt(bar_y)}" '
            f'width="{_fmt(bin_w)}" height="{_fmt(bar_h)}" />'
        )
        marker_cx = plot_left + bin_.mean_predicted_p * inner_w
        marker_cy = plot_bottom - bin_.empirical_rate * inner_h
        parts.append(
            f'<circle class="marker" cx="{_fmt(marker_cx)}" cy="{_fmt(marker_cy)}" r="3" />'
        )

    parts.append("</svg>")
    return "".join(parts)


def _fmt(value: float) -> str:
    """Format a float for SVG with up to 2 decimals, trimming trailing zeros.

    SVG renderers tolerate floats but a tidy decimal keeps the output
    readable in the test fixtures and avoids scientific notation that
    some viewers mishandle.
    """

    formatted = f"{value:.2f}"
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"


__all__ = ["render_reliability_svg"]
