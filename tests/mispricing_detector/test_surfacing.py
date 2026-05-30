"""T-MD-032 — surfacing logic tests."""

from __future__ import annotations

import pytest

from razor_rooster.mispricing_detector.engines.surfacing import (
    SurfacingConfig,
    confidence_weighted_score,
    surfacing_decision,
)


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sector": "public_health",
        "log_odds_delta": 1.0,
        "ci_overlap": False,
        "mapping_confidence": "exact",
        "low_signature_confidence": False,
        "library_stale_warning": False,
        "stale_market_price": False,
        "no_market_price": False,
        "low_liquidity": False,
    }
    base.update(overrides)
    return base


def test_strong_signal_surfaces() -> None:
    decision = surfacing_decision(**_kwargs())  # type: ignore[arg-type]
    assert decision.surfaced is True
    assert decision.suppression_reasons == ()


def test_below_threshold_suppressed() -> None:
    decision = surfacing_decision(**_kwargs(log_odds_delta=0.2))  # type: ignore[arg-type]
    assert decision.surfaced is False
    assert "delta_below_threshold" in decision.suppression_reasons


def test_ci_overlap_suppresses() -> None:
    decision = surfacing_decision(**_kwargs(ci_overlap=True))  # type: ignore[arg-type]
    assert decision.surfaced is False
    assert "ci_overlap" in decision.suppression_reasons


def test_low_mapping_confidence_suppresses() -> None:
    decision = surfacing_decision(**_kwargs(mapping_confidence="low"))  # type: ignore[arg-type]
    assert decision.surfaced is False
    assert "low_mapping_confidence" in decision.suppression_reasons


def test_low_signature_confidence_suppresses() -> None:
    decision = surfacing_decision(  # type: ignore[arg-type]
        **_kwargs(low_signature_confidence=True)
    )
    assert decision.surfaced is False
    assert "low_signature_confidence" in decision.suppression_reasons


def test_stale_market_price_suppresses() -> None:
    decision = surfacing_decision(**_kwargs(stale_market_price=True))  # type: ignore[arg-type]
    assert decision.surfaced is False
    assert "stale_market_price" in decision.suppression_reasons


def test_no_market_price_suppresses() -> None:
    decision = surfacing_decision(  # type: ignore[arg-type]
        **_kwargs(log_odds_delta=None, no_market_price=True)
    )
    assert decision.surfaced is False
    assert "no_market_price" in decision.suppression_reasons


def test_library_stale_suppresses() -> None:
    decision = surfacing_decision(**_kwargs(library_stale_warning=True))  # type: ignore[arg-type]
    assert decision.surfaced is False
    assert "library_stale_warning" in decision.suppression_reasons


def test_low_liquidity_suppresses() -> None:
    decision = surfacing_decision(**_kwargs(low_liquidity=True))  # type: ignore[arg-type]
    assert decision.surfaced is False
    assert "low_liquidity" in decision.suppression_reasons


def test_per_sector_threshold_respected() -> None:
    cfg = SurfacingConfig(log_odds_delta_min=0.5, per_sector_threshold={"geopolitical": 1.0})
    # 0.7 in geopolitical (per-sector 1.0): suppressed.
    decision = surfacing_decision(  # type: ignore[arg-type]
        **_kwargs(sector="geopolitical", log_odds_delta=0.7), config=cfg
    )
    assert decision.surfaced is False
    # 0.7 in public_health (default 0.5): surfaced.
    decision = surfacing_decision(  # type: ignore[arg-type]
        **_kwargs(sector="public_health", log_odds_delta=0.7), config=cfg
    )
    assert decision.surfaced is True


def test_multiple_suppressions_listed() -> None:
    decision = surfacing_decision(  # type: ignore[arg-type]
        **_kwargs(
            ci_overlap=True,
            low_signature_confidence=True,
            mapping_confidence="low",
        )
    )
    assert decision.surfaced is False
    assert "ci_overlap" in decision.suppression_reasons
    assert "low_signature_confidence" in decision.suppression_reasons
    assert "low_mapping_confidence" in decision.suppression_reasons


def test_confidence_weighted_score_basic() -> None:
    score = confidence_weighted_score(
        log_odds_delta=1.0,
        signature_confidence=0.8,
        market_volume_24h=20000.0,
        liquidity_floor=10000.0,
    )
    # Volume above floor -> liquidity_penalty=0; score = 1.0 * 0.8 * 1.0 = 0.8.
    assert score == pytest.approx(0.8)


def test_confidence_weighted_score_low_volume_penalty() -> None:
    score = confidence_weighted_score(
        log_odds_delta=1.0,
        signature_confidence=0.8,
        market_volume_24h=2500.0,
        liquidity_floor=10000.0,
    )
    # 25% of floor -> liquidity_penalty=0.75 -> score = 1.0 * 0.8 * 0.25 = 0.2.
    assert score == pytest.approx(0.2)


def test_confidence_weighted_score_zero_volume() -> None:
    score = confidence_weighted_score(
        log_odds_delta=1.0,
        signature_confidence=0.8,
        market_volume_24h=0.0,
        liquidity_floor=10000.0,
    )
    assert score == pytest.approx(0.0)


def test_confidence_weighted_score_no_floor() -> None:
    score = confidence_weighted_score(
        log_odds_delta=1.0,
        signature_confidence=0.8,
        market_volume_24h=0.0,
        liquidity_floor=None,
    )
    # Without floor, no penalty.
    assert score == pytest.approx(0.8)


def test_confidence_weighted_score_no_market_returns_none() -> None:
    score = confidence_weighted_score(
        log_odds_delta=None,
        signature_confidence=0.8,
        market_volume_24h=20000.0,
        liquidity_floor=10000.0,
    )
    assert score is None
