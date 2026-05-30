"""T-PMC-050 — sector heuristic mapper tests."""

from __future__ import annotations

from typing import Any

import pytest

from razor_rooster.polymarket_connector.client.gamma import GammaMarket
from razor_rooster.polymarket_connector.config.loader import (
    SectorKeywordsConfig,
)
from razor_rooster.polymarket_connector.mapping.sector_heuristic import (
    HEURISTIC_TAG,
    INFERRED_CONFIDENCE,
    SectorMapping,
    map_sector,
)


@pytest.fixture
def keywords() -> SectorKeywordsConfig:
    """Compact six-sector keyword set for deterministic tests."""
    return SectorKeywordsConfig(
        version=1,
        sectors={
            "public_health": ["pandemic", "vaccine", "WHO", "outbreak"],
            "geopolitical": ["election", "war", "ceasefire", "treaty"],
            "regulatory": ["FDA", "Congress", "executive order", "Supreme Court"],
            "commodity": ["oil", "OPEC", "wheat"],
            "climate": ["hurricane", "drought", "ENSO"],
            "infrastructure_energy": ["grid", "blackout", "pipeline"],
        },
    )


def _make_market(
    *,
    condition_id: str = "0xabc",
    question: str = "",
    description: str | None = None,
    tags: list[str] | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    extra: dict[str, Any] | None = None,
) -> GammaMarket:
    raw: dict[str, Any] = {"conditionId": condition_id, "question": question}
    if description is not None:
        raw["description"] = description
    if tags is not None:
        raw["tags"] = tags
    if category is not None:
        raw["category"] = category
    if subcategory is not None:
        raw["subcategory"] = subcategory
    if extra:
        raw.update(extra)
    return GammaMarket(
        condition_id=condition_id,
        slug="m",
        question=question,
        active=True,
        closed=False,
        raw=raw,
    )


# -- happy paths per sector -----------------------------------------------
def test_classifies_public_health(keywords: SectorKeywordsConfig) -> None:
    market = _make_market(question="Will the WHO declare a pandemic in 2026?")
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector == "public_health"
    assert mapping.confidence == INFERRED_CONFIDENCE
    assert mapping.mapped_by == HEURISTIC_TAG


def test_classifies_geopolitical(keywords: SectorKeywordsConfig) -> None:
    market = _make_market(question="Will there be a ceasefire before the election?")
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector == "geopolitical"


def test_classifies_regulatory(keywords: SectorKeywordsConfig) -> None:
    market = _make_market(
        question="Will Congress pass the bill?",
        description="The Supreme Court has weighed in.",
    )
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector == "regulatory"


def test_classifies_commodity(keywords: SectorKeywordsConfig) -> None:
    market = _make_market(question="Will OPEC cut oil output?")
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector == "commodity"


def test_classifies_climate(keywords: SectorKeywordsConfig) -> None:
    market = _make_market(question="Will a hurricane make landfall in Florida?")
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector == "climate"


def test_classifies_infrastructure_energy(keywords: SectorKeywordsConfig) -> None:
    market = _make_market(question="Will a major pipeline outage hit the grid?")
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector == "infrastructure_energy"


# -- edge cases ----------------------------------------------------------
def test_no_keywords_match_returns_none(keywords: SectorKeywordsConfig) -> None:
    market = _make_market(question="Will the Lakers win the championship?")
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector is None
    assert mapping.secondary_sectors == ()


def test_tied_top_score_returns_none_with_secondary_set(
    keywords: SectorKeywordsConfig,
) -> None:
    """Tie between two sectors → razor_sector=None, both surface in secondary_sectors."""
    market = _make_market(
        question="Will OPEC declare a pandemic?",  # 'OPEC' (commodity) + 'pandemic' (public_health)
    )
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector is None
    # Both tied sectors should appear, sorted alphabetically.
    assert "commodity" in mapping.secondary_sectors
    assert "public_health" in mapping.secondary_sectors


def test_secondary_sectors_excluded_when_only_one_sector_hits(
    keywords: SectorKeywordsConfig,
) -> None:
    market = _make_market(question="Will the WHO call a pandemic?")
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector == "public_health"
    assert mapping.secondary_sectors == ()


def test_winner_with_secondary_below(keywords: SectorKeywordsConfig) -> None:
    """Public_health hits twice, commodity hits once → primary + secondary."""
    market = _make_market(question="Will the WHO declare a pandemic during an oil shortage?")
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector == "public_health"
    assert mapping.secondary_sectors == ("commodity",)


def test_repeated_keyword_does_not_inflate_score(
    keywords: SectorKeywordsConfig,
) -> None:
    """The heuristic counts distinct keyword matches, not occurrences."""
    market = _make_market(question="Pandemic pandemic pandemic. Vaccine!")
    mapping = map_sector(market, keywords=keywords)
    # 'pandemic' once + 'vaccine' once = score 2 for public_health.
    assert mapping.scores["public_health"] == 2


def test_word_boundary_avoids_substring_matches(
    keywords: SectorKeywordsConfig,
) -> None:
    """'oil' should not match 'foil' or 'broil'."""
    market = _make_market(question="Will the foil broil at the cookout?")
    mapping = map_sector(market, keywords=keywords)
    assert mapping.scores["commodity"] == 0
    assert mapping.razor_sector is None


def test_phrase_keyword_matches_with_flexible_whitespace(
    keywords: SectorKeywordsConfig,
) -> None:
    """'executive order' phrase matches 'executive   order' too."""
    market = _make_market(question="Will the executive   order survive review?")
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector == "regulatory"


def test_case_insensitive_match(keywords: SectorKeywordsConfig) -> None:
    market = _make_market(question="will the FDA approve the VACCINE?")
    mapping = map_sector(market, keywords=keywords)
    # 'FDA' (regulatory) and 'vaccine' (public_health) — tie.
    assert mapping.razor_sector is None


def test_corpus_includes_description_and_tags(keywords: SectorKeywordsConfig) -> None:
    market = _make_market(
        question="Generic question",
        description="Will the OPEC cartel meet?",
        tags=["energy"],
    )
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector == "commodity"


def test_corpus_includes_category_and_subcategory(
    keywords: SectorKeywordsConfig,
) -> None:
    market = _make_market(
        question="Generic question",
        category="OPEC summit news",
    )
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector == "commodity"


def test_empty_question_no_other_text_returns_none(
    keywords: SectorKeywordsConfig,
) -> None:
    market = _make_market(question="")
    mapping = map_sector(market, keywords=keywords)
    assert mapping.razor_sector is None
    assert mapping.scores == dict.fromkeys(keywords.sectors.keys(), 0)


def test_sector_mapping_dataclass_carries_scores(
    keywords: SectorKeywordsConfig,
) -> None:
    market = _make_market(question="Will OPEC cut oil output?")
    mapping = map_sector(market, keywords=keywords)
    assert isinstance(mapping, SectorMapping)
    assert mapping.scores["commodity"] == 2  # 'OPEC' + 'oil'


def test_returns_inferred_confidence_always(keywords: SectorKeywordsConfig) -> None:
    market = _make_market(question="Will the WHO pandemic continue?")
    mapping = map_sector(market, keywords=keywords)
    assert mapping.confidence == INFERRED_CONFIDENCE
