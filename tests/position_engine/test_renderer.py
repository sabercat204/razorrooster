"""T-PE-040 — analysis renderer tests."""

from __future__ import annotations

from datetime import UTC, datetime

from razor_rooster.position_engine.frame.linter import check_text
from razor_rooster.position_engine.frame.renderer import (
    DISCLAIMER_BLOCK,
    render,
    to_structured_dict,
)
from razor_rooster.position_engine.models import Analysis


def _make_analysis(
    *,
    sub_threshold: bool = False,
    kelly_negative: bool = False,
    suggested_fraction: float = 0.025,
    long_time_to_resolution: bool = False,
    sensitivity: dict[str, object] | None = None,
    invalidation_criteria: list[dict[str, object]] | None = None,
    low_signature_confidence: bool = False,
) -> Analysis:
    return Analysis(
        analysis_id="an-1",
        cycle_id="cy-1",
        comparison_id="cmp-1",
        class_id="pheic_declaration_12mo",
        condition_id="0xabc",
        bankroll_config_id="cfg-1",
        model_probability=0.30,
        market_probability=0.10,
        kelly_unclamped=0.20,
        kelly_negative=kelly_negative,
        kelly_clamped_by_max_cap=False,
        kelly_clamped_by_liquidity=False,
        suggested_fraction=suggested_fraction,
        suggested_dollar_size=suggested_fraction * 1000.0,
        ev_per_dollar=0.20,
        bankroll_after_1_loss_pct=1.0 - suggested_fraction,
        bankroll_after_3_losses_pct=(1.0 - suggested_fraction) ** 3,
        bankroll_after_5_losses_pct=(1.0 - suggested_fraction) ** 5,
        suggested_pct_of_24h_volume=0.025,
        days_to_resolution=180,
        long_time_to_resolution=long_time_to_resolution,
        sub_threshold=sub_threshold,
        sensitivity_analysis=sensitivity,
        invalidation_criteria=tuple(invalidation_criteria or []),
        low_signature_confidence=low_signature_confidence,
        computed_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
    )


def test_render_contains_disclaimer_verbatim() -> None:
    """REQ-PE-FRAME-001 — exact disclaimer text appears."""
    text = render(
        _make_analysis(),
        bankroll_usd=1000.0,
        class_title="WHO PHEIC declaration in a 12-month window",
        sector="public_health",
    )
    assert DISCLAIMER_BLOCK in text


def test_render_uses_conditional_language() -> None:
    """REQ-PE-FRAME-002 — output uses 'if the operator chose to act'."""
    text = render(
        _make_analysis(),
        bankroll_usd=1000.0,
        class_title="x",
    )
    assert "if the operator chose to act" in text


def test_render_warnings_appear_before_sizing() -> None:
    """REQ-PE-FRAME-003 — warnings before sizing math."""
    text = render(
        _make_analysis(low_signature_confidence=True),
        bankroll_usd=1000.0,
        class_title="x",
    )
    warnings_idx = text.find("WARNINGS:")
    sizing_idx = text.find("SIZING ANALYSIS")
    assert warnings_idx >= 0
    assert sizing_idx >= 0
    assert warnings_idx < sizing_idx


def test_render_sub_threshold_skips_sizing_math() -> None:
    text = render(
        _make_analysis(sub_threshold=True),
        bankroll_usd=1000.0,
        class_title="x",
    )
    assert "min_edge_threshold" in text


def test_render_no_warnings_message() -> None:
    text = render(
        _make_analysis(),
        bankroll_usd=1000.0,
        class_title="x",
    )
    assert "(no warnings)" in text


def test_render_long_resolution_caveat() -> None:
    text = render(
        _make_analysis(long_time_to_resolution=True),
        bankroll_usd=1000.0,
        class_title="x",
    )
    assert "long-resolution" in text


def test_render_invalidation_criteria_appear() -> None:
    text = render(
        _make_analysis(
            invalidation_criteria=[
                {
                    "type": "precursor_shift",
                    "description": "if v1 drops below 5.0, signal weakens",
                }
            ]
        ),
        bankroll_usd=1000.0,
        class_title="x",
    )
    assert "drops below 5.0" in text


def test_render_normal_mode_skips_sensitivity() -> None:
    text = render(
        _make_analysis(
            sensitivity={"perturbations": [{"delta_pct": 0.10, "suggested_fraction": 0.022}]}
        ),
        bankroll_usd=1000.0,
        class_title="x",
        verbose=False,
    )
    assert "SENSITIVITY" not in text


def test_render_verbose_includes_sensitivity() -> None:
    text = render(
        _make_analysis(
            sensitivity={
                "perturbations": [
                    {
                        "delta_pct": 0.10,
                        "model_p_perturbed": 0.40,
                        "suggested_fraction": 0.030,
                    }
                ]
            }
        ),
        bankroll_usd=1000.0,
        class_title="x",
        verbose=True,
    )
    assert "SENSITIVITY" in text
    assert "0.10" in text


def test_render_passes_linter() -> None:
    """Standard renderer output never contains forbidden phrases."""
    text = render(
        _make_analysis(),
        bankroll_usd=1000.0,
        class_title="WHO PHEIC declaration in a 12-month window",
        sector="public_health",
        market_spread_bps=200,
        log_odds_delta=1.4,
        model_ci=(0.15, 0.50),
    )
    check_text(text)


def test_to_structured_dict_basic() -> None:
    analysis = _make_analysis()
    payload = to_structured_dict(
        analysis,
        bankroll_usd=1000.0,
        class_title="WHO PHEIC",
        sector="public_health",
        log_odds_delta=1.4,
        market_spread_bps=200,
    )
    assert payload["analysis_id"] == "an-1"
    assert payload["class_title"] == "WHO PHEIC"
    assert payload["sector"] == "public_health"
    assert payload["suggested_fraction"] == 0.025
