"""Comparison reasoning trace builder + renderer (T-MD-033; design §3.7).

Produces a JSON-serializable dict per design §3.7 with three reasoning
sections — ``case_for_model``, ``case_for_market``, ``ambiguity_factors``.
By design (REQ-MD-TRACE-005) the first two are at equal prominence so
the renderer's output can't accidentally favour one over the other.

Render contract: the text renderer emits ``case_for_model`` and
``case_for_market`` as two adjacent equal-prominence blocks, both with
the same number of bullet points where possible (the system pads the
shorter list with explicit "no specific items identified" entries
rather than collapsing the section).

The full ``signal_scanner`` reasoning trace is embedded verbatim under
``embedded_scanner_trace`` so the comparison's audit trail covers raw
data → model probability → comparison.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from razor_rooster.mispricing_detector.models import (
    ClassMarketMapping,
    Polarity,
)


def build_trace(
    *,
    class_id: str,
    condition_id: str,
    polarity: Polarity,
    mapping: ClassMarketMapping,
    model_probability: float,
    model_ci: tuple[float, float],
    market_probability: float | None,
    market_best_bid: float | None,
    market_best_ask: float | None,
    market_volume_24h: float | None,
    market_spread_bps: int | None,
    market_snapshot_ts: str | None,
    delta: float | None,
    log_odds_delta: float | None,
    ci_overlap: bool,
    expected_value_: float | None,
    confidence_weighted_score: float | None,
    embedded_scanner_trace: Mapping[str, Any] | None,
    case_for_model: Sequence[str],
    case_for_market: Sequence[str],
    ambiguity_factors: Sequence[str],
    warnings: Sequence[str],
    suppression_reasons: Sequence[str],
    surfaced: bool,
) -> dict[str, Any]:
    """Assemble the trace JSON for one comparison."""
    # Pad the shorter case section so REQ-MD-TRACE-005 (equal prominence)
    # holds even if one side has nothing concrete to say.
    padded_model, padded_market = _balance_cases(case_for_model, case_for_market)

    return {
        "class_id": class_id,
        "condition_id": condition_id,
        "polarity": polarity,
        "mapping": {
            "mapping_id": mapping.mapping_id,
            "type": mapping.mapping_type,
            "confidence": mapping.mapping_confidence,
            "mapped_by": mapping.mapped_by,
        },
        "model_probability": float(model_probability),
        "model_ci": [float(model_ci[0]), float(model_ci[1])],
        "market_probability": (
            float(market_probability) if market_probability is not None else None
        ),
        "market_best_bid": (float(market_best_bid) if market_best_bid is not None else None),
        "market_best_ask": (float(market_best_ask) if market_best_ask is not None else None),
        "market_volume_24h": (float(market_volume_24h) if market_volume_24h is not None else None),
        "market_spread_bps": (int(market_spread_bps) if market_spread_bps is not None else None),
        "market_snapshot_ts": market_snapshot_ts,
        "delta": float(delta) if delta is not None else None,
        "log_odds_delta": (float(log_odds_delta) if log_odds_delta is not None else None),
        "ci_overlap": bool(ci_overlap),
        "expected_value": (float(expected_value_) if expected_value_ is not None else None),
        "confidence_weighted_score": (
            float(confidence_weighted_score) if confidence_weighted_score is not None else None
        ),
        "embedded_scanner_trace": dict(embedded_scanner_trace or {}),
        "case_for_model": list(padded_model),
        "case_for_market": list(padded_market),
        "ambiguity_factors": list(ambiguity_factors),
        "warnings": list(warnings),
        "suppression_reasons": list(suppression_reasons),
        "surfaced": bool(surfaced),
    }


def render_trace_text(trace: Mapping[str, Any]) -> str:
    """Render the comparison trace to human-readable text.

    The two case sections appear as adjacent equal-prominence blocks
    with matching headers and identical formatting. Ambiguity factors
    follow as a third block.
    """
    lines: list[str] = []
    lines.append(f"class:           {trace.get('class_id')}")
    lines.append(f"condition_id:    {trace.get('condition_id')}")
    lines.append(f"polarity:        {trace.get('polarity')}")
    mapping = trace.get("mapping") or {}
    lines.append(
        f"mapping:         type={mapping.get('type')} "
        f"confidence={mapping.get('confidence')} mapped_by={mapping.get('mapped_by')}"
    )
    model_p = trace.get("model_probability")
    model_ci = trace.get("model_ci") or [0.0, 0.0]
    lines.append(
        f"model:           p={float(model_p or 0.0):.4f} "
        f"CI=[{float(model_ci[0]):.4f}, {float(model_ci[1]):.4f}]"
    )
    market_p = trace.get("market_probability")
    if market_p is None:
        lines.append("market:          (no price)")
    else:
        bid = trace.get("market_best_bid")
        ask = trace.get("market_best_ask")
        bid_str = f"{float(bid):.4f}" if bid is not None else "?"
        ask_str = f"{float(ask):.4f}" if ask is not None else "?"
        lines.append(f"market:          p={float(market_p):.4f} bid={bid_str} ask={ask_str}")
    delta = trace.get("delta")
    log_delta = trace.get("log_odds_delta")
    if delta is not None and log_delta is not None:
        lines.append(f"delta:           {float(delta):+.4f} (log-odds {float(log_delta):+.3f})")
    if trace.get("ci_overlap"):
        lines.append("ci_overlap:      YES (no material disagreement)")
    else:
        lines.append("ci_overlap:      no")
    if trace.get("surfaced"):
        lines.append("surfaced:        YES")
    else:
        reasons = trace.get("suppression_reasons") or []
        lines.append("surfaced:        no" + (f" ({', '.join(reasons)})" if reasons else ""))
    lines.append("")

    # Equal-prominence reasoning blocks. Both sections always appear,
    # both have identical headers, both render bullet items the same way.
    lines.append("Possible reasons the model may be right:")
    lines.extend(_render_bullets(trace.get("case_for_model") or []))
    lines.append("")
    lines.append("Possible reasons the market may be right:")
    lines.extend(_render_bullets(trace.get("case_for_market") or []))
    lines.append("")

    factors = trace.get("ambiguity_factors") or []
    if factors:
        lines.append("Ambiguity factors:")
        lines.extend(_render_bullets(factors))
        lines.append("")

    warnings = trace.get("warnings") or []
    if warnings:
        lines.append(f"warnings:        {', '.join(str(w) for w in warnings)}")
    return "\n".join(lines).rstrip() + "\n"


# -- helpers ----------------------------------------------------------------


def case_for_model_from_signature(*, embedded_scanner_trace: Mapping[str, Any] | None) -> list[str]:
    """Extract bullet items for the case-for-model section from the
    embedded scanner trace.

    Each fired precursor with a non-trivial likelihood ratio
    contributes one bullet. When no scanner trace is available, returns
    a single explanatory item.
    """
    if not embedded_scanner_trace:
        return ["Model trace not available; comparison surfaced on point estimate alone."]
    precursors = embedded_scanner_trace.get("precursors") or []
    bullets: list[str] = []
    for entry in precursors:
        if not entry.get("fired"):
            continue
        title = entry.get("title") or entry.get("variable_id") or "(unnamed)"
        hit = entry.get("hit_rate")
        fpr = entry.get("false_positive_rate")
        lr = entry.get("likelihood_ratio_applied")
        hit_str = f"{float(hit):.2f}" if hit is not None else "?"
        fpr_str = f"{float(fpr):.2f}" if fpr is not None else "?"
        lr_str = f"{float(lr):.2f}" if lr is not None else "?"
        bullets.append(
            f"Precursor {title!r} fired with hit rate {hit_str}, FPR {fpr_str}, "
            f"applied LR {lr_str}."
        )
    if not bullets:
        bullets.append("No precursors fired hard enough to drive the model away from the prior.")
    return bullets


def case_for_market_from_context(
    *,
    market_volume_24h: float | None,
    market_spread_bps: int | None,
    market_probability: float | None,
    market_snapshot_ts: str | None,
    liquidity_floor: float | None,
    embedded_scanner_trace: Mapping[str, Any] | None = None,
) -> list[str]:
    """Construct the case-for-market section from market context.

    The system intentionally treats the market as default-correct: this
    function generates substantive observations about the market's
    information content even when those observations might be
    speculative. The renderer is the operator's primary interface to
    the comparison and must always present a serious case for the
    market view.
    """
    bullets: list[str] = []
    if market_probability is not None:
        bullets.append(
            f"Market price reflects aggregate trader belief at "
            f"{float(market_probability):.4f}; participants have priced in "
            f"information not necessarily captured by the model's precursors."
        )
    if market_volume_24h is not None and market_volume_24h > 0:
        floor_text = (
            f" (liquidity floor {liquidity_floor:.0f})" if liquidity_floor is not None else ""
        )
        bullets.append(
            f"24h volume of {market_volume_24h:.0f}{floor_text} suggests "
            "active price discovery from at least some informed participants."
        )
    if market_spread_bps is not None:
        bullets.append(
            f"Bid-ask spread of {market_spread_bps} bps indicates the level of "
            "agreement among market makers on the current price."
        )
    if embedded_scanner_trace is not None:
        warnings = embedded_scanner_trace.get("warnings") or []
        if "low_sample" in warnings:
            bullets.append(
                "The model's base rate is computed on a small sample; the market "
                "may reflect updated probabilities from sources the model's "
                "historical record cannot capture."
            )
        if "library_stale_warning" in warnings or "library_stale" in warnings:
            bullets.append(
                "Pattern library has not been refreshed recently; market may "
                "have priced in events that the model has not yet ingested."
            )
    if market_snapshot_ts is not None:
        bullets.append(
            f"Latest market snapshot at {market_snapshot_ts} provides a more "
            "recent view of conditions than the model's data_as_of timestamp."
        )
    if not bullets:
        bullets.append(
            "Market price reflects aggregate trader belief; the model may be "
            "missing information the market has priced in."
        )
    return bullets


def ambiguity_factors_from_inputs(
    *,
    mapping_confidence: str,
    ci_overlap: bool,
    polarity: Polarity,
) -> list[str]:
    """Construct the ambiguity-factors section."""
    bullets: list[str] = []
    if mapping_confidence == "low":
        bullets.append(
            "Mapping confidence is 'low' — the class question and market question "
            "may differ in interpretation."
        )
    elif mapping_confidence == "inferred":
        bullets.append(
            "Mapping is auto-derived ('inferred'); operator review recommended "
            "before relying on the comparison for decision support."
        )
    if ci_overlap:
        bullets.append(
            "Model credible interval and market spread overlap, suggesting the "
            "two views may be consistent at current uncertainty levels."
        )
    if polarity == "inverted":
        bullets.append(
            "Mapping is inverted: market YES means event does NOT happen. "
            "Verify the polarity matches the question framing."
        )
    return bullets


# -- internals --------------------------------------------------------------


def _balance_cases(
    case_for_model: Sequence[str], case_for_market: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Pad the shorter side with explicit 'no specific items' entries.

    Ensures REQ-MD-TRACE-005 (equal prominence) holds — the renderer
    can't accidentally make one side look smaller because the
    upstream factory ran short on bullets.
    """
    model_list = list(case_for_model) if case_for_model else []
    market_list = list(case_for_market) if case_for_market else []
    target_len = max(len(model_list), len(market_list), 1)
    placeholder_model = "(no specific items identified for the model side)"
    placeholder_market = "(no specific items identified for the market side)"
    while len(model_list) < target_len:
        model_list.append(placeholder_model)
    while len(market_list) < target_len:
        market_list.append(placeholder_market)
    return model_list, market_list


def _render_bullets(items: Sequence[Any]) -> list[str]:
    return [f"  - {item}" for item in items]


__all__ = [
    "ambiguity_factors_from_inputs",
    "build_trace",
    "case_for_market_from_context",
    "case_for_model_from_signature",
    "render_trace_text",
]
