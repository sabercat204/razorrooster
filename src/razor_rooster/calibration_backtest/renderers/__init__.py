"""Renderers for ``razor-rooster calibration-backtest`` outputs.

T-CB-029 (terminal / markdown / html + native SVG) and T-CB-030 (JSON)
land here. Each renderer consumes a :class:`BacktestRun` (the row
hydrated by :mod:`persistence.operations` after the replay loop
completes) and returns a string the CLI prints to stdout.

The terminal, markdown, and HTML renderers run their final output
through :func:`razor_rooster.calibration_backtest.frame.check_cli_framing`
before returning so any forbidden imperative phrase introduced in
chrome strings (headings, table labels, footer notes) trips the
:class:`razor_rooster.position_engine.frame.linter.ImperativeLanguageDetected`
linter at build time, not in front of the operator. The JSON renderer
intentionally bypasses the linter because its consumer is a tool, not
an operator (REQ-CB-CLI-003); the ``disclaimer`` field carries the
canonical copy of the framing block instead.

The HTML renderer embeds reliability diagrams as native SVG produced
by :func:`reliability_svg.render_reliability_svg`. The Scout amendment
(2026-05-31) determined ``report_generator`` exposes no SVG helper
(its only chart surface is an 11x21 ASCII grid wrapped in ``<pre>``),
so calibration_backtest renders SVG natively. Bit-equality with
``report_generator`` applies to the bin-tuple inputs (parity-locked
to ``_equal_width_bins`` in T-CB-022), not to rendered SVG bytes.
"""

from __future__ import annotations

from razor_rooster.calibration_backtest.renderers.html import render_html
from razor_rooster.calibration_backtest.renderers.json_renderer import render_json
from razor_rooster.calibration_backtest.renderers.markdown import render_markdown
from razor_rooster.calibration_backtest.renderers.reliability_svg import (
    render_reliability_svg,
)
from razor_rooster.calibration_backtest.renderers.terminal import render_terminal

__all__ = [
    "render_html",
    "render_json",
    "render_markdown",
    "render_reliability_svg",
    "render_terminal",
]
