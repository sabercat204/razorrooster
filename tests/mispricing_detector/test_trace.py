"""T-MD-033 — comparison trace tests."""

from __future__ import annotations

import json

from razor_rooster.mispricing_detector.engines.trace import (
    ambiguity_factors_from_inputs,
    build_trace,
    case_for_market_from_context,
    case_for_model_from_signature,
    render_trace_text,
)
from razor_rooster.mispricing_detector.models import ClassMarketMapping


def _make_mapping(
    *,
    confidence: str = "exact",
) -> ClassMarketMapping:
    return ClassMarketMapping(
        mapping_id="m-1",
        class_id="cls",
        condition_id="0xabc",
        mapping_type="direct",
        mapping_confidence=confidence,  # type: ignore[arg-type]
    )


def _scanner_trace_with_fired() -> dict[str, object]:
    return {
        "class_id": "cls",
        "warnings": ["low_sample"],
        "precursors": [
            {
                "variable_id": "v1",
                "title": "First precursor",
                "fired": True,
                "hit_rate": 0.7,
                "false_positive_rate": 0.2,
                "likelihood_ratio_applied": 3.5,
            },
            {
                "variable_id": "v2",
                "title": "Second precursor",
                "fired": False,
                "hit_rate": 0.5,
                "false_positive_rate": 0.5,
                "likelihood_ratio_applied": 1.0,
            },
        ],
    }


def test_build_trace_populates_expected_fields() -> None:
    mapping = _make_mapping()
    trace = build_trace(
        class_id="cls",
        condition_id="0xabc",
        polarity="aligned",
        mapping=mapping,
        model_probability=0.30,
        model_ci=(0.20, 0.40),
        market_probability=0.10,
        market_best_bid=0.09,
        market_best_ask=0.11,
        market_volume_24h=20000.0,
        market_spread_bps=200,
        market_snapshot_ts="2026-05-15T11:00:00+00:00",
        delta=0.20,
        log_odds_delta=1.4,
        ci_overlap=False,
        expected_value_=0.20,
        confidence_weighted_score=1.12,
        embedded_scanner_trace=_scanner_trace_with_fired(),
        case_for_model=["model bullet 1", "model bullet 2"],
        case_for_market=["market bullet 1", "market bullet 2"],
        ambiguity_factors=["ambiguity 1"],
        warnings=("low_sample",),
        suppression_reasons=(),
        surfaced=True,
    )
    assert trace["class_id"] == "cls"
    assert trace["polarity"] == "aligned"
    assert trace["mapping"]["confidence"] == "exact"
    assert trace["model_probability"] == 0.30
    assert trace["market_probability"] == 0.10
    assert trace["delta"] == 0.20
    assert trace["log_odds_delta"] == 1.4
    assert trace["surfaced"] is True
    assert trace["case_for_model"] == ["model bullet 1", "model bullet 2"]
    assert trace["case_for_market"] == ["market bullet 1", "market bullet 2"]
    assert trace["ambiguity_factors"] == ["ambiguity 1"]
    assert trace["embedded_scanner_trace"]["class_id"] == "cls"


def test_build_trace_pads_shorter_case_section() -> None:
    """REQ-MD-TRACE-005 — equal prominence enforced even when upstream
    provides asymmetric bullets.
    """
    mapping = _make_mapping()
    trace = build_trace(
        class_id="cls",
        condition_id="0xabc",
        polarity="aligned",
        mapping=mapping,
        model_probability=0.30,
        model_ci=(0.20, 0.40),
        market_probability=0.10,
        market_best_bid=0.09,
        market_best_ask=0.11,
        market_volume_24h=20000.0,
        market_spread_bps=200,
        market_snapshot_ts=None,
        delta=0.20,
        log_odds_delta=1.4,
        ci_overlap=False,
        expected_value_=0.20,
        confidence_weighted_score=1.12,
        embedded_scanner_trace=None,
        case_for_model=["a", "b", "c"],
        case_for_market=["x"],
        ambiguity_factors=(),
        warnings=(),
        suppression_reasons=(),
        surfaced=True,
    )
    assert len(trace["case_for_model"]) == 3
    assert len(trace["case_for_market"]) == 3
    assert any("no specific items" in s for s in trace["case_for_market"])


def test_trace_is_json_serializable() -> None:
    mapping = _make_mapping()
    trace = build_trace(
        class_id="cls",
        condition_id="0xabc",
        polarity="aligned",
        mapping=mapping,
        model_probability=0.30,
        model_ci=(0.20, 0.40),
        market_probability=0.10,
        market_best_bid=0.09,
        market_best_ask=0.11,
        market_volume_24h=20000.0,
        market_spread_bps=200,
        market_snapshot_ts=None,
        delta=0.20,
        log_odds_delta=1.4,
        ci_overlap=False,
        expected_value_=0.20,
        confidence_weighted_score=1.12,
        embedded_scanner_trace=_scanner_trace_with_fired(),
        case_for_model=["a"],
        case_for_market=["b"],
        ambiguity_factors=(),
        warnings=(),
        suppression_reasons=(),
        surfaced=True,
    )
    serialized = json.dumps(trace)
    deserialized = json.loads(serialized)
    assert deserialized == trace


def test_render_trace_text_has_both_case_sections() -> None:
    """REQ-MD-TRACE-005: case-for-model and case-for-market sections at
    equal prominence. Same header style, same number of bullets after
    padding.
    """
    mapping = _make_mapping()
    trace = build_trace(
        class_id="cls",
        condition_id="0xabc",
        polarity="aligned",
        mapping=mapping,
        model_probability=0.30,
        model_ci=(0.20, 0.40),
        market_probability=0.10,
        market_best_bid=0.09,
        market_best_ask=0.11,
        market_volume_24h=20000.0,
        market_spread_bps=200,
        market_snapshot_ts="2026-05-15T11:00:00+00:00",
        delta=0.20,
        log_odds_delta=1.4,
        ci_overlap=False,
        expected_value_=0.20,
        confidence_weighted_score=1.12,
        embedded_scanner_trace=_scanner_trace_with_fired(),
        case_for_model=["model item one", "model item two"],
        case_for_market=["market item one", "market item two"],
        ambiguity_factors=("amb 1",),
        warnings=("low_sample",),
        suppression_reasons=(),
        surfaced=True,
    )
    rendered = render_trace_text(trace)
    assert "Possible reasons the model may be right:" in rendered
    assert "Possible reasons the market may be right:" in rendered
    assert "model item one" in rendered
    assert "market item one" in rendered
    # Each case section must contribute at least the same bullet count.
    model_count = rendered.count("model item")
    market_count = rendered.count("market item")
    assert model_count == market_count


def test_render_trace_text_handles_no_market_price() -> None:
    mapping = _make_mapping()
    trace = build_trace(
        class_id="cls",
        condition_id="0xabc",
        polarity="aligned",
        mapping=mapping,
        model_probability=0.30,
        model_ci=(0.20, 0.40),
        market_probability=None,
        market_best_bid=None,
        market_best_ask=None,
        market_volume_24h=None,
        market_spread_bps=None,
        market_snapshot_ts=None,
        delta=None,
        log_odds_delta=None,
        ci_overlap=False,
        expected_value_=None,
        confidence_weighted_score=None,
        embedded_scanner_trace=None,
        case_for_model=["one"],
        case_for_market=[],
        ambiguity_factors=(),
        warnings=("no_market_price",),
        suppression_reasons=("no_market_price",),
        surfaced=False,
    )
    rendered = render_trace_text(trace)
    assert "(no price)" in rendered
    assert "no_market_price" in rendered


def test_case_for_model_from_signature_lists_fired() -> None:
    bullets = case_for_model_from_signature(embedded_scanner_trace=_scanner_trace_with_fired())
    assert any("First precursor" in b for b in bullets)
    assert any("0.70" in b for b in bullets)


def test_case_for_model_from_signature_handles_missing_trace() -> None:
    bullets = case_for_model_from_signature(embedded_scanner_trace=None)
    assert bullets
    assert "Model trace not available" in bullets[0]


def test_case_for_market_from_context_emits_substantive_bullets() -> None:
    bullets = case_for_market_from_context(
        market_volume_24h=20000.0,
        market_spread_bps=200,
        market_probability=0.10,
        market_snapshot_ts="2026-05-15T11:00:00+00:00",
        liquidity_floor=10000.0,
        embedded_scanner_trace=_scanner_trace_with_fired(),
    )
    # At least three substantive bullets, not just "market may be right"
    assert len(bullets) >= 3
    # The low_sample warning should produce a market-side note.
    assert any("small sample" in b for b in bullets)


def test_case_for_market_handles_no_market_data() -> None:
    bullets = case_for_market_from_context(
        market_volume_24h=None,
        market_spread_bps=None,
        market_probability=None,
        market_snapshot_ts=None,
        liquidity_floor=None,
    )
    assert bullets
    assert any("aggregate trader belief" in b for b in bullets)


def test_ambiguity_factors_polarity_inverted() -> None:
    bullets = ambiguity_factors_from_inputs(
        mapping_confidence="exact", ci_overlap=False, polarity="inverted"
    )
    assert any("inverted" in b for b in bullets)


def test_ambiguity_factors_low_mapping_confidence() -> None:
    bullets = ambiguity_factors_from_inputs(
        mapping_confidence="low", ci_overlap=False, polarity="aligned"
    )
    assert any("'low'" in b for b in bullets)


def test_ambiguity_factors_ci_overlap() -> None:
    bullets = ambiguity_factors_from_inputs(
        mapping_confidence="exact", ci_overlap=True, polarity="aligned"
    )
    assert any("overlap" in b for b in bullets)
