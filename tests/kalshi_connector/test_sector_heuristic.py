"""T-KSI-050 — Kalshi sector heuristic mapper acceptance tests.

Verifies:
- Each of the eight sectors classifies a representative title.
- ``out_of_scope`` is reachable both via category Pass-1 match and via
  keyword-scan Pass-2 match.
- Sports categories trigger Pass-1 auto-classification.
- Ties (two sectors with equal hit counts) return ``razor_sector=None``
  with both top sectors in ``secondary_sectors``.
- All-zero scores return ``razor_sector=None``.
- Series fields (title, category, tags) contribute to the corpus.
- Word-boundary matching: 'oil' matches 'oil' but not 'foil'.
- Heuristic version + tag are stamped on outputs.
"""

from __future__ import annotations

import pytest

from razor_rooster.kalshi_connector.client.models import (
    KalshiMarket,
    KalshiSeries,
)
from razor_rooster.kalshi_connector.config.loader import (
    KalshiSectorKeywordsConfig,
)
from razor_rooster.kalshi_connector.mapping.sector_heuristic import (
    HEURISTIC_TAG,
    HEURISTIC_VERSION,
    INFERRED_CONFIDENCE,
    map_sector,
)


@pytest.fixture
def keywords() -> KalshiSectorKeywordsConfig:
    return KalshiSectorKeywordsConfig(
        version=1,
        sectors={
            "macroeconomic": ["CPI", "Fed", "rate hike", "GDP"],
            "regulatory": ["FDA", "Congress", "executive order"],
            "commodity": ["oil", "wheat", "OPEC"],
            "climate": ["hurricane", "drought"],
            "geopolitical": ["election", "ceasefire"],
            "public_health": ["vaccine", "outbreak"],
            "infrastructure_energy": ["pipeline", "grid"],
            "cross_cutting": ["global"],
            "out_of_scope": ["Super Bowl", "NFL", "Oscar"],
        },
    )


def _market(
    *,
    ticker: str = "KXFOO",
    title: str = "",
    sub_title: str | None = None,
    category: str | None = None,
    yes_sub_title: str | None = None,
    no_sub_title: str | None = None,
) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        event_ticker="EVT",
        series_ticker="SER",
        title=title,
        sub_title=sub_title,
        market_type="binary",
        strike_type=None,
        floor_strike=None,
        cap_strike=None,
        open_time=None,
        close_time=None,
        expiration_time=None,
        expected_expiration_time=None,
        latest_expiration_time=None,
        settlement_timer_seconds=None,
        status="open",
        yes_sub_title=yes_sub_title,
        no_sub_title=no_sub_title,
        result=None,
        can_close_early=None,
        expiration_value=None,
        category=category,
        risk_limit_cents=None,
        notional_value=None,
        tick_size=None,
        last_price_dollars=None,
        previous_yes_bid_dollars=None,
        previous_yes_ask_dollars=None,
        previous_price_dollars=None,
        volume_24h=None,
        volume=None,
        liquidity=None,
        open_interest=None,
    )


def _series(
    *,
    ticker: str = "SER",
    title: str = "",
    category: str | None = None,
    tags: tuple[str, ...] = (),
) -> KalshiSeries:
    return KalshiSeries(
        series_ticker=ticker,
        title=title,
        category=category,
        tags=tags,
    )


def test_macroeconomic_classification(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    m = _market(title="CPI above 2.5% in August")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "macroeconomic"
    assert result.confidence == INFERRED_CONFIDENCE


def test_regulatory_classification(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    m = _market(title="FDA approves new drug by year-end")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "regulatory"


def test_commodity_classification(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    m = _market(title="WTI oil settles above $80 next week")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "commodity"


def test_climate_classification(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    m = _market(title="Atlantic hurricane makes landfall in September")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "climate"


def test_geopolitical_classification(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    m = _market(title="2026 midterm election control")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "geopolitical"


def test_public_health_classification(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    m = _market(title="vaccine approval before Q3")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "public_health"


def test_infrastructure_energy_classification(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    m = _market(title="major pipeline outage in October")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "infrastructure_energy"


def test_out_of_scope_via_category(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """Pass 1: Sports category auto-classifies as out_of_scope."""
    m = _market(title="Some title with no keywords", category="Sports")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "out_of_scope"


def test_out_of_scope_via_series_category(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """Pass 1: series-level category also triggers out_of_scope."""
    m = _market(title="some title")
    series = _series(category="Entertainment")
    result = map_sector(m, keywords=keywords, series=series)
    assert result.razor_sector == "out_of_scope"


def test_out_of_scope_via_keywords(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """Pass 2: NFL keyword fires out_of_scope without a category match."""
    m = _market(title="NFL season opener winner")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "out_of_scope"


def test_tie_returns_none_with_secondary(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """Two sectors with equal hits → ambiguous → None + secondary list."""
    # 'oil' (commodity) + 'CPI' (macroeconomic), each one keyword.
    m = _market(title="CPI vs oil price impact")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector is None
    assert "macroeconomic" in result.secondary_sectors
    assert "commodity" in result.secondary_sectors


def test_all_zero_scores_returns_none(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    m = _market(title="totally unrelated string about lions and tigers")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector is None
    assert result.secondary_sectors == ()


def test_series_tags_contribute_to_corpus(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """Series tags participate in keyword scoring."""
    m = _market(title="generic question text")
    series = _series(tags=("CPI", "macro"))
    result = map_sector(m, keywords=keywords, series=series)
    assert result.razor_sector == "macroeconomic"


def test_word_boundary_matching(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """'oil' matches 'oil' but not 'foil' or 'broil'."""
    m_pos = _market(title="oil price target reached")
    m_neg = _market(title="aluminum foil futures market")
    pos = map_sector(m_pos, keywords=keywords)
    neg = map_sector(m_neg, keywords=keywords)
    assert pos.razor_sector == "commodity"
    assert neg.razor_sector is None


def test_multi_word_keyword_matching(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """'rate hike' matches as a phrase with flexible whitespace."""
    m = _market(title="Fed rate hike at September meeting")
    result = map_sector(m, keywords=keywords)
    # 'Fed' (1 hit) + 'rate hike' (1 hit) = 2 in macroeconomic.
    assert result.razor_sector == "macroeconomic"
    assert result.scores["macroeconomic"] == 2


def test_heuristic_tag_stamped_on_output(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    m = _market(title="CPI above 2.5%")
    result = map_sector(m, keywords=keywords)
    assert result.mapped_by == HEURISTIC_TAG
    assert f"kalshi_heuristic_v{HEURISTIC_VERSION}" == HEURISTIC_TAG


def test_secondary_sectors_only_include_nonzero_scorers(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """A sector with zero hits doesn't show up in secondary_sectors."""
    m = _market(title="oil and FDA topics; no climate keyword present")
    result = map_sector(m, keywords=keywords)
    # 'oil' (commodity, 1) + 'FDA' (regulatory, 1) → tie. Both top.
    assert result.razor_sector is None
    # 'climate' had zero hits; should NOT be in the secondaries.
    assert "climate" not in result.secondary_sectors


def test_uppercase_keyword_matches_case_insensitively(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    m = _market(title="cpi report on tuesday morning")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "macroeconomic"


def test_yes_no_sub_titles_contribute_to_corpus(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """The sub-titles for the yes / no outcomes participate in scoring."""
    m = _market(
        title="Generic title",
        yes_sub_title="oil above $80",
        no_sub_title="oil at or below $80",
    )
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "commodity"


def test_scores_dict_returned_for_triage(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """Scores dict is populated so triage UI can show why it landed."""
    m = _market(title="CPI release in September")
    result = map_sector(m, keywords=keywords)
    assert "macroeconomic" in result.scores
    assert result.scores["macroeconomic"] >= 1


def test_market_with_only_category_sports_no_keywords(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    """A market whose only signal is the Sports category still classifies."""
    m = _market(title="random title", category="Sports - Daily")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "out_of_scope"


def test_oscar_keyword_classifies_out_of_scope(
    keywords: KalshiSectorKeywordsConfig,
) -> None:
    m = _market(title="Oscar winner for Best Picture")
    result = map_sector(m, keywords=keywords)
    assert result.razor_sector == "out_of_scope"
