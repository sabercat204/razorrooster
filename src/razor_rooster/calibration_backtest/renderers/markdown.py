"""Markdown renderer for ``calibration-backtest run`` output (T-CB-029).

Mirrors :mod:`renderers.terminal` but emits GitHub-Flavoured-Markdown
tables and headings instead of fixed-width text. Reliability diagrams
are emitted as fenced code blocks (``\\`\\`\\`text``) carrying a textual
summary of each bin: a small ASCII bar plus the underlying statistics.
SVG embedding is deliberately omitted from the markdown surface
because GitHub, Bitbucket, and most static-site renderers strip or
silently swallow inline ``<svg>`` blocks; the operator who wants the
graphical diagram should render the HTML output instead.

The full output is passed through
:func:`razor_rooster.calibration_backtest.frame.check_cli_framing`
before return so any forbidden imperative phrase introduced into
chrome copy fails the build.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from razor_rooster.calibration_backtest.frame import (
    DISCLAIMER,
    FOOTER_NOTE,
    check_cli_framing,
)
from razor_rooster.calibration_backtest.models import BacktestRun

_BRIER_DECIMALS: int = 4
_RATE_DECIMALS: int = 4
_FALLBACK_NOTE_THRESHOLD: float = 0.05


def render_markdown(run: BacktestRun) -> str:
    """Render ``run`` as a Markdown document.

    Output sections (top-down):

    1. Title + disclaimer block.
    2. Run header (key/value table).
    3. Prediction counts.
    4. Overall Brier.
    5. Per-sector Brier table.
    6. Per-class Brier table.
    7. Fallback-polarity rate (with a >5% note when applicable).
    8. Reliability-diagram code blocks per sector.
    9. Footer note.

    The final string ends with a newline so it concatenates cleanly
    when piped to ``cat`` or appended to an existing transcript.

    Args:
        run: The :class:`BacktestRun` row to render.

    Returns:
        Linter-cleared Markdown text.

    Raises:
        ImperativeLanguageDetected: if any chrome copy carries a
            forbidden phrase.
    """

    lines: list[str] = []
    lines.append("# Calibration Backtest")
    lines.append("")
    lines.append("> Decision-support analysis. The operator decides; the system describes.")
    lines.append("")
    lines.append("## Disclaimer")
    lines.append("")
    lines.append(DISCLAIMER)
    lines.append("")

    lines.append("## Run header")
    lines.append("")
    lines.append("| field | value |")
    lines.append("| --- | --- |")
    lines.append(f"| run_id | `{run.run_id}` |")
    lines.append(f"| since_ts | {_iso(run.since_ts)} |")
    lines.append(f"| until_ts | {_iso(run.until_ts)} |")
    lines.append(f"| lag_days | {run.lag_days} |")
    lines.append(f"| library_version | {run.library_version} |")
    lines.append(f"| system_revision | `{run.system_revision}` |")
    lines.append(f"| status | {run.status.value} |")
    lines.append("")

    lines.append("## Prediction counts")
    lines.append("")
    lines.append("| field | count |")
    lines.append("| --- | ---: |")
    lines.append(f"| total | {run.predictions_total} |")
    lines.append(f"| scored | {run.predictions_scored} |")
    lines.append(f"| skipped | {run.predictions_skipped} |")
    lines.append("")

    lines.append("## Overall Brier")
    lines.append("")
    lines.append(f"`overall_brier` = {_fmt_optional_float(run.overall_brier)}")
    lines.append("")

    per_sector = _per_sector_brier(run)
    lines.append("## Per-sector Brier")
    lines.append("")
    if per_sector:
        lines.extend(_render_md_brier_table("sector", per_sector))
    else:
        lines.append("_no sector breakdown available_")
    lines.append("")

    per_class = _per_class_brier(run)
    lines.append("## Per-class Brier")
    lines.append("")
    if per_class:
        lines.extend(_render_md_brier_table("class_id", per_class))
    else:
        lines.append("_no class breakdown available_")
    lines.append("")

    fallback_rate = _fallback_polarity_rate(run)
    lines.append("## Fallback polarity")
    lines.append("")
    lines.append(f"- count: {run.fallback_polarity_count}")
    lines.append(f"- rate: {_fmt_rate(fallback_rate)}")
    if fallback_rate is not None and fallback_rate > _FALLBACK_NOTE_THRESHOLD:
        lines.append(
            "- note: fallback rate exceeds 5%; the operator may want to review polarity coverage."
        )
    lines.append("")

    diagrams = _reliability_diagrams(run)
    if diagrams:
        lines.append("## Reliability diagrams")
        lines.append("")
        for sector, diagram in sorted(diagrams.items()):
            lines.append(f"### Sector: {sector}")
            lines.append("")
            lines.append("```text")
            lines.extend(_render_diagram_block(diagram))
            lines.append("```")
            lines.append("")

    lines.append("## Footer")
    lines.append("")
    lines.append(FOOTER_NOTE)
    lines.append("")

    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    check_cli_framing(text)
    return text


def _per_sector_brier(run: BacktestRun) -> Mapping[str, float]:
    summary = run.summary_json or {}
    raw = summary.get("per_sector_brier") if isinstance(summary, dict) else None
    if isinstance(raw, dict):
        return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}
    return {}


def _per_class_brier(run: BacktestRun) -> Mapping[str, float]:
    summary = run.summary_json or {}
    raw = summary.get("per_class_brier") if isinstance(summary, dict) else None
    if isinstance(raw, dict):
        return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}
    return {}


def _fallback_polarity_rate(run: BacktestRun) -> float | None:
    summary = run.summary_json or {}
    if isinstance(summary, dict):
        raw = summary.get("fallback_polarity_rate")
        if isinstance(raw, (int, float)):
            return float(raw)
    if run.predictions_scored <= 0:
        return None
    return run.fallback_polarity_count / run.predictions_scored


def _reliability_diagrams(run: BacktestRun) -> Mapping[str, Mapping[str, Any]]:
    """Pull the per-sector reliability-diagram payload from ``summary_json``.

    The persisted shape is the dict produced by
    :func:`models._reliability_diagram_to_mapping`: an outer dict keyed
    by sector, each value carrying ``bin_count`` and an ordered ``bins``
    list. The renderer is permissive about missing keys so a partially
    populated summary still renders cleanly.
    """

    summary = run.summary_json or {}
    if not isinstance(summary, dict):
        return {}
    raw = summary.get("reliability_diagrams")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Mapping[str, Any]] = {}
    for sector, diagram in raw.items():
        if isinstance(diagram, dict):
            out[str(sector)] = diagram
    return out


def _render_md_brier_table(key_label: str, mapping: Mapping[str, float]) -> list[str]:
    """Render a Markdown table row-by-row, right-aligning the brier column."""

    rows = sorted(mapping.items())
    out: list[str] = []
    out.append(f"| {key_label} | brier |")
    out.append("| --- | ---: |")
    for key, value in rows:
        out.append(f"| {key} | {_fmt_float(value)} |")
    return out


def _render_diagram_block(diagram: Mapping[str, Any]) -> list[str]:
    """Emit a textual reliability summary inside a fenced code block.

    Each bin is rendered as ``[lo, hi)  count  mean_p  empirical`` with
    ``count == 0`` bins surfaced as ``(empty)`` so the operator sees the
    zero coverage explicitly. The block is fixed-width so the fenced
    output stays aligned in any markdown viewer.
    """

    bins = diagram.get("bins")
    if not isinstance(bins, list):
        return ["(no bin data)"]
    out: list[str] = []
    out.append("range            count   mean_p   empirical")
    out.append("--------------- -----  -------  ----------")
    for bin_entry in bins:
        if not isinstance(bin_entry, dict):
            continue
        lo = bin_entry.get("lower_p")
        hi = bin_entry.get("upper_p")
        count = bin_entry.get("count")
        mean_p = bin_entry.get("mean_predicted_p")
        emp = bin_entry.get("empirical_rate")
        range_str = f"[{_fmt_optional_float(lo)}, {_fmt_optional_float(hi)})"
        if isinstance(count, (int, float)) and int(count) == 0:
            out.append(f"{range_str:<15} {int(count):>5}  (empty)   (empty)")
            continue
        out.append(
            f"{range_str:<15} "
            f"{int(count) if isinstance(count, (int, float)) else 0:>5}  "
            f"{_fmt_optional_float(mean_p):>7}  "
            f"{_fmt_optional_float(emp):>10}"
        )
    return out


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _fmt_float(value: float) -> str:
    return f"{value:.{_BRIER_DECIMALS}f}"


def _fmt_optional_float(value: object) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, (int, float)):
        return f"{float(value):.{_BRIER_DECIMALS}f}"
    return str(value)


def _fmt_rate(value: float | None) -> str:
    if value is None:
        return "(none)"
    return f"{value:.{_RATE_DECIMALS}f}"


__all__ = ["render_markdown"]
