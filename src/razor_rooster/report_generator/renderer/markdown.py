"""Markdown renderer (T-RG-032; design §3.6; OQ-RG-002 resolution).

GitHub-Flavored Markdown. ``##`` for sections, ``###`` for
per-comparison subsections, fenced code blocks for embedded traces,
GFM tables for the calibration log, horizontal rules between sections.
"""

from __future__ import annotations

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
    "system_health": "_No system-health warnings this cycle._",
    "recent_tuning": "_No threshold changes since the prior report._",
    "at_a_glance": "_No at-a-glance facts available this cycle._",
    "surfaced": "_No comparisons surfaced this cycle._",
    "cross_venue": "_No cross-venue disagreements this cycle._",
    "watched": "_No watched analyses required review this cycle._",
    "calibration": "_No new resolutions this cycle._",
    "reliability": "_No reliability bins populated this cycle._",
    "watchlist": "_No unmapped candidates this cycle._",
}


def render(
    *,
    header: Mapping[str, Any],
    body_sections: Sequence[SectionContent],
    footer: Mapping[str, Any],
) -> str:
    parts: list[str] = []
    parts.append(_render_header(header))
    for sec in body_sections:
        parts.append(_render_body_section(sec))
    parts.append(_render_footer(footer))
    return "\n\n".join(p for p in parts if p)


# -- header / footer --------------------------------------------------------


def _render_header(header: Mapping[str, Any]) -> str:
    lines: list[str] = []
    cycle_date = header.get("cycle_date", "?")
    lines.append(f"# Razor-Rooster Report — {cycle_date}")
    since_ts = header.get("since_ts")
    until_ts = header.get("until_ts")
    if since_ts is not None and until_ts is not None:
        lines.append(f"Cycle window: `{since_ts.isoformat()}` → `{until_ts.isoformat()}`")
    lines.append(f"Library version: `{header.get('library_version', '?')}`")
    if header.get("library_age_days") is not None:
        lines.append(f"Library refresh age: {header['library_age_days']} days")
    stale_count = int(header.get("stale_source_count") or 0)
    if stale_count:
        lines.append(f"Stale source count: **{stale_count}**")
    disabled = tuple(header.get("disabled_sections") or ())
    if disabled:
        lines.append(f"Disabled sections (config): {', '.join(disabled)}")
    lines.append(f"Report ID: `{header.get('report_id', '?')}`")
    return "\n\n".join(lines)


def _render_footer(footer: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append("---")
    lines.append("## Disclaimer")
    text = str(footer.get("disclaimer_text", "")).strip()
    if text:
        # Render as a blockquote so it stands out visually.
        for line in text.split("\n"):
            lines.append(f"> {line}" if line else ">")
    completed_at = footer.get("completed_at")
    completed_str = completed_at.isoformat() if completed_at is not None else "?"
    lines.append("")
    lines.append(
        f"Razor-Rooster `{footer.get('system_version', '?')}` — generated `{completed_str}`"
    )
    lines.append(f"Report ID: `{footer.get('report_id', '?')}`")
    return "\n".join(lines)


# -- body sections ----------------------------------------------------------


def _render_body_section(section: SectionContent) -> str:
    label = _HEADER_LABEL_BY_SECTION.get(section.name, section.name.title())
    lines: list[str] = ["---", f"## {label}"]
    if not section.ok:
        lines.append(f"_section error: {section.error}_")
        return "\n\n".join(lines)
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
        body = _EMPTY_MESSAGE_BY_SECTION.get(section.name, "_(empty)_")
    lines.append(body)
    return "\n\n".join(lines)


def _render_system_health(content: Mapping[str, Any]) -> str:
    blocks: list[str] = []
    stale = content.get("stale_sources") or []
    if stale:
        rows = ["**Stale sources:**", ""]
        for s in stale:
            days = s.get("days_stale")
            days_str = f"{days} days" if days is not None else "?"
            rows.append(f"- `{s.get('source_id')}` ({days_str})")
        blocks.append("\n".join(rows))
    library_age = content.get("library_age_days")
    if library_age is not None:
        blocks.append(f"Library refresh age: {library_age} days")
    errored = content.get("errored_subsystems") or []
    if errored:
        rows = ["**Errored subsystems since prior report:**", ""]
        for e in errored:
            rows.append(
                f"- `{e.get('subsystem')}` "
                f"({e.get('error_count')} error(s) in cycle "
                f"`{e.get('cycle_id')}`)"
            )
        blocks.append("\n".join(rows))
    breakdown = content.get("suppressed_breakdown") or {}
    if breakdown:
        rows = ["**Suppressed comparisons this cycle:**", ""]
        for reason, count in breakdown.items():
            rows.append(f"- `{reason}`: {count}")
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


def _render_recent_tuning(content: Mapping[str, Any]) -> str:
    entries = content.get("entries") or []
    if not entries:
        return ""
    blocks: list[str] = [
        "_Threshold changes recorded since the prior report. "
        "Listed newest first; see "
        "`razor-rooster report tuning-log` for details._",
        "",
        "| Applied at | Kind | Knob | Previous → New | Note |",
        "|------------|------|------|----------------|------|",
    ]
    for entry in entries:
        applied_at = entry.get("applied_at")
        applied_str = applied_at.isoformat() if applied_at is not None else "?"
        kind = str(entry.get("measurement_kind", "?")).replace("|", "\\|")
        knob = str(entry.get("knob", "?")).replace("|", "\\|")
        prev_v = entry.get("previous_value")
        new_v = entry.get("new_value")
        target = entry.get("target_percentile")
        prev_str = f"`{prev_v:g}`" if isinstance(prev_v, int | float) else "(unset)"
        new_str = f"`{new_v:g}`" if isinstance(new_v, int | float) else "?"
        target_str = f" (p{round(float(target) * 100)})" if isinstance(target, int | float) else ""
        note = entry.get("note") or ""
        note_cell = str(note).replace("|", "\\|") if note else ""
        blocks.append(
            f"| `{applied_str}` | {kind} | {knob} | "
            f"{prev_str} → {new_str}{target_str} | {note_cell} |"
        )
    return "\n".join(blocks)


def _render_at_a_glance(content: Mapping[str, Any]) -> str:
    facts = content.get("facts") or []
    if not facts:
        return ""
    lines: list[str] = []
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        label = str(fact.get("label", "?")).replace("|", "\\|")
        value = str(fact.get("value", "?")).replace("|", "\\|")
        lines.append(f"- **{label}**: {value}")
    return "\n".join(lines)


def _render_surfaced(content: Mapping[str, Any]) -> str:
    comparisons = content.get("comparisons") or []
    if not comparisons:
        return ""
    blocks: list[str] = []
    for cmp_ in comparisons:
        blocks.append(_render_one_comparison_md(cmp_))
    return "\n\n".join(blocks)


def _render_one_comparison_md(cmp_: Mapping[str, Any]) -> str:
    lines: list[str] = []
    title = cmp_.get("class_title", "?")
    sector = cmp_.get("domain_sector", "?")
    lines.append(f"### {title} ({sector})")
    lines.append(f"- comparison_id: `{cmp_.get('comparison_id', '?')}`")
    venue = cmp_.get("venue", "polymarket")
    condition_id = cmp_.get("condition_id")
    if condition_id is not None:
        lines.append(f"- market: `{condition_id}` ({venue})")
    model_p = cmp_.get("model_p")
    market_p = cmp_.get("market_p")
    ci = cmp_.get("model_ci") or (None, None)
    if model_p is not None:
        ci_str = (
            f" (CI {ci[0]:.2f}–{ci[1]:.2f})"  # noqa: RUF001
            if ci[0] is not None and ci[1] is not None
            else ""
        )
        lines.append(f"- model probability: **{model_p:.3f}**{ci_str}")
    if market_p is not None:
        spread = cmp_.get("market_spread_bps")
        spread_str = f" (spread {spread} bps)" if spread is not None else ""
        lines.append(f"- market probability: **{market_p:.3f}**{spread_str}")
    delta = cmp_.get("delta")
    if delta is not None:
        lines.append(f"- delta: `{delta:+.3f}`")
    ev = cmp_.get("ev")
    if ev is not None:
        lines.append(f"- expected value per dollar: `{ev:+.4f}`")
    warnings = cmp_.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("**Warnings:**")
        for w in warnings:
            lines.append(f"- `{w}`")
    case_for_model = cmp_.get("case_for_model") or []
    case_for_market = cmp_.get("case_for_market") or []
    if case_for_model or case_for_market:
        lines.append("")
        lines.append(_balanced_cases_md(case_for_model, case_for_market))
    ambiguity = cmp_.get("ambiguity_factors") or []
    if ambiguity:
        lines.append("")
        lines.append("**Ambiguity factors:**")
        for a in ambiguity:
            lines.append(f"- {a}")
    analysis = cmp_.get("analysis")
    if analysis:
        lines.append("")
        lines.append("**Analysis:**")
        rendered = analysis.get("rendered_text")
        if rendered:
            lines.append("```")
            lines.append(str(rendered).rstrip())
            lines.append("```")
        else:
            lines.append(_render_analysis_summary_md(analysis))
    return "\n".join(lines)


def _balanced_cases_md(
    case_for_model: Sequence[str],
    case_for_market: Sequence[str],
) -> str:
    target_len = max(len(case_for_model), len(case_for_market), 1)
    model_padded = list(case_for_model) + ["(no specific items identified)"] * (
        target_len - len(case_for_model)
    )
    market_padded = list(case_for_market) + ["(no specific items identified)"] * (
        target_len - len(case_for_market)
    )
    lines: list[str] = []
    lines.append("**Possible reasons the model may be right:**")
    for b in model_padded:
        lines.append(f"- {b}")
    lines.append("")
    lines.append("**Possible reasons the market may be right:**")
    for b in market_padded:
        lines.append(f"- {b}")
    return "\n".join(lines)


def _render_analysis_summary_md(analysis: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(
        f"- Suggested fraction: `{analysis.get('suggested_fraction', 0.0):.4f}` (half-Kelly)"
    )
    lines.append(f"- Suggested dollar size: `${analysis.get('suggested_dollar_size', 0.0):.2f}`")
    if analysis.get("ev_per_dollar") is not None:
        lines.append(f"- EV per dollar: `{analysis['ev_per_dollar']:+.4f}`")
    if analysis.get("days_to_resolution") is not None:
        lines.append(f"- Days to resolution: `{analysis['days_to_resolution']}`")
    if analysis.get("kelly_clamped_by_max_cap"):
        lines.append("- _(clamped by max-position cap)_")
    if analysis.get("kelly_clamped_by_liquidity"):
        lines.append("- _(clamped by market liquidity)_")
    if analysis.get("sub_threshold"):
        lines.append("- _(below min-edge threshold; sizing not surfaced)_")
    return "\n".join(lines)


def _render_cross_venue(content: Mapping[str, Any]) -> str:
    items = content.get("items") or []
    if not items:
        return ""
    threshold_bps = int(content.get("spread_threshold_bps") or 0)
    blocks: list[str] = []
    if threshold_bps:
        blocks.append(
            f"_Showing classes whose venue prices disagree by at least "
            f"{threshold_bps} bps ({threshold_bps / 100:.1f} percentage points)._"
        )
    blocks.append(
        "| Class | Sector | Spread (bps) | Consensus | Venue prices |\n"
        "|-------|--------|--------------|-----------|--------------|"
    )
    for item in items:
        cls = str(item.get("class_title", "?")).replace("|", "\\|")
        sector = str(item.get("domain_sector", "?")).replace("|", "\\|")
        spread_bps = int(item.get("spread_bps") or 0)
        consensus_p = item.get("consensus_market_p")
        consensus_cell = f"`{consensus_p:.3f}`" if consensus_p is not None else "—"
        venue_prices = item.get("venue_prices") or []
        venue_cell_parts: list[str] = []
        for vp in venue_prices:
            venue = str(vp.get("venue", "?"))
            market_p = vp.get("market_probability")
            mp_str = f"{market_p:.3f}" if market_p is not None else "?"
            venue_cell_parts.append(f"`{venue}` {mp_str}")
        venue_cell = "<br>".join(venue_cell_parts)
        blocks.append(f"| {cls} | {sector} | {spread_bps} | {consensus_cell} | {venue_cell} |")
    # Per-class detail blocks below the table.
    for item in items:
        blocks.append(_render_one_cross_venue_item_md(item))
    return "\n".join(blocks)


def _render_one_cross_venue_item_md(item: Mapping[str, Any]) -> str:
    lines: list[str] = []
    title = item.get("class_title", "?")
    sector = item.get("domain_sector", "?")
    lines.append(f"\n### {title} ({sector})")
    spread_bps = int(item.get("spread_bps") or 0)
    lines.append(f"- spread: **{spread_bps} bps** ({spread_bps / 100:.2f} percentage points)")
    consensus_p = item.get("consensus_market_p")
    total_vol = item.get("total_volume_24h")
    if consensus_p is not None:
        if total_vol is not None and total_vol > 0:
            lines.append(
                f"- liquidity-weighted consensus: **{consensus_p:.3f}** "
                f"(across `${total_vol:,.0f}` of 24h volume)"
            )
        else:
            lines.append(
                f"- unweighted consensus: **{consensus_p:.3f}** _(no per-venue volume available)_"
            )
    venue_prices = item.get("venue_prices") or []
    for vp in venue_prices:
        venue = vp.get("venue", "?")
        condition_id = vp.get("condition_id", "?")
        market_p = vp.get("market_probability")
        market_p_str = f"`{market_p:.3f}`" if market_p is not None else "`?`"
        spread = vp.get("market_spread_bps")
        spread_str = f" (spread {spread} bps)" if spread is not None else ""
        volume = vp.get("market_volume_24h")
        vol_str = f" — vol_24h `${volume:,.0f}`" if volume is not None else ""
        lines.append(f"- `{venue}` market_p {market_p_str}: `{condition_id}`{spread_str}{vol_str}")
    if venue_prices:
        first = venue_prices[0]
        model_p = first.get("model_probability")
        if model_p is not None:
            ci = first.get("model_ci") or (None, None)
            ci_str = (
                f" (CI {ci[0]:.2f}-{ci[1]:.2f})" if ci[0] is not None and ci[1] is not None else ""
            )
            lines.append(f"- model_p: **{model_p:.3f}**{ci_str}")
    lines.append(
        "\n_Both venue prices are shown so the operator can weigh how the "
        "two markets are pricing this question. The disagreement is "
        "informative regardless of where the model sits._"
    )
    return "\n".join(lines)


def _render_watched(content: Mapping[str, Any]) -> str:
    follow_ups = content.get("follow_ups") or []
    if not follow_ups:
        return ""
    blocks: list[str] = []
    for fu in follow_ups:
        blocks.append(_render_one_follow_up_md(fu))
    return "\n\n".join(blocks)


def _render_one_follow_up_md(fu: Mapping[str, Any]) -> str:
    lines: list[str] = []
    title = fu.get("class_title", "?")
    lines.append(f"### {title}")
    lines.append(f"- follow_up_id: `{fu.get('follow_up_id', '?')}`")
    lines.append(f"- analysis_id: `{fu.get('analysis_id', '?')}`")
    venue = fu.get("venue", "polymarket")
    condition_id = fu.get("condition_id")
    if condition_id is not None:
        lines.append(f"- market: `{condition_id}` ({venue})")
    lines.append(f"- primary alert: **{fu.get('primary_alert_tier') or '(none)'}**")
    if fu.get("alert_tiers"):
        tiers = ", ".join(f"`{t}`" for t in fu.get("alert_tiers", []))
        lines.append(f"- all alert tiers: {tiers}")
    analysis_model_p = fu.get("analysis_model_p")
    current_model_p = fu.get("current_model_p")
    if analysis_model_p is not None and current_model_p is not None:
        lines.append(f"- model probability: `{analysis_model_p:.3f}` → `{current_model_p:.3f}`")
    analysis_market_p = fu.get("analysis_market_p")
    current_market_p = fu.get("current_market_p")
    if analysis_market_p is not None and current_market_p is not None:
        lines.append(f"- market probability: `{analysis_market_p:.3f}` → `{current_market_p:.3f}`")
    if fu.get("days_since_analysis") is not None:
        lines.append(f"- days since analysis: {fu['days_since_analysis']}")
    if fu.get("days_to_resolution") is not None:
        lines.append(f"- days to resolution: {fu['days_to_resolution']}")
    lines.append("")
    reasoning_text = str(fu.get("reasoning_text") or "")
    if reasoning_text:
        lines.append("```")
        lines.append(reasoning_text.rstrip())
        lines.append("```")
    return "\n".join(lines)


def _render_calibration(content: Mapping[str, Any]) -> str:
    resolutions = content.get("resolutions") or []
    sector_brier_scores = content.get("sector_brier_scores") or []
    if not resolutions and not sector_brier_scores:
        return ""
    blocks: list[str] = []
    if resolutions:
        lines: list[str] = [
            "| Class | Venue | Outcome | Predicted p | Days to Resolution | Verdict |",
            "|-------|-------|---------|-------------|---------------------|---------|",
        ]
        for res in resolutions:
            cls = str(res.get("class_title", "?")).replace("|", "\\|")
            venue = str(res.get("venue", "polymarket"))
            outcome = str(res.get("resolution_outcome", "?")).upper()
            model_p = float(res.get("model_probability", 0.0))
            days = res.get("days_to_resolution") or "?"
            verdict = str(res.get("verdict_text", "?")).replace("|", "\\|")
            lines.append(f"| {cls} | {venue} | {outcome} | {model_p:.2f} | {days} | {verdict} |")
        blocks.append("\n".join(lines))
    if sector_brier_scores:
        rows: list[str] = [
            "**Per-sector Brier scores (rolling window):**",
            "",
            "| Sector | Brier | n | Window | Status |",
            "|--------|-------|---|--------|--------|",
        ]
        for entry in sector_brier_scores:
            sector = str(entry.get("sector", "?")).replace("|", "\\|")
            n = int(entry.get("n_resolutions", 0))
            brier = float(entry.get("brier_score", 0.0))
            window_days = int(entry.get("window_days", 0))
            miscal = bool(entry.get("miscalibrated", False))
            status = "**miscalibrated** (weight outputs less)" if miscal else "ok"
            rows.append(f"| {sector} | `{brier:.4f}` | {n} | {window_days}d | {status} |")
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks)


def _render_reliability(content: Mapping[str, Any]) -> str:
    sectors = content.get("sectors") or []
    if not sectors:
        return ""
    min_per_bin = int(content.get("min_resolutions_per_bin", 5))
    blocks: list[str] = []
    blocks.append(
        "_Per-sector calibration bins. Mean predicted probability vs. "
        "empirical hit rate per bin. Sparse bins have fewer than "
        f"{min_per_bin} resolutions and should be treated as noisy._"
    )
    blocks.append(
        "_A positive gap means the model was under-confident in that bin; "
        "negative means over-confident._"
    )
    for sector_entry in sectors:
        sector = str(sector_entry.get("sector", "?")).replace("|", "\\|")
        n_total = int(sector_entry.get("n_resolutions", 0))
        sector_window = int(sector_entry.get("window_days", 0))
        rows: list[str] = [
            f"### {sector} (n={n_total}, window={sector_window}d)",
            "",
            "| Bin | n | Mean predicted | Empirical | Gap | Notes |",
            "|-----|---|----------------|-----------|-----|-------|",
        ]
        bin_entries = sector_entry.get("bins") or []
        for bin_index, bin_entry in enumerate(bin_entries):
            bin_lo = float(bin_entry.get("bin_lo", 0.0))
            bin_hi = float(bin_entry.get("bin_hi", 0.0))
            n = int(bin_entry.get("n", 0))
            is_top = bin_index == len(bin_entries) - 1
            label = f"`[{bin_lo:.2f}, {bin_hi:.2f}{']' if is_top else ')'}`"
            if n == 0:
                rows.append(f"| {label} | 0 | — | — | — | empty |")
                continue
            mean_p = float(bin_entry.get("mean_predicted") or 0.0)
            empirical = float(bin_entry.get("empirical_rate") or 0.0)
            gap = float(bin_entry.get("calibration_gap") or 0.0)
            sparse = bool(bin_entry.get("sparse", False))
            note = "sparse" if sparse else ""
            rows.append(
                f"| {label} | {n} | `{mean_p:.4f}` | `{empirical:.4f}` | `{gap:+.4f}` | {note} |"
            )
        sector_block = "\n".join(rows)
        chart = render_calibration_chart(bin_entries)
        if chart:
            sector_block = sector_block + "\n\n```\n" + chart + "\n```"
        blocks.append(sector_block)
    return "\n\n".join(blocks)


def _render_watchlist(content: Mapping[str, Any]) -> str:
    candidates = content.get("candidates") or []
    if not candidates:
        return ""
    blocks: list[str] = []
    for cand in candidates:
        lines: list[str] = []
        lines.append(f"### {cand.get('class_title', '?')} ({cand.get('domain_sector', '?')})")
        verbosity = cand.get("verbosity", "full")
        if verbosity == "full":
            lines.append(
                f"- posterior `{cand.get('posterior', 0):.3f}` "
                f"(base rate `{cand.get('base_rate', 0):.3f}`, "
                f"log-odds shift `{cand.get('log_odds_shift', 0):+.3f}`)"
            )
            direction = cand.get("candidate_direction")
            if direction:
                lines.append(f"- direction: `{direction}`")
        lines.append(f"- reason: `{cand.get('reason', '?')}`")
        suggestion = cand.get("suggestion")
        if suggestion:
            lines.append(f"- suggestion: {suggestion}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


__all__ = ["render"]
