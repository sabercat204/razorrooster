"""T-SCAN-022 — candidate identification acceptance tests."""

from __future__ import annotations

from razor_rooster.signal_scanner.engines.candidates import (
    CandidateConfig,
    identify_candidate,
)


def test_strong_signal_identified_as_candidate() -> None:
    decision = identify_candidate(
        sector="public_health",
        log_odds_shift=1.2,
        signature_confidence=0.7,
        source_stale=False,
        no_update_applied=False,
    )
    assert decision.is_candidate is True
    assert decision.direction == "elevated"
    assert decision.rejection_reasons == ()


def test_negative_shift_marked_depressed() -> None:
    decision = identify_candidate(
        sector="public_health",
        log_odds_shift=-1.2,
        signature_confidence=0.7,
        source_stale=False,
        no_update_applied=False,
    )
    assert decision.is_candidate is True
    assert decision.direction == "depressed"


def test_below_threshold_not_candidate() -> None:
    decision = identify_candidate(
        sector="public_health",
        log_odds_shift=0.2,
        signature_confidence=0.7,
        source_stale=False,
        no_update_applied=False,
    )
    assert decision.is_candidate is False
    assert "below_threshold" in decision.rejection_reasons
    assert decision.direction is None


def test_low_confidence_blocks_candidate() -> None:
    """REQ-SCAN-CAND-003: confidence floor."""
    decision = identify_candidate(
        sector="public_health",
        log_odds_shift=2.0,
        signature_confidence=0.1,  # below floor
        source_stale=False,
        no_update_applied=False,
    )
    assert decision.is_candidate is False
    assert "low_signature_confidence" in decision.rejection_reasons


def test_none_confidence_blocks_candidate() -> None:
    decision = identify_candidate(
        sector="public_health",
        log_odds_shift=2.0,
        signature_confidence=None,
        source_stale=False,
        no_update_applied=False,
    )
    assert decision.is_candidate is False
    assert "low_signature_confidence" in decision.rejection_reasons


def test_stale_source_blocks_candidate_by_default() -> None:
    """REQ-SCAN-CAND-004: default disables stale-source candidates."""
    decision = identify_candidate(
        sector="public_health",
        log_odds_shift=2.0,
        signature_confidence=0.7,
        source_stale=True,
        no_update_applied=False,
    )
    assert decision.is_candidate is False
    assert "source_stale" in decision.rejection_reasons


def test_stale_source_eligible_when_configured() -> None:
    cfg = CandidateConfig(stale_source_eligible=True)
    decision = identify_candidate(
        sector="public_health",
        log_odds_shift=2.0,
        signature_confidence=0.7,
        source_stale=True,
        no_update_applied=False,
        config=cfg,
    )
    assert decision.is_candidate is True


def test_no_update_blocks_candidate() -> None:
    """REQ-SCAN-PROB-003 fallback: no candidate even on large shifts."""
    decision = identify_candidate(
        sector="public_health",
        log_odds_shift=3.0,
        signature_confidence=0.7,
        source_stale=False,
        no_update_applied=True,
    )
    assert decision.is_candidate is False
    assert "no_update_applied" in decision.rejection_reasons


def test_per_sector_threshold_respected() -> None:
    cfg = CandidateConfig(
        log_odds_shift_min=0.5,
        per_sector_threshold={"geopolitical": 1.0},  # higher bar
    )
    # 0.7 shift in geopolitical: under per-sector threshold (1.0); not candidate.
    decision = identify_candidate(
        sector="geopolitical",
        log_odds_shift=0.7,
        signature_confidence=0.7,
        source_stale=False,
        no_update_applied=False,
        config=cfg,
    )
    assert decision.is_candidate is False
    # 0.7 in public_health (uses default 0.5): is candidate.
    decision = identify_candidate(
        sector="public_health",
        log_odds_shift=0.7,
        signature_confidence=0.7,
        source_stale=False,
        no_update_applied=False,
        config=cfg,
    )
    assert decision.is_candidate is True


def test_zero_shift_no_direction() -> None:
    decision = identify_candidate(
        sector="public_health",
        log_odds_shift=0.0,
        signature_confidence=0.7,
        source_stale=False,
        no_update_applied=False,
    )
    assert decision.is_candidate is False
    # direction is None when there's no actual shift
    assert decision.direction is None


def test_multiple_rejection_reasons_accumulate() -> None:
    decision = identify_candidate(
        sector="public_health",
        log_odds_shift=0.1,
        signature_confidence=0.1,
        source_stale=True,
        no_update_applied=False,
    )
    assert decision.is_candidate is False
    assert "below_threshold" in decision.rejection_reasons
    assert "low_signature_confidence" in decision.rejection_reasons
    assert "source_stale" in decision.rejection_reasons
