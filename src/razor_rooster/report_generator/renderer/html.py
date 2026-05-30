"""HTML renderer for the daily report (T-RG-COMPAT-HTML-001 v0.44.0).

Operators who want a more legible reading experience than terminal
text or raw markdown can open the HTML output in a browser. The
HTML is **fully self-contained** — inline CSS only, no external
fonts, no JavaScript, no images, no network calls. Renders fine
offline; renders fine in restricted environments where the
operator can't reach a CDN.

Designed to mirror the markdown renderer's structure: the same
sections, the same per-section content shape, but real HTML
elements instead of GFM tables. The calibration chart is wrapped
in a ``<pre>`` block to preserve monospace alignment.

Color scheme picks up the operator's system preference via
``prefers-color-scheme`` so the page works in both light and dark
modes without configuration.

The generated HTML passes through the same imperative-language
linter as the terminal and markdown outputs (REQ-RG-FRAME-001
carry-forward).
"""

from __future__ import annotations

import html as _html_module
from collections.abc import Mapping, Sequence
from typing import Any

from razor_rooster.report_generator.models import SectionContent
from razor_rooster.report_generator.renderer.calibration_chart import (
    render_chart as render_calibration_chart,
)

_HEADER_LABEL_BY_SECTION: dict[str, str] = {
    "system_health": "System Health",
    "recent_tuning": "Recent Threshold Changes",
    "at_a_glance": "At a Glance",
    "surfaced": "Surfaced Comparisons",
    "cross_venue": "Cross-Venue Disagreements",
    "watched": "Active Watched Situations",
    "calibration": "Calibration Log",
    "reliability": "Reliability Diagram",
    "watchlist": "Watchlist (Developing)",
}

_EMPTY_MESSAGE_BY_SECTION: dict[str, str] = {
    "system_health": "No system-health warnings this cycle.",
    "recent_tuning": "No threshold changes since the prior report.",
    "at_a_glance": "No at-a-glance facts available this cycle.",
    "surfaced": "No comparisons surfaced this cycle.",
    "cross_venue": "No cross-venue disagreements this cycle.",
    "watched": "No watched analyses required review this cycle.",
    "calibration": "No new resolutions this cycle.",
    "reliability": "No reliability bins populated this cycle.",
    "watchlist": "No developing watchlist items this cycle.",
}


_INLINE_CSS = """
:root {
    --bg: #fafafa;
    --fg: #1a1a1a;
    --muted: #6b6b6b;
    --accent: #0066aa;
    --accent-bg: #e6f0fa;
    --warn: #aa4400;
    --warn-bg: #fff0e0;
    --border: #d0d0d0;
    --table-bg: #ffffff;
    --code-bg: #f0f0f0;
}
@media (prefers-color-scheme: dark) {
    :root {
        --bg: #1a1a1a;
        --fg: #f0f0f0;
        --muted: #a0a0a0;
        --accent: #66aaff;
        --accent-bg: #1a3050;
        --warn: #ffaa66;
        --warn-bg: #4a2a10;
        --border: #404040;
        --table-bg: #252525;
        --code-bg: #2a2a2a;
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
.muted { color: var(--muted); }
.empty {
    color: var(--muted);
    font-style: italic;
    margin: 0.5rem 0;
}
.warn-list {
    background: var(--warn-bg);
    color: var(--warn);
    border-left: 3px solid var(--warn);
    padding: 0.5rem 0.75rem;
    margin: 0.5rem 0;
    border-radius: 2px;
}
table {
    border-collapse: collapse;
    background: var(--table-bg);
    margin: 0.5rem 0;
    width: 100%;
    font-size: 0.95rem;
}
th, td {
    border: 1px solid var(--border);
    padding: 0.4rem 0.6rem;
    text-align: left;
    vertical-align: top;
}
th { background: var(--accent-bg); color: var(--accent); font-weight: 600; }
code, pre {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background: var(--code-bg);
    border-radius: 2px;
}
code { padding: 0 0.25rem; font-size: 0.9em; }
pre {
    padding: 0.75rem 1rem;
    overflow-x: auto;
    font-size: 0.85rem;
    line-height: 1.4;
}
.disclaimer {
    margin-top: 2rem;
    padding: 1rem;
    border-left: 3px solid var(--accent);
    background: var(--accent-bg);
    font-style: italic;
}
ul.case-list { margin: 0.25rem 0; padding-left: 1.5rem; }
.balanced-cases {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
}
@media (max-width: 40rem) {
    .balanced-cases { grid-template-columns: 1fr; }
}
""".strip()


def render(
    *,
    header: Mapping[str, Any],
    body_sections: Sequence[SectionContent],
    footer: Mapping[str, Any],
) -> str:
    """Render the full HTML document."""
    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="en">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(f"<title>Razor-Rooster Report — {_html(header.get('cycle_date', '?'))}</title>")
    parts.append(f"<style>{_INLINE_CSS}</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append(_render_header(header))
    for section in body_sections:
        parts.append(_render_section(section))
    parts.append(_render_footer(footer))
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


# -- internals --------------------------------------------------------------


def _render_header(header: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"<h1>Razor-Rooster Report — {_html(header.get('cycle_date', '?'))}</h1>")
    meta_parts: list[str] = []
    if header.get("report_id") is not None:
        meta_parts.append(f"report id <code>{_html(header.get('report_id'))}</code>")
    if header.get("library_version") is not None:
        meta_parts.append(f"library v{_html(header.get('library_version'))}")
    if header.get("library_age_days") is not None:
        meta_parts.append(f"library age {_html(header.get('library_age_days'))}d")
    stale = header.get("stale_source_count")
    if isinstance(stale, int):
        meta_parts.append(f"{stale} stale source(s)")
    since_ts = header.get("since_ts")
    if since_ts is not None:
        meta_parts.append(
            f"since {_html(since_ts.isoformat() if hasattr(since_ts, 'isoformat') else since_ts)}"
        )
    if meta_parts:
        lines.append(f"<p class='muted'>{' &middot; '.join(meta_parts)}</p>")
    disabled = header.get("disabled_sections") or ()
    if disabled:
        lines.append(
            f"<p class='muted'>Disabled sections: {_html(', '.join(str(d) for d in disabled))}</p>"
        )
    return "\n".join(lines)


def _render_section(section: SectionContent) -> str:
    label = _HEADER_LABEL_BY_SECTION.get(section.name, section.name.upper())
    parts: list[str] = []
    parts.append(f'<section data-section="{_html(section.name)}">')
    parts.append(f"<h2>{_html(label)}</h2>")
    if not section.ok:
        parts.append(f"<p class='warn-list'>Section error: {_html(section.error or '?')}</p>")
        parts.append("</section>")
        return "\n".join(parts)
    content = section.content or {}
    body = ""
    if section.name == "system_health":
        body = _render_system_health(content)
    elif section.name == "recent_tuning":
        body = _render_recent_tuning(content)
    elif section.name == "at_a_glance":
        body = _render_at_a_glance(content)
    elif section.name == "surfaced":
        body = _render_surfaced(content)
    elif section.name == "cross_venue":
        body = _render_cross_venue(content)
    elif section.name == "watched":
        body = _render_watched(content)
    elif section.name == "calibration":
        body = _render_calibration(content)
    elif section.name == "reliability":
        body = _render_reliability(content)
    elif section.name == "watchlist":
        body = _render_watchlist(content)
    if not body:
        body = (
            f"<p class='empty'>"
            f"{_html(_EMPTY_MESSAGE_BY_SECTION.get(section.name, '(no content)'))}"
            f"</p>"
        )
    parts.append(body)
    parts.append("</section>")
    return "\n".join(parts)


def _render_footer(footer: Mapping[str, Any]) -> str:
    parts: list[str] = []
    disclaimer_text = footer.get("disclaimer_text")
    if disclaimer_text:
        # Preserve paragraph breaks but escape entities.
        paragraphs = [p.strip() for p in str(disclaimer_text).split("\n\n") if p.strip()]
        if not paragraphs:
            paragraphs = [str(disclaimer_text)]
        body = "".join(f"<p>{_html(p)}</p>" for p in paragraphs)
        parts.append(f"<div class='disclaimer'>{body}</div>")
    meta_parts: list[str] = []
    if footer.get("system_version"):
        meta_parts.append(f"version {_html(footer.get('system_version'))}")
    if footer.get("report_id"):
        meta_parts.append(f"report id <code>{_html(footer.get('report_id'))}</code>")
    completed_at = footer.get("completed_at")
    if completed_at is not None:
        meta_parts.append(
            f"completed {_html(completed_at.isoformat() if hasattr(completed_at, 'isoformat') else completed_at)}"
        )
    if meta_parts:
        parts.append(f"<p class='muted'>{' &middot; '.join(meta_parts)}</p>")
    return "\n".join(parts)


def _render_system_health(content: Mapping[str, Any]) -> str:
    parts: list[str] = []
    stale = content.get("stale_sources") or []
    if stale:
        parts.append("<h3>Stale sources</h3>")
        parts.append("<ul>")
        for s in stale:
            parts.append(
                f"<li><code>{_html(s.get('source_id'))}</code> — "
                f"{_html(s.get('days_stale'))}d stale</li>"
            )
        parts.append("</ul>")
    errored = content.get("errored_subsystems") or []
    if errored:
        parts.append("<h3>Errored subsystems</h3>")
        parts.append("<ul>")
        for e in errored:
            parts.append(
                f"<li><code>{_html(e.get('subsystem'))}</code> "
                f"({_html(e.get('error_count'))} error(s) "
                f"in cycle <code>{_html(e.get('cycle_id'))}</code>)</li>"
            )
        parts.append("</ul>")
    breakdown = content.get("suppressed_breakdown") or {}
    if breakdown:
        parts.append("<h3>Suppressed comparisons this cycle</h3>")
        parts.append("<table>")
        parts.append("<thead><tr><th>Reason</th><th>Count</th></tr></thead>")
        parts.append("<tbody>")
        for reason, count in breakdown.items():
            parts.append(f"<tr><td><code>{_html(reason)}</code></td><td>{_html(count)}</td></tr>")
        parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_recent_tuning(content: Mapping[str, Any]) -> str:
    entries = content.get("entries") or []
    if not entries:
        return ""
    parts: list[str] = [
        "<p class='muted'>Threshold changes recorded since the prior report. "
        "Listed newest first; see "
        "<code>razor-rooster report tuning-log</code> for details.</p>",
        "<table>",
        "<thead><tr>"
        "<th>Applied at</th><th>Kind</th><th>Knob</th>"
        "<th>Previous → New</th><th>Note</th>"
        "</tr></thead>",
        "<tbody>",
    ]
    for entry in entries:
        applied_at = entry.get("applied_at")
        applied_str = (
            applied_at.isoformat() if hasattr(applied_at, "isoformat") else str(applied_at)
        )
        prev_v = entry.get("previous_value")
        new_v = entry.get("new_value")
        target = entry.get("target_percentile")
        prev_cell = (
            f"<code>{_html(format(prev_v, 'g'))}</code>"
            if isinstance(prev_v, int | float)
            else "(unset)"
        )
        new_cell = (
            f"<code>{_html(format(new_v, 'g'))}</code>" if isinstance(new_v, int | float) else "?"
        )
        target_str = f" (p{round(float(target) * 100)})" if isinstance(target, int | float) else ""
        parts.append(
            f"<tr>"
            f"<td><code>{_html(applied_str)}</code></td>"
            f"<td>{_html(entry.get('measurement_kind', '?'))}</td>"
            f"<td><code>{_html(entry.get('knob', '?'))}</code></td>"
            f"<td>{prev_cell} → {new_cell}{_html(target_str)}</td>"
            f"<td>{_html(entry.get('note') or '')}</td>"
            f"</tr>"
        )
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_at_a_glance(content: Mapping[str, Any]) -> str:
    facts = content.get("facts") or []
    if not facts:
        return ""
    parts: list[str] = ["<dl>"]
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        parts.append(f"<dt>{_html(fact.get('label', '?'))}</dt>")
        parts.append(f"<dd>{_html(fact.get('value', '?'))}</dd>")
    parts.append("</dl>")
    return "\n".join(parts)


def _render_surfaced(content: Mapping[str, Any]) -> str:
    comparisons = content.get("comparisons") or []
    if not comparisons:
        return ""
    parts: list[str] = []
    for cmp_ in comparisons:
        parts.append(_render_one_comparison(cmp_))
    return "\n".join(parts)


def _render_one_comparison(cmp_: Mapping[str, Any]) -> str:
    parts: list[str] = []
    title = _html(cmp_.get("class_title", "?"))
    sector = _html(cmp_.get("domain_sector", "?"))
    parts.append(f"<h3>{title} <span class='muted'>({sector})</span></h3>")
    parts.append("<ul>")
    parts.append(f"<li>comparison id: <code>{_html(cmp_.get('comparison_id', '?'))}</code></li>")
    venue = cmp_.get("venue", "polymarket")
    condition_id = cmp_.get("condition_id")
    if condition_id is not None:
        parts.append(f"<li>market: <code>{_html(condition_id)}</code> ({_html(venue)})</li>")
    model_p = cmp_.get("model_p")
    market_p = cmp_.get("market_p")
    ci = cmp_.get("model_ci") or (None, None)
    if model_p is not None:
        ci_str = (
            f" (CI {ci[0]:.2f}–{ci[1]:.2f})"  # noqa: RUF001
            if ci[0] is not None and ci[1] is not None
            else ""
        )
        parts.append(f"<li>model probability: <code>{model_p:.3f}</code>{_html(ci_str)}</li>")
    if market_p is not None:
        parts.append(f"<li>market probability: <code>{market_p:.3f}</code></li>")
    delta = cmp_.get("delta")
    if delta is not None:
        parts.append(f"<li>delta: <code>{delta:+.3f}</code></li>")
    parts.append("</ul>")
    warnings = cmp_.get("warnings") or []
    if warnings:
        parts.append("<div class='warn-list'>")
        parts.append("<strong>Warnings:</strong> ")
        parts.append(", ".join(f"<code>{_html(w)}</code>" for w in warnings))
        parts.append("</div>")
    case_for_model = cmp_.get("case_for_model") or []
    case_for_market = cmp_.get("case_for_market") or []
    if case_for_model or case_for_market:
        parts.append("<div class='balanced-cases'>")
        parts.append("<div>")
        parts.append("<h4>Possible reasons the model may be right</h4>")
        parts.append("<ul class='case-list'>")
        if case_for_model:
            for item in case_for_model:
                parts.append(f"<li>{_html(item)}</li>")
        else:
            parts.append("<li class='muted'>(no specific items identified)</li>")
        parts.append("</ul>")
        parts.append("</div>")
        parts.append("<div>")
        parts.append("<h4>Possible reasons the market may be right</h4>")
        parts.append("<ul class='case-list'>")
        if case_for_market:
            for item in case_for_market:
                parts.append(f"<li>{_html(item)}</li>")
        else:
            parts.append("<li class='muted'>(no specific items identified)</li>")
        parts.append("</ul>")
        parts.append("</div>")
        parts.append("</div>")
    analysis = cmp_.get("analysis")
    if analysis is not None:
        rendered_text = analysis.get("rendered_text")
        if rendered_text:
            parts.append(f"<pre>{_html(rendered_text)}</pre>")
    return "\n".join(parts)


def _render_cross_venue(content: Mapping[str, Any]) -> str:
    items = content.get("items") or []
    if not items:
        return ""
    threshold_bps = int(content.get("spread_threshold_bps") or 0)
    parts: list[str] = []
    if threshold_bps:
        parts.append(
            f"<p class='muted'>Showing classes whose venue prices disagree "
            f"by at least {threshold_bps} bps "
            f"({threshold_bps / 100:.1f} percentage points).</p>"
        )
    parts.append("<table>")
    parts.append(
        "<thead><tr>"
        "<th>Class</th><th>Sector</th><th>Spread (bps)</th>"
        "<th>Consensus</th><th>Venue prices</th>"
        "</tr></thead>"
    )
    parts.append("<tbody>")
    for item in items:
        spread_bps = int(item.get("spread_bps") or 0)
        consensus_p = item.get("consensus_market_p")
        consensus_cell = f"<code>{consensus_p:.3f}</code>" if consensus_p is not None else "—"
        venue_prices = item.get("venue_prices") or []
        venue_cell_parts: list[str] = []
        for vp in venue_prices:
            venue = vp.get("venue", "?")
            market_p = vp.get("market_probability")
            mp_str = f"{market_p:.3f}" if market_p is not None else "?"
            venue_cell_parts.append(f"<code>{_html(venue)}</code> {mp_str}")
        venue_cell = "<br>".join(venue_cell_parts)
        parts.append(
            f"<tr>"
            f"<td>{_html(item.get('class_title', '?'))}</td>"
            f"<td>{_html(item.get('domain_sector', '?'))}</td>"
            f"<td>{spread_bps}</td>"
            f"<td>{consensus_cell}</td>"
            f"<td>{venue_cell}</td>"
            f"</tr>"
        )
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_watched(content: Mapping[str, Any]) -> str:
    follow_ups = content.get("follow_ups") or []
    if not follow_ups:
        return ""
    parts: list[str] = []
    for fu in follow_ups:
        title = _html(fu.get("class_title", "?"))
        sector = _html(fu.get("domain_sector", "?"))
        parts.append(f"<h3>{title} <span class='muted'>({sector})</span></h3>")
        parts.append("<ul>")
        parts.append(f"<li>follow_up_id: <code>{_html(fu.get('follow_up_id', '?'))}</code></li>")
        parts.append(f"<li>analysis_id: <code>{_html(fu.get('analysis_id', '?'))}</code></li>")
        venue = fu.get("venue", "polymarket")
        condition_id = fu.get("condition_id")
        if condition_id is not None:
            parts.append(f"<li>market: <code>{_html(condition_id)}</code> ({_html(venue)})</li>")
        primary = fu.get("primary_alert_tier")
        parts.append(f"<li>primary alert: <strong>{_html(primary or '(none)')}</strong></li>")
        parts.append("</ul>")
        text = fu.get("reasoning_text")
        if text:
            parts.append(f"<pre>{_html(text)}</pre>")
    return "\n".join(parts)


def _render_calibration(content: Mapping[str, Any]) -> str:
    resolutions = content.get("resolutions") or []
    sector_brier_scores = content.get("sector_brier_scores") or []
    if not resolutions and not sector_brier_scores:
        return ""
    parts: list[str] = []
    if resolutions:
        parts.append("<table>")
        parts.append(
            "<thead><tr>"
            "<th>Class</th><th>Venue</th><th>Outcome</th>"
            "<th>Predicted p</th><th>Days to Resolution</th><th>Verdict</th>"
            "</tr></thead>"
        )
        parts.append("<tbody>")
        for res in resolutions:
            parts.append(
                f"<tr>"
                f"<td>{_html(res.get('class_title', '?'))}</td>"
                f"<td>{_html(res.get('venue', 'polymarket'))}</td>"
                f"<td>{_html(str(res.get('resolution_outcome', '?')).upper())}</td>"
                f"<td>{float(res.get('model_probability', 0.0)):.2f}</td>"
                f"<td>{_html(res.get('days_to_resolution') or '?')}</td>"
                f"<td>{_html(res.get('verdict_text', '?'))}</td>"
                f"</tr>"
            )
        parts.append("</tbody></table>")
    if sector_brier_scores:
        parts.append("<h3>Per-sector Brier scores (rolling window)</h3>")
        parts.append("<table>")
        parts.append(
            "<thead><tr>"
            "<th>Sector</th><th>Brier</th><th>n</th><th>Window</th><th>Status</th>"
            "</tr></thead>"
        )
        parts.append("<tbody>")
        for entry in sector_brier_scores:
            miscal = bool(entry.get("miscalibrated", False))
            status = "<strong>miscalibrated</strong> (weight outputs less)" if miscal else "ok"
            parts.append(
                f"<tr>"
                f"<td>{_html(entry.get('sector', '?'))}</td>"
                f"<td><code>{float(entry.get('brier_score', 0.0)):.4f}</code></td>"
                f"<td>{int(entry.get('n_resolutions', 0))}</td>"
                f"<td>{int(entry.get('window_days', 0))}d</td>"
                f"<td>{status}</td>"
                f"</tr>"
            )
        parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_reliability(content: Mapping[str, Any]) -> str:
    sectors = content.get("sectors") or []
    if not sectors:
        return ""
    min_per_bin = int(content.get("min_resolutions_per_bin", 5))
    parts: list[str] = [
        "<p class='muted'>Per-sector calibration bins. Mean predicted "
        "probability vs. empirical hit rate per bin. Sparse bins have "
        f"fewer than {min_per_bin} resolutions and should be treated "
        "as noisy.</p>",
        "<p class='muted'>A positive gap means the model was "
        "under-confident in that bin; negative means over-confident.</p>",
    ]
    for sector_entry in sectors:
        sector_name = _html(sector_entry.get("sector", "?"))
        n_total = int(sector_entry.get("n_resolutions", 0))
        sector_window = int(sector_entry.get("window_days", 0))
        parts.append(
            f"<h3>{sector_name} <span class='muted'>"
            f"(n={n_total}, window={sector_window}d)</span></h3>"
        )
        parts.append("<table>")
        parts.append(
            "<thead><tr>"
            "<th>Bin</th><th>n</th><th>Mean predicted</th>"
            "<th>Empirical</th><th>Gap</th><th>Notes</th>"
            "</tr></thead>"
        )
        parts.append("<tbody>")
        bin_entries = sector_entry.get("bins") or []
        for bin_index, bin_entry in enumerate(bin_entries):
            bin_lo = float(bin_entry.get("bin_lo", 0.0))
            bin_hi = float(bin_entry.get("bin_hi", 0.0))
            n = int(bin_entry.get("n", 0))
            is_top = bin_index == len(bin_entries) - 1
            label = f"[{bin_lo:.2f}, {bin_hi:.2f}{']' if is_top else ')'}"
            if n == 0:
                parts.append(
                    f"<tr><td><code>{_html(label)}</code></td>"
                    f"<td>0</td><td>—</td><td>—</td><td>—</td>"
                    f"<td class='muted'>empty</td></tr>"
                )
                continue
            mean_p = float(bin_entry.get("mean_predicted") or 0.0)
            empirical = float(bin_entry.get("empirical_rate") or 0.0)
            gap = float(bin_entry.get("calibration_gap") or 0.0)
            sparse = bool(bin_entry.get("sparse", False))
            note = "sparse" if sparse else ""
            parts.append(
                f"<tr><td><code>{_html(label)}</code></td>"
                f"<td>{n}</td>"
                f"<td><code>{mean_p:.4f}</code></td>"
                f"<td><code>{empirical:.4f}</code></td>"
                f"<td><code>{gap:+.4f}</code></td>"
                f"<td>{note}</td></tr>"
            )
        parts.append("</tbody></table>")
        chart = render_calibration_chart(bin_entries)
        if chart:
            parts.append(f"<pre>{_html(chart)}</pre>")
    return "\n".join(parts)


def _render_watchlist(content: Mapping[str, Any]) -> str:
    candidates = content.get("candidates") or []
    if not candidates:
        return ""
    parts: list[str] = []
    for cand in candidates:
        title = _html(cand.get("class_title", "?"))
        sector = _html(cand.get("domain_sector", "?"))
        parts.append(f"<h3>{title} <span class='muted'>({sector})</span></h3>")
        parts.append("<ul>")
        verbosity = cand.get("verbosity", "full")
        if verbosity == "full":
            parts.append(
                f"<li>posterior <code>{cand.get('posterior', 0):.3f}</code> "
                f"(base rate <code>{cand.get('base_rate', 0):.3f}</code>, "
                f"log-odds shift <code>{cand.get('log_odds_shift', 0):+.3f}</code>)</li>"
            )
            direction = cand.get("candidate_direction")
            if direction:
                parts.append(f"<li>direction: <code>{_html(direction)}</code></li>")
        parts.append(f"<li>reason: <code>{_html(cand.get('reason', '?'))}</code></li>")
        suggestion = cand.get("suggestion")
        if suggestion:
            parts.append(f"<li>suggestion: {_html(suggestion)}</li>")
        parts.append("</ul>")
    return "\n".join(parts)


def _html(value: object) -> str:
    """HTML-escape any value safely."""
    if value is None:
        return ""
    return _html_module.escape(str(value), quote=True)


__all__ = ["render"]
