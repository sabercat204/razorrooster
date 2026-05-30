"""Kalshi sector heuristic mapper (T-KSI-050; design §3.5; OQ-KSI-001 resolution).

Mirrors :mod:`razor_rooster.polymarket_connector.mapping.sector_heuristic`
with two Kalshi-specific differences:

1. The mapper consults the market's title, sub_title, and category
   plus the parent series' category and tags. Polymarket's mapper
   reads only market-level fields; Kalshi's series-level metadata is
   richer than Polymarket's, so we pull it in.
2. The output sector enum includes ``'out_of_scope'``: a deliberate
   bucket for sports / entertainment / daily-life markets that have
   no Razor-sector analogue. Categories matching known
   sports/entertainment names auto-classify as ``out_of_scope`` in
   Pass 1 before the keyword scan; this means a sports keyword on its
   own can't be misclassified as macro just because of a stray
   ticker-name match.

The mapper is intentionally conservative: ties produce
``razor_sector=None`` so downstream subsystems can route the market to
the operator review queue.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Final

from razor_rooster.kalshi_connector.client.models import (
    KalshiMarket,
    KalshiSeries,
)
from razor_rooster.kalshi_connector.config.loader import (
    KalshiSectorKeywordsConfig,
)

logger = logging.getLogger(__name__)


# Bump when the keyword-matching logic itself changes. The keyword
# catalogue is data and tracked separately by the config's ``version``.
HEURISTIC_VERSION: Final[int] = 1
HEURISTIC_TAG: Final[str] = f"kalshi_heuristic_v{HEURISTIC_VERSION}"


# Confidence levels in ``kalshi_sector_mapping.confidence``. Same set
# as Polymarket so report and CLI surfaces share vocabulary.
INFERRED_CONFIDENCE: Final[str] = "inferred"
MANUAL_CONFIDENCE: Final[str] = "manual"
EXACT_CONFIDENCE: Final[str] = "exact"


# Category names (case-insensitive) that auto-classify a market as
# out_of_scope without consulting the keyword scan. The list is
# deliberately narrow to avoid misclassifying a Kalshi macro market
# whose category happens to mention "sports" tangentially. Operators
# who find a missing entertainment/daily-life category that should
# also auto-classify can extend this list — but the keyword scan path
# is the more flexible mechanism.
_OUT_OF_SCOPE_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "sports",
        "entertainment",
        "pop culture",
        "daily life",
        "sports - daily",
    }
)

# Sentinel value: when the Pass-1 category match fires, we mark the
# entire ``out_of_scope`` bucket as the only winner with this score.
_CATEGORY_HIT_SCORE: Final[int] = 9999


@dataclass(frozen=True, slots=True)
class SectorMapping:
    """Result of one Kalshi heuristic classification.

    Attributes:
        razor_sector: The primary Razor sector or ``None`` when the
            mapper couldn't break a tie or no keyword fired. The value
            ``'out_of_scope'`` is a Kalshi-specific bucket.
        secondary_sectors: Other sectors that scored above zero.
        confidence: One of 'inferred' | 'manual' | 'exact'. The
            heuristic always emits 'inferred'.
        scores: Per-sector hit counts. Useful for triage.
        mapped_by: Identifier of the mapping source (``'kalshi_heuristic_v1'``
            here; ``'operator'`` for manual overrides).
    """

    razor_sector: str | None
    secondary_sectors: tuple[str, ...]
    confidence: str
    scores: dict[str, int] = field(default_factory=dict)
    mapped_by: str = HEURISTIC_TAG


def map_sector(
    market: KalshiMarket,
    *,
    keywords: KalshiSectorKeywordsConfig,
    series: KalshiSeries | None = None,
) -> SectorMapping:
    """Classify a Kalshi market into a Razor sector.

    Three-pass logic:

    - **Pass 1**: exact category-name match. If the market's category
      (or the parent series' category) is in
      :data:`_OUT_OF_SCOPE_CATEGORIES`, return ``out_of_scope`` immediately.
    - **Pass 2**: keyword scan over title, sub_title, category, and the
      series' title and tags against per-sector keyword sets. Highest
      hit count wins; tie returns ``None``.
    - **Pass 3**: when both top sectors have score 0 (no keyword fired),
      return ``None``.
    """
    # Pass 1: category auto-classification.
    if _is_out_of_scope_category(market, series):
        return SectorMapping(
            razor_sector="out_of_scope",
            secondary_sectors=(),
            confidence=INFERRED_CONFIDENCE,
            scores={"out_of_scope": _CATEGORY_HIT_SCORE},
        )

    corpus = _build_corpus(market, series)
    scores: dict[str, int] = {}
    for sector_name, sector_keywords in keywords.sectors.items():
        scores[sector_name] = _count_hits(corpus, sector_keywords)

    if not any(scores.values()):
        return SectorMapping(
            razor_sector=None,
            secondary_sectors=(),
            confidence=INFERRED_CONFIDENCE,
            scores=scores,
        )

    top_score = max(scores.values())
    top_sectors = [s for s, count in scores.items() if count == top_score]

    if len(top_sectors) > 1:
        secondary = tuple(
            sorted(s for s, count in scores.items() if count > 0 and s not in top_sectors)
        )
        logger.debug(
            "Kalshi sector heuristic ambiguous for ticker %s: %s",
            market.ticker,
            scores,
        )
        return SectorMapping(
            razor_sector=None,
            secondary_sectors=tuple(sorted(top_sectors)) + secondary,
            confidence=INFERRED_CONFIDENCE,
            scores=scores,
        )

    primary = top_sectors[0]
    secondary = tuple(sorted(s for s, count in scores.items() if count > 0 and s != primary))
    return SectorMapping(
        razor_sector=primary,
        secondary_sectors=secondary,
        confidence=INFERRED_CONFIDENCE,
        scores=scores,
    )


# -- internals --------------------------------------------------------------


def _is_out_of_scope_category(market: KalshiMarket, series: KalshiSeries | None) -> bool:
    """Return True iff either the market or its series category lands in
    :data:`_OUT_OF_SCOPE_CATEGORIES`.
    """
    candidates: list[str] = []
    if market.category:
        candidates.append(market.category.strip().lower())
    if series is not None and series.category:
        candidates.append(series.category.strip().lower())
    return any(c in _OUT_OF_SCOPE_CATEGORIES for c in candidates)


def _build_corpus(market: KalshiMarket, series: KalshiSeries | None) -> str:
    """Concatenate the market + series text fields into a lowercase corpus."""
    parts: list[str] = []
    if market.title:
        parts.append(market.title)
    if market.sub_title:
        parts.append(market.sub_title)
    if market.category:
        parts.append(market.category)
    if market.yes_sub_title:
        parts.append(market.yes_sub_title)
    if market.no_sub_title:
        parts.append(market.no_sub_title)
    if series is not None:
        if series.title:
            parts.append(series.title)
        if series.category:
            parts.append(series.category)
        if series.tags:
            parts.extend(series.tags)
    return " ".join(parts).lower()


def _count_hits(corpus: str, keywords: list[str]) -> int:
    """Count distinct keyword matches in the corpus.

    Each unique keyword that appears at least once contributes 1 to the
    score. Repeated occurrences don't inflate the score — the heuristic
    asks "is this market about X" not "how often does X get
    mentioned." Matching is case-insensitive and uses word boundaries
    so 'oil' doesn't match 'foil' or 'broil'.
    """
    if not corpus or not keywords:
        return 0
    hits = 0
    for keyword in keywords:
        normalized = keyword.lower().strip()
        if not normalized:
            continue
        pattern = _build_keyword_pattern(normalized)
        if pattern.search(corpus):
            hits += 1
    return hits


def _build_keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Build a case-insensitive whole-word regex for a keyword phrase."""
    tokens = keyword.split()
    if not tokens:
        return re.compile("(?!)")  # never matches
    if len(tokens) == 1:
        return re.compile(rf"\b{re.escape(tokens[0])}\b", re.IGNORECASE)
    escaped_tokens = [re.escape(t) for t in tokens]
    pattern_str = r"\b" + r"\s+".join(escaped_tokens) + r"\b"
    return re.compile(pattern_str, re.IGNORECASE)


__all__ = [
    "EXACT_CONFIDENCE",
    "HEURISTIC_TAG",
    "HEURISTIC_VERSION",
    "INFERRED_CONFIDENCE",
    "MANUAL_CONFIDENCE",
    "SectorMapping",
    "map_sector",
]
