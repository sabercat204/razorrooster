"""Terminal-format renderer for ``calibration-backtest run`` output (T-CB-029).

Produces a plain-text rendering of one :class:`BacktestRun` row suitable
for piping to a terminal. The layout mirrors the structure rendered by
the markdown and HTML surfaces — disclaimer, run header, prediction
counts, overall Brier, per-sector / per-class Brier tables, the
fallback-polarity rate (with a >5% note when applicable), and the
footer note — but uses fixed-width column alignment instead of
markdown pipes.

The final string is passed through
:func:`razor_rooster.calibration_backtest.frame.check_cli_framing`
before return so a forbidden imperative phrase introduced into chrome
copy fails the build, not the operator's eyes.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from razor_rooster.calibration_backtest.frame import (
    DISCLAIMER,
    FOOTER_NOTE,
    check_cli_framing,
)
from razor_rooster.calibration_backtest.models import BacktestRun

_RUN_ID_PREFIX_LEN: int = 12
_SYSTEM_REVISION_PREFIX_LEN: int = 16
_BRIER_DECIMALS: int = 4
_RATE_DECIMALS: int = 4
_FALLBACK_NOTE_THRESHOLD: float = 0.05


def render_terminal(run: BacktestRun) -> str:
    """Render ``run`` as a plain-text terminal block.

    The output is composed top-down so it reads naturally when piped
    to ``less`` or captured by an operator transcript: disclaimer →
    run header → counts → overall Brier → per-sector / per-class
    breakdown → fallback-polarity → footer note. Numeric fields use
    ``_BRIER_DECIMALS`` decimals so the operator sees the same
    rounding the JSON renderer emits.

    Args:
        run: The :class:`BacktestRun` row produced by the replay loop.

    Returns:
        Linter-cleared plain-text rendering ending with a single
        newline so command-line composition (``echo``, ``cat``)
        behaves predictably.

    Raises:
        ImperativeLanguageDetected: if any chrome copy carries a
            forbidden phrase.
    """

    lines: list[str] = []
    lines.append("Calibration Backtest — Decision-support analysis")
    lines.append("=" * 64)
    lines.append("")
    lines.append("Disclaimer:")
    for paragraph in _wrap_paragraph(DISCLAIMER, width=72):
        lines.append(f"  {paragraph}")
    lines.append("")

    lines.append("Run header")
    lines.append("-" * 64)
    lines.append(f"  run_id           : {run.run_id[:_RUN_ID_PREFIX_LEN]}")
    lines.append(f"  since_ts         : {_iso(run.since_ts)}")
    lines.append(f"  until_ts         : {_iso(run.until_ts)}")
    lines.append(f"  lag_days         : {run.lag_days}")
    lines.append(f"  library_version  : {run.library_version}")
    lines.append(f"  system_revision  : {run.system_revision[:_SYSTEM_REVISION_PREFIX_LEN]}")
    lines.append(f"  status           : {run.status.value}")
    lines.append("")

    lines.append("Prediction counts")
    lines.append("-" * 64)
    lines.append(f"  total            : {run.predictions_total}")
    lines.append(f"  scored           : {run.predictions_scored}")
    lines.append(f"  skipped          : {run.predictions_skipped}")
    lines.append("")

    lines.append("Overall Brier")
    lines.append("-" * 64)
    lines.append(f"  overall_brier    : {_fmt_optional_float(run.overall_brier)}")
    lines.append("")

    per_sector = _per_sector_brier(run)
    lines.append("Per-sector Brier")
    lines.append("-" * 64)
    if per_sector:
        lines.extend(_render_brier_table("sector", per_sector))
    else:
        lines.append("  (no sector breakdown available)")
    lines.append("")

    per_class = _per_class_brier(run)
    lines.append("Per-class Brier")
    lines.append("-" * 64)
    if per_class:
        lines.extend(_render_brier_table("class_id", per_class))
    else:
        lines.append("  (no class breakdown available)")
    lines.append("")

    fallback_rate = _fallback_polarity_rate(run)
    lines.append("Fallback polarity")
    lines.append("-" * 64)
    lines.append(f"  fallback_count   : {run.fallback_polarity_count}")
    lines.append(f"  fallback_rate    : {_fmt_rate(fallback_rate)}")
    if fallback_rate is not None and fallback_rate > _FALLBACK_NOTE_THRESHOLD:
        lines.append(
            "  note             : fallback rate exceeds 5%; the operator may "
            "want to review polarity coverage."
        )
    lines.append("")

    lines.append("Footer")
    lines.append("-" * 64)
    for paragraph in _wrap_paragraph(FOOTER_NOTE, width=72):
        lines.append(f"  {paragraph}")
    lines.append("")

    text = "\n".join(lines) + "\n"
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
    """Derive the fallback rate, preferring the persisted summary value."""

    summary = run.summary_json or {}
    if isinstance(summary, dict):
        raw = summary.get("fallback_polarity_rate")
        if isinstance(raw, (int, float)):
            return float(raw)
    if run.predictions_scored <= 0:
        return None
    return run.fallback_polarity_count / run.predictions_scored


def _render_brier_table(key_label: str, mapping: Mapping[str, float]) -> list[str]:
    """Render a 2-column ``key | brier`` table with right-padded keys."""

    rows = sorted(mapping.items())
    key_width = max(len(key_label), max((len(k) for k, _ in rows), default=0))
    header = f"  {key_label.ljust(key_width)}    brier"
    rule = f"  {'-' * key_width}    {'-' * (3 + _BRIER_DECIMALS)}"
    out: list[str] = [header, rule]
    for key, value in rows:
        out.append(f"  {key.ljust(key_width)}    {_fmt_float(value)}")
    return out


def _iso(ts: datetime) -> str:
    """Render a timestamp as ISO-8601 with timezone preserved."""

    return ts.isoformat()


def _fmt_float(value: float) -> str:
    return f"{value:.{_BRIER_DECIMALS}f}"


def _fmt_optional_float(value: float | None) -> str:
    if value is None:
        return "(none)"
    return _fmt_float(value)


def _fmt_rate(value: float | None) -> str:
    if value is None:
        return "(none)"
    return f"{value:.{_RATE_DECIMALS}f}"


def _wrap_paragraph(text: str, *, width: int) -> list[str]:
    """Word-wrap ``text`` at ``width`` columns without splitting words.

    Avoids :mod:`textwrap` so the renderer stays self-contained and
    behaves predictably with the disclaimer's pre-flowed copy.
    """

    words = text.split()
    if not words:
        return [""]
    out: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        added = len(word) + (1 if current else 0)
        if current and current_len + added > width:
            out.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += added
    if current:
        out.append(" ".join(current))
    return out


__all__ = ["render_terminal"]
