"""T-PE-031 — bankroll-survival tests."""

from __future__ import annotations

import pytest

from razor_rooster.position_engine.engines.bankroll import compute_survival


def test_zero_fraction_no_loss() -> None:
    survival = compute_survival(0.0, scenarios=(1, 3, 5))
    assert survival == {1: 1.0, 3: 1.0, 5: 1.0}


def test_default_scenarios() -> None:
    survival = compute_survival(0.05)
    assert set(survival.keys()) == {1, 3, 5}


def test_5pct_fraction_5_losses() -> None:
    survival = compute_survival(0.05, scenarios=(1, 3, 5))
    assert survival[1] == pytest.approx(0.95)
    assert survival[3] == pytest.approx(0.95**3, abs=0.0001)
    assert survival[5] == pytest.approx(0.95**5, abs=0.0001)


def test_50pct_fraction_severe_decay() -> None:
    """50% of bankroll per round means after 5 losses you're at ~3% bankroll."""
    survival = compute_survival(0.50, scenarios=(1, 3, 5))
    assert survival[5] == pytest.approx(0.5**5, abs=0.0001)


def test_negative_fraction_clamped_to_zero() -> None:
    """A negative suggested_fraction is treated as zero loss."""
    survival = compute_survival(-0.1, scenarios=(1, 3, 5))
    assert survival[5] == 1.0


def test_above_one_fraction_clamped() -> None:
    """A fraction above 1 means losing more than bankroll; clamped to 1."""
    survival = compute_survival(1.5, scenarios=(1, 3, 5))
    assert survival[1] == 0.0
    assert survival[5] == 0.0
