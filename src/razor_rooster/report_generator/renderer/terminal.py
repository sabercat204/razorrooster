"""Terminal renderer (T-RG-031; design §3.6).

Plain-text output with ASCII section dividers. Width target 80
columns; no fixed truncation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from razor_rooster.report_generator.models import SectionContent
from razor_rooster.report_generator.renderer.calibration_chart import (
    render_chart as render_calibration_chart,
)
from razor_rooster.report_generator.renderer.shared import (
    disclaimer_block,
    equal_prominence_blocks,
    section_divider,
    thin_divider,
    warnings_block,
)

_HEADER_LABEL_BY_SECTION: dict[str, str] = {
    "system_health": "SYSTEM HEALTH",
    "recent_tuning": "RECENT THRESHOLD CHANGES",
    "at_a_glance": "AT A GLANCE",
    "surfaced": "SURFACED COMPARISONS",
    "cross_venue": "CROSS-VENUE DISAGREEMENTS",
    "watched": "ACTIVE WATCHED SITUATIONS",
    "calibration": "CALIBRATION LOG",
    "reliability": "RELIABILITY DIAGRAM",
    "watchlist": "WATCHLIST (DEVELOPING)",
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
    "watchlist": "No unmapped candidates this cycle.",
}


def render(
    *,
    header: Mapping[str, Any],
    body_sections: Sequence[SectionContent],
    footer: Mapping[str, Any],
) -> str:
    """Render the full report as plain text."""
    parts: list[str] = []
    parts.append(_render_header(header))
    for sec in body_sections:
        parts.append(_render_body_section(sec))
    parts.append(_render_footer(footer))
    return "\n".join(parts)


# -- header / footer --------------------------------------------------------


def _render_header(header: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(section_divider())
    lines.append("RAZOR-ROOSTER REPORT")
    cycle_date = header.get("cycle_date", "?")
    since_ts = header.get("since_ts")
    until_ts = header.get("until_ts")
    if since_ts is not None and until_ts is not None:
        lines.append(
            f"Cycle {cycle_date} (since {since_ts.isoformat()}, until {until_ts.isoformat()})"
        )
    else:
        lines.append(f"Cycle {cycle_date}")
    lines.append(f"Library version {header.get('library_version', '?')}")
    if header.get("library_age_days") is not None:
        lines.append(f"Library refresh age: {header['library_age_days']} days")
    stale_count = int(header.get("stale_source_count") or 0)
    if stale_count:
        lines.append(f"Stale source count: {stale_count}")
    disabled = tuple(header.get("disabled_sections") or ())
    if disabled:
        lines.append(f"Disabled sections (config): {', '.join(disabled)}")
    lines.append(f"Report ID: {header.get('report_id', '?')}")
    lines.append(section_divider())
    lines.append("")
    return "\n".join(lines)


def _render_footer(footer: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(section_divider())
    lines.append("")
    lines.append(disclaimer_block(str(footer.get("disclaimer_text", ""))))
    lines.append("")
    completed_at = footer.get("completed_at")
    completed_str = completed_at.isoformat() if completed_at is not None else "?"
    lines.append(f"Razor-Rooster {footer.get('system_version', '?')} — generated {completed_str}")
    lines.append(f"Report ID: {footer.get('report_id', '?')}")
    lines.append(section_divider())
    return "\n".join(lines)


# -- body sections ----------------------------------------------------------


def _render_body_section(section: SectionContent) -> str:
    label = _HEADER_LABEL_BY_SECTION.get(section.name, section.name.upper())
    lines: list[str] = [label, thin_divider()]
    if not section.ok:
        lines.append(f"section error: {section.error}")
        lines.append("")
        lines.append(section_divider())
        return "\n".join(lines) + "\n"
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
        body = _EMPTY_MESSAGE_BY_SECTION.get(section.name, "(no content)")
    lines.append(body)
    lines.append("")
    lines.append(section_divider())
    return "\n".join(lines) + "\n"


def _render_system_health(content: Mapping[str, Any]) -> str:
    lines: list[str] = []
    stale = content.get("stale_sources") or []
    if stale:
        lines.append("Stale sources:")
        for s in stale:
            days = s.get("days_stale")
            days_str = f"{days} days" if days is not None else "?"
            lines.append(f"  - {s.get('source_id')} ({days_str})")
    library_age = content.get("library_age_days")
    if library_age is not None:
        lines.append(f"Library refresh age: {library_age} days")
    errored = content.get("errored_subsystems") or []
    if errored:
        lines.append("Errored subsystems since prior report:")
        for e in errored:
            lines.append(
                f"  - {e.get('subsystem')} ({e.get('error_count')} error(s) "
                f"in cycle {e.get('cycle_id')})"
            )
    breakdown = content.get("suppressed_breakdown") or {}
    if breakdown:
        lines.append("Suppressed comparisons this cycle:")
        for reason, count in breakdown.items():
            lines.append(f"  {reason}: {count}")
    return "\n".join(lines)


def _render_recent_tuning(content: Mapping[str, Any]) -> str:
    entries = content.get("entries") or []
    if not entries:
        return ""
    lines: list[str] = [
        "Threshold changes recorded since the prior report. "
        "Listed newest first; see `razor-rooster report tuning-log` for details."
    ]
    for entry in entries:
        applied_at = entry.get("applied_at")
        applied_str = applied_at.isoformat() if applied_at is not None else "?"
        kind = entry.get("measurement_kind", "?")
        knob = entry.get("knob", "?")
        prev_v = entry.get("previous_value")
        new_v = entry.get("new_value")
        target = entry.get("target_percentile")
        prev_str = f"{prev_v:g}" if isinstance(prev_v, int | float) else "(unset)"
        new_str = f"{new_v:g}" if isinstance(new_v, int | float) else "?"
        target_str = f" at p{round(float(target) * 100)}" if isinstance(target, int | float) else ""
        lines.append(f"  {applied_str}  kind={kind}")
        lines.append(f"    {knob}: {prev_str} → {new_str}{target_str}")
        note = entry.get("note")
        if note:
            lines.append(f"    note: {note}")
    return "\n".join(lines)


def _render_at_a_glance(content: Mapping[str, Any]) -> str:
    facts = content.get("facts") or []
    if not facts:
        return ""
    lines: list[str] = []
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        label = str(fact.get("label", "?"))
        value = str(fact.get("value", "?"))
        lines.append(f"  {label}: {value}")
    return "\n".join(lines)


def _render_surfaced(content: Mapping[str, Any]) -> str:
    comparisons = content.get("comparisons") or []
    if not comparisons:
        return ""
    blocks: list[str] = []
    for cmp_ in comparisons:
        blocks.append(_render_one_comparison(cmp_))
    return "\n\n".join(blocks)


def _render_one_comparison(cmp_: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"{cmp_.get('class_title', '?')} ({cmp_.get('domain_sector', '?')})")
    lines.append(f"  comparison_id: {cmp_.get('comparison_id', '?')}")
    venue = cmp_.get("venue", "polymarket")
    condition_id = cmp_.get("condition_id")
    if condition_id is not None:
        lines.append(f"  market: {condition_id} ({venue})")
    model_p = cmp_.get("model_p")
    market_p = cmp_.get("market_p")
    ci = cmp_.get("model_ci") or (None, None)
    if model_p is not None:
        ci_str = (
            f" (CI {ci[0]:.2f}–{ci[1]:.2f})"  # noqa: RUF001
            if ci[0] is not None and ci[1] is not None
            else ""
        )
        lines.append(f"  model probability: {model_p:.3f}{ci_str}")
    if market_p is not None:
        spread = cmp_.get("market_spread_bps")
        spread_str = f" (spread {spread} bps)" if spread is not None else ""
        lines.append(f"  market probability: {market_p:.3f}{spread_str}")
    delta = cmp_.get("delta")
    if delta is not None:
        lines.append(f"  delta: {delta:+.3f}")
    ev = cmp_.get("ev")
    if ev is not None:
        lines.append(f"  expected value per dollar: {ev:+.4f}")
    warnings = cmp_.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append(warnings_block(warnings))
    case_for_model = cmp_.get("case_for_model") or []
    case_for_market = cmp_.get("case_for_market") or []
    if case_for_model or case_for_market:
        lines.append("")
        lines.append(
            equal_prominence_blocks(
                model_label="Possible reasons the model may be right:",
                model_bullets=case_for_model,
                market_label="Possible reasons the market may be right:",
                market_bullets=case_for_market,
            )
        )
    ambiguity = cmp_.get("ambiguity_factors") or []
    if ambiguity:
        lines.append("")
        lines.append("Ambiguity factors:")
        for a in ambiguity:
            lines.append(f"  - {a}")
    analysis = cmp_.get("analysis")
    if analysis:
        lines.append("")
        lines.append("Analysis:")
        rendered = analysis.get("rendered_text")
        if rendered:
            for ln in str(rendered).split("\n"):
                lines.append(f"  {ln}" if ln else "")
        else:
            lines.append(_render_analysis_summary(analysis))
    return "\n".join(lines)


def _render_analysis_summary(analysis: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(
        f"  Suggested fraction: {analysis.get('suggested_fraction', 0.0):.4f} (half-Kelly)"
    )
    lines.append(f"  Suggested dollar size: ${analysis.get('suggested_dollar_size', 0.0):.2f}")
    if analysis.get("ev_per_dollar") is not None:
        lines.append(f"  EV per dollar: {analysis['ev_per_dollar']:+.4f}")
    if analysis.get("days_to_resolution") is not None:
        lines.append(f"  Days to resolution: {analysis['days_to_resolution']}")
    if analysis.get("kelly_clamped_by_max_cap"):
        lines.append("  (clamped by max-position cap)")
    if analysis.get("kelly_clamped_by_liquidity"):
        lines.append("  (clamped by market liquidity)")
    if analysis.get("sub_threshold"):
        lines.append("  (below min-edge threshold; sizing not surfaced)")
    return "\n".join(lines)


def _render_cross_venue(content: Mapping[str, Any]) -> str:
    items = content.get("items") or []
    if not items:
        return ""
    threshold_bps = int(content.get("spread_threshold_bps") or 0)
    blocks: list[str] = []
    if threshold_bps:
        blocks.append(
            f"(showing classes whose venue prices disagree by at least "
            f"{threshold_bps} bps / {threshold_bps / 100:.1f} percentage points)"
        )
    for item in items:
        blocks.append(_render_one_cross_venue_item(item))
    return "\n\n".join(blocks)


def _render_one_cross_venue_item(item: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"{item.get('class_title', '?')} ({item.get('domain_sector', '?')})")
    spread_bps = int(item.get("spread_bps") or 0)
    lines.append(f"  spread: {spread_bps} bps ({spread_bps / 100:.2f} percentage points)")
    consensus_p = item.get("consensus_market_p")
    total_vol = item.get("total_volume_24h")
    if consensus_p is not None:
        if total_vol is not None and total_vol > 0:
            lines.append(
                f"  liquidity-weighted consensus: {consensus_p:.3f}  "
                f"(across ${total_vol:,.0f} of 24h volume)"
            )
        else:
            lines.append(
                f"  unweighted consensus: {consensus_p:.3f}  (no per-venue volume available)"
            )
    venue_prices = item.get("venue_prices") or []
    for vp in venue_prices:
        venue = vp.get("venue", "?")
        condition_id = vp.get("condition_id", "?")
        market_p = vp.get("market_probability")
        market_p_str = f"{market_p:.3f}" if market_p is not None else "?"
        spread = vp.get("market_spread_bps")
        spread_str = f" (spread {spread} bps)" if spread is not None else ""
        volume = vp.get("market_volume_24h")
        vol_str = f"  vol_24h ${volume:,.0f}" if volume is not None else ""
        lines.append(
            f"  {venue:<10}  market_p {market_p_str}  market: {condition_id}{spread_str}{vol_str}"
        )
    # Model probability is the same across the venue rows (one class →
    # one model belief). Show it once.
    if venue_prices:
        first = venue_prices[0]
        model_p = first.get("model_probability")
        if model_p is not None:
            ci = first.get("model_ci") or (None, None)
            ci_str = (
                f" (CI {ci[0]:.2f}-{ci[1]:.2f})" if ci[0] is not None and ci[1] is not None else ""
            )
            lines.append(f"  model_p     {model_p:.3f}{ci_str}")
    lines.append(
        "  Both venue prices are shown so the operator can weigh how the "
        "two markets are pricing this question. The disagreement is "
        "informative regardless of where the model sits."
    )
    return "\n".join(lines)


def _render_watched(content: Mapping[str, Any]) -> str:
    follow_ups = content.get("follow_ups") or []
    if not follow_ups:
        return ""
    blocks: list[str] = []
    for fu in follow_ups:
        blocks.append(_render_one_follow_up(fu))
    return "\n\n".join(blocks)


def _render_one_follow_up(fu: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"{fu.get('class_title', '?')} (analysis {fu.get('analysis_id', '?')})")
    lines.append(f"  follow_up_id: {fu.get('follow_up_id', '?')}")
    venue = fu.get("venue", "polymarket")
    condition_id = fu.get("condition_id")
    if condition_id is not None:
        lines.append(f"  market: {condition_id} ({venue})")
    lines.append(f"  primary alert: {fu.get('primary_alert_tier') or '(none)'}")
    if fu.get("alert_tiers"):
        lines.append(f"  all alert tiers: {', '.join(fu.get('alert_tiers', []))}")
    analysis_model_p = fu.get("analysis_model_p")
    current_model_p = fu.get("current_model_p")
    if analysis_model_p is not None:
        if current_model_p is not None:
            lines.append(f"  model probability: {analysis_model_p:.3f} → {current_model_p:.3f}")
        else:
            lines.append(f"  model probability at analysis: {analysis_model_p:.3f}")
    analysis_market_p = fu.get("analysis_market_p")
    current_market_p = fu.get("current_market_p")
    if analysis_market_p is not None:
        if current_market_p is not None:
            lines.append(f"  market probability: {analysis_market_p:.3f} → {current_market_p:.3f}")
        else:
            lines.append(f"  market probability at analysis: {analysis_market_p:.3f}")
    if fu.get("days_since_analysis") is not None:
        lines.append(f"  days since analysis: {fu['days_since_analysis']}")
    if fu.get("days_to_resolution") is not None:
        lines.append(f"  days to resolution: {fu['days_to_resolution']}")
    lines.append("")
    reasoning_text = str(fu.get("reasoning_text") or "")
    for ln in reasoning_text.split("\n"):
        lines.append(f"  {ln}" if ln else "")
    return "\n".join(lines)


def _render_calibration(content: Mapping[str, Any]) -> str:
    resolutions = content.get("resolutions") or []
    sector_brier_scores = content.get("sector_brier_scores") or []
    # Render the section even when there are no new resolutions if we
    # have a Brier-score summary to show; that's the per-sector
    # calibration health view from supplement §3.
    if not resolutions and not sector_brier_scores:
        return ""
    lines: list[str] = []
    for res in resolutions:
        lines.append(f"- {res.get('class_title', '?')}")
        venue = res.get("venue", "polymarket")
        lines.append(f"    market: {res.get('condition_id', '?')} ({venue})")
        lines.append(f"    {res.get('verdict_text', '?')}")
        if res.get("days_to_resolution") is not None:
            lines.append(f"    days from comparison to resolution: {res['days_to_resolution']}")
        market_p = res.get("market_probability")
        if market_p is not None:
            lines.append(f"    market_p at comparison: {market_p:.3f}")
        lines.append("")
    if sector_brier_scores:
        # Trailing summary block. Same indentation pattern as the
        # per-resolution lines so the visual hierarchy is stable.
        if resolutions:
            lines.append("")
        lines.append("Per-sector Brier scores (rolling window):")
        for entry in sector_brier_scores:
            sector = entry.get("sector", "?")
            n = int(entry.get("n_resolutions", 0))
            brier = float(entry.get("brier_score", 0.0))
            window_days = int(entry.get("window_days", 0))
            miscal = bool(entry.get("miscalibrated", False))
            tag = " (miscalibrated; weight outputs less)" if miscal else ""
            lines.append(f"  {sector:<24} brier={brier:.4f}  n={n}  window={window_days}d{tag}")
    return "\n".join(lines).rstrip()


def _render_reliability(content: Mapping[str, Any]) -> str:
    sectors = content.get("sectors") or []
    if not sectors:
        return ""
    min_per_bin = int(content.get("min_resolutions_per_bin", 5))
    lines: list[str] = []
    lines.append(
        "Per-sector calibration bins. Each row shows the mean predicted "
        "probability and the empirical hit rate for resolutions whose "
        "model probability fell in that bin. Equal-width bins; sparse "
        f"bins (< {min_per_bin} resolutions) are flagged."
    )
    lines.append(
        "A positive gap means the model was under-confident in that bin; "
        "negative means over-confident. Treat sparse bins as noisy."
    )
    for sector_entry in sectors:
        sector = sector_entry.get("sector", "?")
        n_total = int(sector_entry.get("n_resolutions", 0))
        sector_window = int(sector_entry.get("window_days", 0))
        lines.append("")
        lines.append(f"{sector} (n={n_total}, window={sector_window}d)")
        lines.append(f"  {'bin':<14} {'n':>4}  {'mean_p':>8}  {'empirical':>10}  {'gap':>8}")
        for bin_entry in sector_entry.get("bins") or []:
            bin_lo = float(bin_entry.get("bin_lo", 0.0))
            bin_hi = float(bin_entry.get("bin_hi", 0.0))
            n = int(bin_entry.get("n", 0))
            label = f"[{bin_lo:.2f}, {bin_hi:.2f}{']' if bin_entry is sector_entry['bins'][-1] else ')'}"
            if n == 0:
                lines.append(f"  {label:<14} {'-':>4}  {'-':>8}  {'-':>10}  {'-':>8}  (empty)")
                continue
            mean_p = float(bin_entry.get("mean_predicted") or 0.0)
            empirical = float(bin_entry.get("empirical_rate") or 0.0)
            gap = float(bin_entry.get("calibration_gap") or 0.0)
            sparse = bool(bin_entry.get("sparse", False))
            tag = "  (sparse)" if sparse else ""
            lines.append(
                f"  {label:<14} {n:>4}  {mean_p:>8.4f}  {empirical:>10.4f}  {gap:>+8.4f}{tag}"
            )
        # ASCII calibration-curve overlay (v0.40.0). Drawn after the
        # per-bin table so the table remains the primary surface and
        # the chart is a visual cue.
        chart = render_calibration_chart(sector_entry.get("bins") or [])
        if chart:
            lines.append("")
            lines.append(chart)
    return "\n".join(lines).rstrip()


def _render_watchlist(content: Mapping[str, Any]) -> str:
    candidates = content.get("candidates") or []
    if not candidates:
        return ""
    lines: list[str] = []
    for cand in candidates:
        lines.append(f"- {cand.get('class_title', '?')} ({cand.get('domain_sector', '?')})")
        verbosity = cand.get("verbosity", "full")
        if verbosity == "full":
            lines.append(
                f"    posterior {cand.get('posterior', 0):.3f} "
                f"(base rate {cand.get('base_rate', 0):.3f}, "
                f"log-odds shift {cand.get('log_odds_shift', 0):+.3f})"
            )
            direction = cand.get("candidate_direction")
            if direction:
                lines.append(f"    direction: {direction}")
        lines.append(f"    reason: {cand.get('reason', '?')}")
        suggestion = cand.get("suggestion")
        if suggestion:
            lines.append(f"    suggestion: {suggestion}")
        lines.append("")
    return "\n".join(lines).rstrip()


__all__ = ["render"]
