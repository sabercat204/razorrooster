"""T-PE-034 — sensitivity-analysis tests."""

from __future__ import annotations

from razor_rooster.position_engine.engines.sensitivity import compute_sensitivity


def test_returns_perturbations_in_both_directions() -> None:
    result = compute_sensitivity(
        model_probability=0.30,
        market_probability=0.10,
        kelly_fraction_default=0.5,
        max_single_position_pct=0.25,
        perturbations=(0.10, 0.20),
    )
    perturbations = result["perturbations"]
    deltas = {row["delta_pct"] for row in perturbations}
    assert deltas == {0.10, -0.10, 0.20, -0.20}


def test_perturbation_changes_suggested_fraction() -> None:
    result = compute_sensitivity(
        model_probability=0.30,
        market_probability=0.10,
        kelly_fraction_default=0.5,
        max_single_position_pct=0.25,
        perturbations=(0.10,),
    )
    rows = result["perturbations"]
    plus_10 = next(r for r in rows if r["delta_pct"] == 0.10)
    minus_10 = next(r for r in rows if r["delta_pct"] == -0.10)
    # Higher model_p → higher Kelly.
    assert plus_10["suggested_fraction"] > minus_10["suggested_fraction"]


def test_clips_perturbed_probability_to_unit_interval() -> None:
    """Perturbing 0.05 by -0.20 would go negative; gets clipped."""
    result = compute_sensitivity(
        model_probability=0.05,
        market_probability=0.10,
        kelly_fraction_default=0.5,
        max_single_position_pct=0.25,
        perturbations=(0.20,),
    )
    rows = result["perturbations"]
    minus_20 = next(r for r in rows if r["delta_pct"] == -0.20)
    assert 0.0 < minus_20["model_p_perturbed"] < 1.0


def test_method_field_documents_approach() -> None:
    result = compute_sensitivity(
        model_probability=0.30,
        market_probability=0.10,
        kelly_fraction_default=0.5,
        max_single_position_pct=0.25,
    )
    assert "method" in result
    assert "kelly" in result["method"].lower()


def test_default_perturbations_are_10_and_20() -> None:
    result = compute_sensitivity(
        model_probability=0.30,
        market_probability=0.10,
        kelly_fraction_default=0.5,
        max_single_position_pct=0.25,
    )
    deltas = {row["delta_pct"] for row in result["perturbations"]}
    assert deltas == {0.10, -0.10, 0.20, -0.20}
