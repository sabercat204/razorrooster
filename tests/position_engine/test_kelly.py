"""T-PE-030 — Kelly math tests."""

from __future__ import annotations

import pytest

from razor_rooster.position_engine.engines.kelly import (
    apply_pipeline,
    kelly_fraction,
)


def test_kelly_positive_when_model_above_market() -> None:
    # model 0.3, market 0.1: Kelly = (0.3 - 0.1) / (1 - 0.1) = 0.222...
    assert kelly_fraction(0.30, 0.10) == pytest.approx(0.2222, abs=0.001)


def test_kelly_negative_when_model_below_market() -> None:
    # model 0.05, market 0.20: negative
    f = kelly_fraction(0.05, 0.20)
    assert f < 0.0


def test_kelly_zero_when_equal() -> None:
    assert kelly_fraction(0.20, 0.20) == pytest.approx(0.0)


def test_kelly_handles_market_boundary_zero() -> None:
    """market_p = 0 would be infinite; eps clipping makes it finite."""
    f = kelly_fraction(0.50, 0.0)
    assert f > 0.0
    assert f < 1.0  # clipped, not exactly 1


def test_kelly_handles_market_boundary_one() -> None:
    """market_p = 1 would be undefined; eps clipping makes it finite."""
    f = kelly_fraction(0.50, 1.0)
    assert f < 0.0


def test_kelly_returns_zero_when_market_none() -> None:
    assert kelly_fraction(0.50, None) == 0.0


def test_apply_pipeline_clamps_negative_to_zero() -> None:
    result = apply_pipeline(
        model_p=0.05,
        market_p=0.50,
        kelly_fraction_default=0.5,
        max_single_position_pct=0.05,
    )
    assert result.kelly_negative is True
    assert result.suggested_after_max_cap == 0.0


def test_apply_pipeline_half_kelly_default() -> None:
    """Half-Kelly multiplies by 0.5 default."""
    result = apply_pipeline(
        model_p=0.30,
        market_p=0.10,
        kelly_fraction_default=0.5,
        max_single_position_pct=0.25,
    )
    # Unclamped Kelly ~ 0.222; after half = ~0.111; under cap.
    assert result.suggested_after_default == pytest.approx(0.111, abs=0.005)
    assert result.suggested_after_max_cap == pytest.approx(0.111, abs=0.005)
    assert result.clamped_by_max_cap is False


def test_apply_pipeline_clamps_by_max_cap() -> None:
    result = apply_pipeline(
        model_p=0.80,
        market_p=0.10,
        kelly_fraction_default=0.5,
        max_single_position_pct=0.05,
    )
    # Unclamped Kelly ~ 0.778; half ~ 0.389; clamped to 0.05.
    assert result.clamped_by_max_cap is True
    assert result.suggested_after_max_cap == pytest.approx(0.05)


def test_apply_pipeline_zero_kelly_default_means_no_size() -> None:
    result = apply_pipeline(
        model_p=0.30,
        market_p=0.10,
        kelly_fraction_default=0.0,
        max_single_position_pct=0.25,
    )
    assert result.suggested_after_max_cap == 0.0
    assert result.kelly_negative is False  # original Kelly was positive


def test_apply_pipeline_unclamped_preserved_for_transparency() -> None:
    """Even when clamping fires, the unclamped value is preserved."""
    result = apply_pipeline(
        model_p=0.80,
        market_p=0.10,
        kelly_fraction_default=0.5,
        max_single_position_pct=0.05,
    )
    assert result.kelly_unclamped > 0.5  # raw was huge
    assert result.suggested_after_max_cap == 0.05  # clamped
