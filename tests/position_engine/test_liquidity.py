"""T-PE-032 — liquidity-feasibility tests."""

from __future__ import annotations

import math

import pytest

from razor_rooster.position_engine.engines.liquidity import compute_liquidity


def test_below_threshold_no_clamp() -> None:
    result = compute_liquidity(
        suggested_fraction=0.05,
        bankroll_usd=1000.0,
        volume_24h=10000.0,
        threshold_pct_of_volume=0.05,
    )
    # 5% of 1000 = $50; market volume $10k; 50/10000 = 0.5% of volume.
    assert result.pct_of_24h_volume == pytest.approx(0.005)
    assert result.clamped is False
    assert result.low_liquidity_flag is False
    assert result.suggested_fraction_after_clamp == pytest.approx(0.05)


def test_above_threshold_clamps() -> None:
    """Suggested $200 vs $1000 volume = 20% of volume, above 5% threshold."""
    result = compute_liquidity(
        suggested_fraction=0.20,
        bankroll_usd=1000.0,
        volume_24h=1000.0,
        threshold_pct_of_volume=0.05,
    )
    assert result.clamped is True
    assert result.low_liquidity_flag is True
    # Clamped to 5% of $1000 = $50; back-derived fraction = $50/$1000 = 0.05.
    assert result.suggested_dollar_size_after_clamp == pytest.approx(50.0)
    assert result.suggested_fraction_after_clamp == pytest.approx(0.05)


def test_zero_volume_clamps_to_zero() -> None:
    result = compute_liquidity(
        suggested_fraction=0.05,
        bankroll_usd=1000.0,
        volume_24h=0.0,
        threshold_pct_of_volume=0.05,
    )
    assert result.clamped is True
    assert result.low_liquidity_flag is True
    assert result.suggested_fraction_after_clamp == 0.0
    assert math.isinf(result.pct_of_24h_volume)


def test_null_volume_clamps_to_zero() -> None:
    result = compute_liquidity(
        suggested_fraction=0.05,
        bankroll_usd=1000.0,
        volume_24h=None,
        threshold_pct_of_volume=0.05,
    )
    assert result.clamped is True
    assert result.low_liquidity_flag is True
    assert result.suggested_fraction_after_clamp == 0.0


def test_zero_suggested_fraction_no_clamp() -> None:
    result = compute_liquidity(
        suggested_fraction=0.0,
        bankroll_usd=1000.0,
        volume_24h=10000.0,
        threshold_pct_of_volume=0.05,
    )
    assert result.clamped is False
    assert result.suggested_fraction_after_clamp == 0.0
