"""HTML renderer for ``calibration-backtest run`` output (T-CB-029).

Produces a self-contained HTML document with inline CSS — no template
engine, no external assets, no JavaScript — mirroring the
``report_generator.renderer.html`` idiom (plain string concatenation
plus an ``html.escape`` helper for operator-supplied values). Embeds
inline SVG reliability diagrams produced by
:func:`reliability_svg.render_reliability_svg`.

The full output is passed through
:func:`razor_rooster.calibration_backtest.frame.check_cli_framing`
before return so a forbidden imperative phrase introduced in chrome
copy fails the build, not the operator.
"""

from __future__ import annotations

import html as _html_module
from collections.abc import Mapping
from datetime import datetime

from razor_rooster.calibration_backtest.frame import (
    DISCLAIMER,
    FOOTER_NOTE,
    check_cli_framing,
)
from razor_rooster.calibration_backtest.models import BacktestRun
from razor_rooster.calibration_backtest.renderers._diagram_hydrate import (
    reliability_diagrams_from_run as _reliability_diagrams_from_run,
)
from razor_rooster.calibration_backtest.renderers.reliability_svg import (
    render_reliability_svg,
)

_BRIER_DECIMALS: int = 4
_RATE_DECIMALS: int = 4
_FALLBACK_NOTE_THRESHOLD: float = 0.05

_INLINE_CSS: str = """
:root {
    --bg: #fafafa;
    --fg: #1a1a1a;
    --muted: #6b6b6b;
    --accent: #0066aa;
    --warn: #aa4400;
    --warn-bg: #fff0e0;
    --border: #d0d0d0;
    --table-bg: #ffffff;
}
@media (prefers-color-scheme: dark) {
    :root {
        --bg: #1a1a1a;
        --fg: #f0f0f0;
        --muted: #a0a0a0;
        --accent: #66aaff;
        --warn: #ffaa66;
        --warn-bg: #4a2a10;
        --border: #404040;
        --table-bg: #252525;
    }
}
* { box-sizing: border-box; }
body {
    background: var(--bg);
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                 Roboto, sans-serif;
    line-height: 1.5;
    margin: 0;
    padding: 1.5rem;
    max-width: 60rem;
    margin-left: auto;
    margin-right: auto;
}
h1 { font-size: 1.6rem; margin: 0 0 0.5rem; }
h2 {
    font-size: 1.2rem;
    margin: 1.5rem 0 0.5rem;
    padding-bottom: 0.25rem;
    border-bottom: 1px solid var(--border);
}
h3 { font-size: 1.05rem; margin: 1rem 0 0.5rem; }
section { margin-bottom: 2rem; }
section.disclaimer, section.footer-note {
    background: var(--table-bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.75rem 1rem;
    color: var(--muted);
}
.warn-banner {
    background: var(--warn-bg);
    color: var(--warn);
    border-left: 3px solid var(--warn);
    padding: 0.5rem 0.75rem;
    margin: 0.5rem 0;
}
table {
    background: var(--table-bg);
    border-collapse: collapse;
    margin: 0.5rem 0;
    width: 100%;
    max-width: 36rem;
}
th, td {
    border: 1px solid var(--border);
    padding: 0.25rem 0.5rem;
    text-align: left;
}
th { background: var(--bg); }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.muted { color: var(--muted); }
.diagram { margin: 0.5rem 0; }
"""


def render_html(run: BacktestRun) -> str:
    """Render ``run`` as a full HTML document.

    Output structure (top-down):

    1. ``<!DOCTYPE html>`` + ``<head>`` with embedded CSS.
    2. ``<h1>`` title and ``<section class="disclaimer">`` block.
    3. Run-header definition table.
    4. Prediction-counts table.
    5. Overall Brier paragraph.
    6. Per-sector / per-class Brier tables.
    7. Fallback-polarity block (with a warn banner when >5%).
    8. Per-sector reliability diagrams as inline SVG.
    9. ``<section class="footer-note">`` block.

    Args:
        run: The :class:`BacktestRun` row to render.

    Returns:
        Linter-cleared HTML text.

    Raises:
        ImperativeLanguageDetected: if any chrome copy carries a
            forbidden phrase.
    """

    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="en">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8" />')
    parts.append(f"<title>Calibration Backtest — {_html(run.run_id[:12])}</title>")
    parts.append(f"<style>{_INLINE_CSS}</style>")
    parts.append("</head>")
    parts.append("<body>")

    parts.append("<h1>Calibration Backtest</h1>")
    parts.append('<p class="muted">Decision-support analysis.</p>')

    parts.append('<section class="disclaimer">')
    parts.append("<h2>Disclaimer</h2>")
    parts.append(f"<p>{_html(DISCLAIMER)}</p>")
    parts.append("</section>")

    parts.append("<section>")
    parts.append("<h2>Run header</h2>")
    parts.append(_render_kv_table(_run_header_pairs(run)))
    parts.append("</section>")

    parts.append("<section>")
    parts.append("<h2>Prediction counts</h2>")
    parts.append(
        _render_kv_table(
            (
                ("total", str(run.predictions_total)),
                ("scored", str(run.predictions_scored)),
                ("skipped", str(run.predictions_skipped)),
            )
        )
    )
    parts.append("</section>")

    parts.append("<section>")
    parts.append("<h2>Overall Brier</h2>")
    parts.append(
        f"<p><code>overall_brier</code> = {_html(_fmt_optional_float(run.overall_brier))}</p>"
    )
    parts.append("</section>")

    parts.append("<section>")
    parts.append("<h2>Per-sector Brier</h2>")
    per_sector = _per_sector_brier(run)
    if per_sector:
        parts.append(_render_brier_table("sector", per_sector))
    else:
        parts.append('<p class="muted">No sector breakdown available.</p>')
    parts.append("</section>")

    parts.append("<section>")
    parts.append("<h2>Per-class Brier</h2>")
    per_class = _per_class_brier(run)
    if per_class:
        parts.append(_render_brier_table("class_id", per_class))
    else:
        parts.append('<p class="muted">No class breakdown available.</p>')
    parts.append("</section>")

    parts.append("<section>")
    parts.append("<h2>Fallback polarity</h2>")
    fallback_rate = _fallback_polarity_rate(run)
    parts.append(
        _render_kv_table(
            (
                ("count", str(run.fallback_polarity_count)),
                ("rate", _fmt_rate(fallback_rate)),
            )
        )
    )
    if fallback_rate is not None and fallback_rate > _FALLBACK_NOTE_THRESHOLD:
        parts.append(
            '<p class="warn-banner">Fallback rate exceeds 5%; the operator '
            "may want to review polarity coverage.</p>"
        )
    parts.append("</section>")

    diagrams = _reliability_diagrams_from_run(run)
    if diagrams:
        parts.append("<section>")
        parts.append("<h2>Reliability diagrams</h2>")
        for sector, diagram in sorted(diagrams.items()):
            parts.append(f"<h3>Sector: {_html(sector)}</h3>")
            parts.append('<div class="diagram">')
            parts.append(render_reliability_svg(diagram, sector_label=sector))
            parts.append("</div>")
        parts.append("</section>")

    parts.append('<section class="footer-note">')
    parts.append("<h2>Footer</h2>")
    parts.append(f"<p>{_html(FOOTER_NOTE)}</p>")
    parts.append("</section>")

    parts.append("</body>")
    parts.append("</html>")

    text = "\n".join(parts) + "\n"
    check_cli_framing(text)
    return text


def _run_header_pairs(run: BacktestRun) -> tuple[tuple[str, str], ...]:
    return (
        ("run_id", run.run_id[:12]),
        ("since_ts", _iso(run.since_ts)),
        ("until_ts", _iso(run.until_ts)),
        ("lag_days", str(run.lag_days)),
        ("library_version", str(run.library_version)),
        ("system_revision", run.system_revision[:16]),
        ("status", run.status.value),
    )


def _render_kv_table(rows: tuple[tuple[str, str], ...]) -> str:
    body = "".join(f"<tr><th>{_html(k)}</th><td>{_html(v)}</td></tr>" for k, v in rows)
    return f"<table>{body}</table>"


def _render_brier_table(key_label: str, mapping: Mapping[str, float]) -> str:
    head = f"<thead><tr><th>{_html(key_label)}</th><th>brier</th></tr></thead>"
    rows = sorted(mapping.items())
    body = "".join(
        f'<tr><td>{_html(k)}</td><td class="num">{_html(_fmt_float(v))}</td></tr>' for k, v in rows
    )
    return f"<table>{head}<tbody>{body}</tbody></table>"


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


def _iso(ts: datetime) -> str:
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


def _html(value: object) -> str:
    """HTML-escape any value safely (mirror ``report_generator``'s helper)."""

    if value is None:
        return ""
    return _html_module.escape(str(value), quote=True)


__all__ = ["render_html"]
