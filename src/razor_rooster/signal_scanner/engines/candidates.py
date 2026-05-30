"""Candidate identification (T-SCAN-022; design §3.7).

Apply the candidate-identification gates and return a typed decision.
The five gates from REQ-SCAN-CAND-001..004:

1. Magnitude. Absolute log-odds shift must exceed the per-sector
   threshold from config (default 0.5).
2. Confidence. Signature confidence must be at or above the floor
   (default 0.3).
3. Stale-source eligibility. When source_stale_warning is set and
   ``stale_source_eligible_for_candidate`` is False (the default),
   the record is not eligible.
4. Definition-drift. Drift warnings do not automatically disqualify;
   the operator sees the warning in the trace.
5. No-update fallback. Records where ``no_update_applied`` is True
   (REQ-SCAN-PROB-003) cannot be candidates regardless of magnitude.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Default thresholds embed design §3.7 defaults so the engine works
# without a config file present (tests don't have to seed one).
_DEFAULT_LOG_ODDS_THRESHOLD: Final[float] = 0.5
_DEFAULT_CONFIDENCE_FLOOR: Final[float] = 0.3


@dataclass(frozen=True, slots=True)
class CandidateConfig:
    """Tunable knobs for candidate identification (config/scanner.yaml)."""

    log_odds_shift_min: float = _DEFAULT_LOG_ODDS_THRESHOLD
    per_sector_threshold: dict[str, float] | None = None
    confidence_floor: float = _DEFAULT_CONFIDENCE_FLOOR
    stale_source_eligible: bool = False


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    """Decision outcome for one record."""

    is_candidate: bool
    direction: str | None  # 'elevated' | 'depressed' | None
    rejection_reasons: tuple[str, ...] = ()


def identify_candidate(
    *,
    sector: str,
    log_odds_shift: float,
    signature_confidence: float | None,
    source_stale: bool,
    no_update_applied: bool,
    config: CandidateConfig | None = None,
) -> CandidateDecision:
    """Apply the candidate-identification gates and return a typed decision."""
    cfg = config or CandidateConfig()
    threshold = _resolve_threshold(sector, cfg)
    direction = _direction(log_odds_shift)

    reasons: list[str] = []
    if no_update_applied:
        reasons.append("no_update_applied")
    if abs(log_odds_shift) < threshold:
        reasons.append("below_threshold")
    if signature_confidence is None or signature_confidence < cfg.confidence_floor:
        reasons.append("low_signature_confidence")
    if source_stale and not cfg.stale_source_eligible:
        reasons.append("source_stale")

    is_candidate = not reasons
    return CandidateDecision(
        is_candidate=is_candidate,
        direction=direction if is_candidate else None,
        rejection_reasons=tuple(reasons),
    )


def _resolve_threshold(sector: str, cfg: CandidateConfig) -> float:
    if cfg.per_sector_threshold is not None and sector in cfg.per_sector_threshold:
        return float(cfg.per_sector_threshold[sector])
    return float(cfg.log_odds_shift_min)


def _direction(log_odds_shift: float) -> str | None:
    if log_odds_shift > 0.0:
        return "elevated"
    if log_odds_shift < 0.0:
        return "depressed"
    return None
