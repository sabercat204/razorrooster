"""Analysis renderer (T-PE-040; design §3.5).

Fills the analysis text template with conditional language (REQ-PE-FRAME-002),
warnings before sizing math (REQ-PE-FRAME-003), and the standard
disclaimer block (REQ-PE-FRAME-001) — all by construction. Pair with
``frame/linter.py`` to enforce that no imperative phrasing slips in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from razor_rooster.position_engine.models import Analysis

# Standard disclaimer block. The exact text is asserted by REQ-PE-FRAME-001
# tests; do not edit lightly.
DISCLAIMER_BLOCK = (
    "DISCLAIMER:\n"
    "  This is decision-support analysis. Kelly figures are theoretical "
    "optima before\n"
    "  accounting for model error, transaction costs, slippage, and the "
    "possibility that\n"
    "  the model is wrong. Half-Kelly is the conservative default and "
    "should still be\n"
    "  considered an upper bound. The system does not place orders; the "
    "operator decides\n"
    "  whether and how to act, and is responsible for any real-world outcomes."
)


def render(
    analysis: Analysis,
    *,
    bankroll_usd: float,
    class_title: str | None = None,
    sector: str | None = None,
    market_spread_bps: int | None = None,
    log_odds_delta: float | None = None,
    model_ci: tuple[float, float] | None = None,
    verbose: bool = False,
) -> str:
    """Render an analysis to human-readable text.

    Args:
        analysis: The Analysis dataclass to render.
        bankroll_usd: Operator's analytical bankroll figure.
        class_title: Optional class title (from EventClass.title).
            Defaults to the class_id when unavailable.
        sector: Optional sector label (from EventClass.domain_sector).
        market_spread_bps: Optional market bid-ask spread; rendered when
            present.
        log_odds_delta: Optional log-odds delta from the source
            comparison; rendered when present.
        model_ci: Optional model credible interval (lower, upper).
        verbose: When True, include the sensitivity-analysis section.
    """
    title = class_title or analysis.class_id
    sector_str = sector or "(unknown)"

    warnings_block = _render_warnings(analysis)
    clamping_notes = _render_clamping_notes(analysis)
    invalidation_lines = _render_invalidation(analysis.invalidation_criteria)
    sensitivity_block = (
        _render_sensitivity(analysis.sensitivity_analysis)
        if verbose and analysis.sensitivity_analysis
        else ""
    )
    long_caveat = (
        "\n  This is a long-resolution market; uncertainty compounds over the "
        "lead time. Consider this when weighing the analysis."
        if analysis.long_time_to_resolution
        else ""
    )

    market_p_str = (
        f"{analysis.market_probability:.4f}" if analysis.market_probability is not None else "?"
    )
    spread_str = f"{market_spread_bps} bps" if market_spread_bps is not None else "?"
    delta_str = (
        f"{(analysis.model_probability - (analysis.market_probability or 0.0)):+.4f}"
        if analysis.market_probability is not None
        else "?"
    )
    log_odds_str = f"{log_odds_delta:+.3f}" if log_odds_delta is not None else "?"
    ci_str = f"[{model_ci[0]:.4f}, {model_ci[1]:.4f}]" if model_ci is not None else "(not provided)"
    pct_volume_str = (
        f"{analysis.suggested_pct_of_24h_volume:.4f}"
        if analysis.suggested_pct_of_24h_volume is not None
        else "?"
    )
    days_str = (
        f"{analysis.days_to_resolution} days"
        if analysis.days_to_resolution is not None
        else "(end date unknown)"
    )
    ev_str = (
        f"{analysis.ev_per_dollar:.4f}"
        if analysis.ev_per_dollar is not None
        else "(not computable)"
    )

    if analysis.sub_threshold:
        sizing_block = (
            "  Edge below the configured min_edge_threshold; no sizing math "
            "performed.\n"
            "  If the model and market move further apart, this analysis "
            "would be revisited."
        )
    else:
        sizing_block = (
            f"  Kelly fraction (theoretical maximum, before clamping): "
            f"{analysis.kelly_unclamped:.4f}\n"
            f"  Suggested fraction (after half-Kelly + caps, conservative): "
            f"{analysis.suggested_fraction:.4f}\n"
            f"  Suggested dollar size: ${analysis.suggested_dollar_size:.2f} "
            f"of ${bankroll_usd:.2f} analytical bankroll\n"
            f"  This represents {pct_volume_str} of the market's 24h volume.\n"
            f"  {clamping_notes}".rstrip()
        )

    text = (
        f"==============================================\n"
        f"ANALYSIS: {title}\n"
        f"SECTOR: {sector_str}\n"
        f"==============================================\n"
        f"\n"
        f"MARKET: {analysis.condition_id} ({analysis.venue})\n"
        f"\n"
        f"WARNINGS:\n{warnings_block}\n"
        f"\n"
        f"SOURCE COMPARISON:\n"
        f"  Model probability: {analysis.model_probability:.4f}  "
        f"(CI: {ci_str})\n"
        f"  Market-implied probability: {market_p_str}  (spread: {spread_str})\n"
        f"  Delta: {delta_str}  (log-odds: {log_odds_str})\n"
        f"\n"
        f"SIZING ANALYSIS (if the operator chose to act):\n"
        f"{sizing_block}\n"
        f"\n"
        f"BANKROLL-SURVIVAL SCENARIOS:\n"
        f"  After 1 adverse outcome: bankroll at "
        f"{analysis.bankroll_after_1_loss_pct:.4f} of starting\n"
        f"  After 3 adverse outcomes: bankroll at "
        f"{analysis.bankroll_after_3_losses_pct:.4f}\n"
        f"  After 5 adverse outcomes: bankroll at "
        f"{analysis.bankroll_after_5_losses_pct:.4f}\n"
        f"\n"
        f"EXPECTED VALUE (analytical metric, not a recommendation):\n"
        f"  EV per dollar (if held to resolution): {ev_str}\n"
        f"\n"
        f"INVALIDATION CRITERIA:\n{invalidation_lines}\n"
        f"\n"
        f"TIME TO RESOLUTION:\n  {days_str} remaining{long_caveat}\n"
    )
    if sensitivity_block:
        text += f"\n{sensitivity_block}\n"
    text += f"\n{DISCLAIMER_BLOCK}\n"
    text += "==============================================\n"
    return text


def to_structured_dict(
    analysis: Analysis,
    *,
    bankroll_usd: float,
    class_title: str | None = None,
    sector: str | None = None,
    log_odds_delta: float | None = None,
    market_spread_bps: int | None = None,
) -> dict[str, Any]:
    """Return a JSON-serializable structured projection of the analysis."""
    return {
        "analysis_id": analysis.analysis_id,
        "class_id": analysis.class_id,
        "class_title": class_title or analysis.class_id,
        "sector": sector,
        "comparison_id": analysis.comparison_id,
        "venue": analysis.venue,
        "condition_id": analysis.condition_id,
        "model_probability": analysis.model_probability,
        "market_probability": analysis.market_probability,
        "market_spread_bps": market_spread_bps,
        "log_odds_delta": log_odds_delta,
        "kelly_unclamped": analysis.kelly_unclamped,
        "suggested_fraction": analysis.suggested_fraction,
        "suggested_dollar_size": analysis.suggested_dollar_size,
        "analytical_bankroll_usd": bankroll_usd,
        "ev_per_dollar": analysis.ev_per_dollar,
        "bankroll_after_1_loss_pct": analysis.bankroll_after_1_loss_pct,
        "bankroll_after_3_losses_pct": analysis.bankroll_after_3_losses_pct,
        "bankroll_after_5_losses_pct": analysis.bankroll_after_5_losses_pct,
        "suggested_pct_of_24h_volume": analysis.suggested_pct_of_24h_volume,
        "days_to_resolution": analysis.days_to_resolution,
        "long_time_to_resolution": analysis.long_time_to_resolution,
        "sub_threshold": analysis.sub_threshold,
        "kelly_negative": analysis.kelly_negative,
        "kelly_clamped_by_max_cap": analysis.kelly_clamped_by_max_cap,
        "kelly_clamped_by_liquidity": analysis.kelly_clamped_by_liquidity,
        "low_signature_confidence": analysis.low_signature_confidence,
        "source_stale_warning": analysis.source_stale_warning,
        "library_stale_warning": analysis.library_stale_warning,
        "definition_drift_warning": analysis.definition_drift_warning,
        "low_mapping_confidence": analysis.low_mapping_confidence,
        "low_liquidity": analysis.low_liquidity,
        "invalidation_criteria": [dict(c) for c in analysis.invalidation_criteria],
        "sensitivity_analysis": (
            dict(analysis.sensitivity_analysis) if analysis.sensitivity_analysis else None
        ),
        "error": analysis.error,
    }


# -- internals --------------------------------------------------------------


def _render_warnings(analysis: Analysis) -> str:
    flags: list[tuple[str, bool]] = [
        ("low_signature_confidence", analysis.low_signature_confidence),
        ("source_stale_warning", analysis.source_stale_warning),
        ("library_stale_warning", analysis.library_stale_warning),
        ("definition_drift_warning", analysis.definition_drift_warning),
        ("low_mapping_confidence", analysis.low_mapping_confidence),
        ("low_liquidity", analysis.low_liquidity),
        ("kelly_negative", analysis.kelly_negative),
        ("kelly_clamped_by_max_cap", analysis.kelly_clamped_by_max_cap),
        ("kelly_clamped_by_liquidity", analysis.kelly_clamped_by_liquidity),
        ("long_time_to_resolution", analysis.long_time_to_resolution),
        ("sub_threshold", analysis.sub_threshold),
    ]
    active = [f"  - {name}" for name, fired in flags if fired]
    if not active:
        return "  (no warnings)"
    return "\n".join(active)


def _render_clamping_notes(analysis: Analysis) -> str:
    notes: list[str] = []
    if analysis.kelly_clamped_by_max_cap:
        notes.append("Suggested fraction was clamped down by the max_single_position_pct cap.")
    if analysis.kelly_clamped_by_liquidity:
        notes.append("Suggested fraction was clamped down by the liquidity feasibility threshold.")
    if analysis.kelly_negative:
        notes.append("Unclamped Kelly was negative; suggested fraction is zero.")
    if not notes:
        return "(no clamping applied)"
    return " ".join(notes)


def _render_invalidation(
    criteria: Sequence[Mapping[str, Any]],
) -> str:
    if not criteria:
        return "  (no invalidation criteria computable for this analysis)"
    lines = []
    for entry in criteria:
        description = entry.get("description") or entry.get("type") or "(criterion)"
        lines.append(f"  - {description}")
    return "\n".join(lines)


def _render_sensitivity(sensitivity: Mapping[str, Any]) -> str:
    rows = sensitivity.get("perturbations") or []
    if not rows:
        return ""
    lines = ["SENSITIVITY (model_p perturbations):"]
    for row in rows:
        delta = row.get("delta_pct")
        perturbed = row.get("model_p_perturbed")
        suggested = row.get("suggested_fraction")
        delta_str = f"{float(delta):+.2f}" if delta is not None else "?"
        perturbed_str = f"{float(perturbed):.4f}" if perturbed is not None else "?"
        suggested_str = f"{float(suggested):.4f}" if suggested is not None else "?"
        lines.append(
            f"  delta={delta_str}  model_p->{perturbed_str}  suggested_fraction={suggested_str}"
        )
    return "\n".join(lines)
