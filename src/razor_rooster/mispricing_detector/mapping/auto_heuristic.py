"""Auto-mapping confidence heuristic (T-MD-021; OQ-MD-001 resolution).

Decides whether a (class, market) pair gets an auto-derived mapping
and at what confidence level. The contract:

- Returns ``None`` when no auto-mapping should be created (sector
  mismatch).
- Returns ``'inferred'`` when the sector matches AND keyword overlap
  meets the configured minimum AND a temporal qualifier consistent
  with the class's resolution semantics is present in the market
  question.
- Returns ``'low'`` when the sector matches but the keyword/temporal
  conditions don't reach the inferred bar.

Anything weaker than 'low' is not auto-mapped. The operator's curated
mappings always take precedence and are never overwritten by this
heuristic.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from razor_rooster.mispricing_detector.models import MappingConfidence
from razor_rooster.pattern_library.models.event_class import EventClass, Sector

# Stop words pruned from keyword overlap. Kept short and inclusive.
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "over",
        "than",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "when",
        "with",
        "will",
        "shall",
        "may",
        "might",
        "would",
        "could",
        "should",
        "do",
        "does",
        "did",
        "not",
        "no",
        "yes",
        "their",
        "they",
        "them",
        "we",
        "us",
        "our",
        "i",
        "you",
        "your",
        "his",
        "her",
    }
)


# Temporal qualifier regex catalogue. Matches phrases like "in 2026",
# "by year-end", "before 2027", "this year", "by Q4 2026".
_TEMPORAL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bin\s+\d{4}\b", re.IGNORECASE),
    re.compile(r"\bby\s+\d{4}\b", re.IGNORECASE),
    re.compile(r"\bbefore\s+\d{4}\b", re.IGNORECASE),
    re.compile(r"\b(this|next|last)\s+(year|month|quarter)\b", re.IGNORECASE),
    re.compile(r"\bby\s+year[-\s]end\b", re.IGNORECASE),
    re.compile(r"\bq[1-4]\s+\d{4}\b", re.IGNORECASE),
    re.compile(r"\bin\s+the\s+next\s+\d+\s+(months|years|days)\b", re.IGNORECASE),
    re.compile(r"\bwithin\s+\d+\s+(months|years|days)\b", re.IGNORECASE),
    re.compile(r"\bby\s+(?:end\s+of\s+)?\d{4}\b", re.IGNORECASE),
)


# Sectors that match exactly between class and market.
def _sectors_match(class_sector: Sector, market_sector: str | None) -> bool:
    if market_sector is None:
        return False
    if class_sector == Sector.CROSS_CUTTING:
        # Cross-cutting classes don't auto-map; require explicit operator action.
        return False
    return class_sector.value == market_sector


def _tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, drop stopwords."""
    if not text:
        return set()
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
    tokens = {tok for tok in cleaned.split() if tok and tok not in _STOPWORDS and len(tok) > 2}
    # Stem trailing 's' for naive plural collapse.
    return {(tok[:-1] if tok.endswith("s") and len(tok) > 3 else tok) for tok in tokens}


def keyword_overlap(class_text: str, market_text: str) -> int:
    """Count tokens shared by the two texts after tokenization."""
    return len(_tokenize(class_text) & _tokenize(market_text))


def has_temporal_qualifier(market_text: str) -> bool:
    """True when the market question contains a recognized temporal phrase."""
    return any(pat.search(market_text) for pat in _TEMPORAL_PATTERNS)


@dataclass(frozen=True, slots=True)
class MarketSummary:
    """Lightweight projection of a Polymarket market for the heuristic."""

    condition_id: str
    razor_sector: str | None
    question: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class HeuristicConfig:
    """Tunable knobs for the heuristic (config/mispricing.yaml)."""

    min_keyword_overlap_for_inferred: int = 3
    require_temporal_qualifier_for_inferred: bool = True


def confidence(
    cls: EventClass,
    market: MarketSummary,
    *,
    config: HeuristicConfig | None = None,
) -> MappingConfidence | None:
    """Compute the auto-mapping confidence for a (class, market) pair."""
    cfg = config or HeuristicConfig()
    if not _sectors_match(cls.domain_sector, market.razor_sector):
        return None

    market_text = market.question + " " + (market.description or "")
    class_text = cls.title + " " + cls.description
    overlap = keyword_overlap(class_text, market_text)
    temporal = has_temporal_qualifier(market.question)

    enough_overlap = overlap >= cfg.min_keyword_overlap_for_inferred
    if cfg.require_temporal_qualifier_for_inferred:
        if enough_overlap and temporal:
            return "inferred"
    elif enough_overlap:
        return "inferred"

    return "low"


__all__ = [
    "HeuristicConfig",
    "MarketSummary",
    "confidence",
    "has_temporal_qualifier",
    "keyword_overlap",
]


# Reserved imports.
_RESERVED: tuple[object, ...] = (Iterable, timedelta)
