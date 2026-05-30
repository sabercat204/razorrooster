"""Surfacing logic (T-MD-032; design §3.6 + OQ-MD-004 resolution).

Apply the gates from REQ-MD-CMP-008. A comparison is surfaced only
when ALL of the following hold:

- ``|log_odds_delta|`` exceeds the per-sector threshold.
- ``ci_overlap = False`` (the model and market are not in material
  agreement at current uncertainty levels).
- No critical warnings set:
  ``stale_market_price``, ``no_market_price``, ``low_mapping_confidence``,
  ``low_signature_confidence``, ``library_stale_warning``.
- Mapping confidence is ``'exact'`` or ``'inferred'`` (not ``'low'``).

Otherwise the comparison is persisted but not surfaced. The
``suppression_reasons`` list explains why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from razor_rooster.mispricing_detector.models import MappingConfidence

_DEFAULT_LOG_ODDS_THRESHOLD: Final[float] = 0.5


@dataclass(frozen=True, slots=True)
class SurfacingConfig:
    """Tunable knobs for surfacing decisions (config/mispricing.yaml)."""

    log_odds_delta_min: float = _DEFAULT_LOG_ODDS_THRESHOLD
    per_sector_threshold: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class SurfacingDecision:
    """Decision outcome for one comparison."""

    surfaced: bool
    suppression_reasons: tuple[str, ...]


def surfacing_decision(
    *,
    sector: str,
    log_odds_delta: float | None,
    ci_overlap: bool,
    mapping_confidence: MappingConfidence,
    low_signature_confidence: bool,
    library_stale_warning: bool,
    stale_market_price: bool,
    no_market_price: bool,
    low_liquidity: bool,
    config: SurfacingConfig | None = None,
) -> SurfacingDecision:
    """Apply the five gates and return the typed decision."""
    cfg = config or SurfacingConfig()
    threshold = _resolve_threshold(sector, cfg)

    reasons: list[str] = []
    if log_odds_delta is None:
        reasons.append("no_market_price")
    elif abs(log_odds_delta) < threshold:
        reasons.append("delta_below_threshold")
    if ci_overlap:
        reasons.append("ci_overlap")
    if low_signature_confidence:
        reasons.append("low_signature_confidence")
    if library_stale_warning:
        reasons.append("library_stale_warning")
    if stale_market_price:
        reasons.append("stale_market_price")
    if no_market_price and "no_market_price" not in reasons:
        reasons.append("no_market_price")
    if low_liquidity:
        reasons.append("low_liquidity")
    if mapping_confidence == "low":
        reasons.append("low_mapping_confidence")

    surfaced = not reasons
    # Deduplicate reasons while preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for r in reasons:
        if r not in seen:
            uniq.append(r)
            seen.add(r)
    return SurfacingDecision(surfaced=surfaced, suppression_reasons=tuple(uniq))


def confidence_weighted_score(
    *,
    log_odds_delta: float | None,
    signature_confidence: float | None,
    market_volume_24h: float | None,
    liquidity_floor: float | None,
) -> float | None:
    """Heuristic ranking score: ``|delta| * confidence * (1 - liquidity_penalty)``.

    Returns None when ``log_odds_delta`` is None (no market price) so
    callers can persist NULL.
    """
    if log_odds_delta is None:
        return None
    confidence = signature_confidence if signature_confidence is not None else 0.0
    if liquidity_floor is None or liquidity_floor <= 0:
        liquidity_penalty = 0.0
    else:
        volume = market_volume_24h or 0.0
        ratio = max(0.0, min(1.0, volume / liquidity_floor))
        liquidity_penalty = 1.0 - ratio
    return float(abs(log_odds_delta) * confidence * (1.0 - liquidity_penalty))


# -- internals --------------------------------------------------------------


def _resolve_threshold(sector: str, cfg: SurfacingConfig) -> float:
    if cfg.per_sector_threshold is not None and sector in cfg.per_sector_threshold:
        return float(cfg.per_sector_threshold[sector])
    return float(cfg.log_odds_delta_min)
