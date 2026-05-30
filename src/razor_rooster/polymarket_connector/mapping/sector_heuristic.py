"""Sector heuristic mapper (T-PMC-050; design §3.5; OQ-PMC-001 resolution).

Classifies a Polymarket market into one of the six Razor-Rooster sectors
by counting keyword hits across the market's question, description, and
tags. The keyword catalogue lives in ``config/sector_keywords.yaml``
(loaded via T-PMC-002) so operators can tune it without code changes.

The mapper is intentionally conservative: ties produce
``razor_sector=None`` so downstream subsystems can route the market to
the operator review queue rather than acting on a low-confidence guess.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Final

from razor_rooster.polymarket_connector.client.gamma import GammaMarket
from razor_rooster.polymarket_connector.config.loader import (
    SectorKeywordsConfig,
)

logger = logging.getLogger(__name__)


# Heuristic version recorded with each mapping so downstream consumers
# can detect when the heuristic has changed materially. Bump when the
# keyword-matching logic itself changes (the keyword catalogue is data
# and tracked separately by config version).
HEURISTIC_VERSION: Final[int] = 1
HEURISTIC_TAG: Final[str] = f"heuristic_v{HEURISTIC_VERSION}"

# Confidence values in polymarket_sector_mapping. 'inferred' means the
# mapping came from this heuristic; 'manual' is operator override
# (T-PMC-051); 'exact' is reserved for a future curated mapping table
# if Polymarket ever publishes one we trust.
INFERRED_CONFIDENCE: Final[str] = "inferred"


@dataclass(frozen=True, slots=True)
class SectorMapping:
    """Result of one heuristic classification.

    Attributes:
        razor_sector: The primary Razor sector, or ``None`` if the
            mapper couldn't break a tie or no keyword fired.
        secondary_sectors: Other sectors that scored above zero. Empty
            for unambiguous classifications.
        confidence: One of 'inferred' | 'manual' | 'exact'. The
            heuristic always emits 'inferred'.
        scores: Per-sector hit counts. Useful for operator triage and
            for surfacing why a mapping landed where it did.
        mapped_by: Identifier of the mapping source (e.g.
            'heuristic_v1' or 'operator').
    """

    razor_sector: str | None
    secondary_sectors: tuple[str, ...]
    confidence: str
    scores: dict[str, int] = field(default_factory=dict)
    mapped_by: str = HEURISTIC_TAG


def map_sector(
    market: GammaMarket,
    *,
    keywords: SectorKeywordsConfig,
) -> SectorMapping:
    """Classify a market into a Razor sector via keyword scoring.

    Behavior:

    - Concatenates question, description, and tag strings into one
      lowercase corpus.
    - For each sector in ``keywords.sectors``, counts how many of its
      keywords appear in the corpus (case-insensitive whole-token /
      phrase match using a word-boundary regex).
    - Picks the sector with the highest score. Ties (two or more
      sectors with the same top score) → ``razor_sector=None``.
    - Sectors with score > 0 that didn't win become
      ``secondary_sectors``.
    - All-zero scores → ``razor_sector=None`` with empty
      ``secondary_sectors``.

    Args:
        market: The market to classify. Only the parsed surface fields
            and the verbatim raw payload are consulted; the database
            isn't touched.
        keywords: The loaded keyword catalogue.

    Returns:
        :class:`SectorMapping`. Always returns; never raises.
    """
    corpus = _build_corpus(market)
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
        # Ambiguous — operator review.
        secondary = tuple(
            sorted(s for s, count in scores.items() if count > 0 and s not in top_sectors)
        )
        logger.debug(
            "Polymarket sector heuristic ambiguous for market %s: %s",
            market.condition_id,
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


def _build_corpus(market: GammaMarket) -> str:
    """Concatenate the market's text-bearing fields into a lowercase corpus."""
    parts: list[str] = []
    if market.question:
        parts.append(market.question)
    description = market.raw.get("description")
    if isinstance(description, str) and description:
        parts.append(description)
    tags = market.raw.get("tags")
    if isinstance(tags, list):
        parts.extend(str(t) for t in tags if t)
    elif isinstance(tags, str) and tags:
        parts.append(tags)
    category = market.raw.get("category")
    if isinstance(category, str) and category:
        parts.append(category)
    subcategory = market.raw.get("subcategory")
    if isinstance(subcategory, str) and subcategory:
        parts.append(subcategory)
    return " ".join(parts).lower()


def _count_hits(corpus: str, keywords: list[str]) -> int:
    """Count distinct keyword matches in the corpus.

    Each unique keyword that appears at least once contributes 1 to the
    score. Repeated occurrences of the same keyword don't inflate the
    score — the heuristic asks "is this market about X" not "how often
    does X get mentioned." Keyword matching is case-insensitive and
    uses word-boundary regex so 'oil' doesn't match 'foil' or 'broil'.
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
    """Build a case-insensitive whole-word regex for a keyword phrase.

    Multi-word keywords ("executive order") match as a phrase with
    flexible whitespace between words. Single tokens use \\b boundaries
    so we don't match substrings.
    """
    tokens = keyword.split()
    if not tokens:
        return re.compile("(?!)")  # never matches
    if len(tokens) == 1:
        return re.compile(rf"\b{re.escape(tokens[0])}\b", re.IGNORECASE)
    escaped_tokens = [re.escape(t) for t in tokens]
    pattern_str = r"\b" + r"\s+".join(escaped_tokens) + r"\b"
    return re.compile(pattern_str, re.IGNORECASE)
