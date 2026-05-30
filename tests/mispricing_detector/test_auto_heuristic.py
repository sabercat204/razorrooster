"""T-MD-021 — auto-mapping heuristic acceptance tests."""

from __future__ import annotations

import pandas as pd

from razor_rooster.mispricing_detector.mapping.auto_heuristic import (
    HeuristicConfig,
    MarketSummary,
    confidence,
    has_temporal_qualifier,
    keyword_overlap,
)
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    Sector,
)


def _occurrences(_conn: object) -> pd.DataFrame:
    return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})


def _make_class(
    *,
    class_id: str = "pheic_declaration_12mo",
    title: str = "WHO PHEIC declaration in a 12-month window",
    description: str = (
        "A Public Health Emergency of International Concern declaration by the "
        "WHO in the rolling 12-month window."
    ),
    sector: Sector = Sector.PUBLIC_HEALTH,
) -> EventClass:
    return EventClass(
        class_id=class_id,
        title=title,
        description=description,
        domain_sector=sector,
        occurrence_query=_occurrences,
    )


def test_keyword_overlap_basic() -> None:
    overlap = keyword_overlap(
        "WHO declares public health emergency",
        "Will the WHO declare a Public Health Emergency in 2026?",
    )
    # who, declare, public, health, emergency
    assert overlap >= 4


def test_keyword_overlap_strips_stopwords() -> None:
    """The stopword set ('the', 'in', 'a') doesn't inflate overlap counts."""
    overlap = keyword_overlap("a the in", "a the in")
    assert overlap == 0


def test_temporal_qualifier_detection() -> None:
    assert has_temporal_qualifier("Will it happen in 2026?") is True
    assert has_temporal_qualifier("Will it happen by year-end?") is True
    assert has_temporal_qualifier("Will it happen in Q4 2026?") is True
    assert has_temporal_qualifier("Will it happen?") is False
    assert has_temporal_qualifier("This year, will it happen?") is True


def test_inferred_when_sector_match_and_strong_overlap_and_temporal() -> None:
    cls = _make_class()
    market = MarketSummary(
        condition_id="0xabc",
        razor_sector="public_health",
        question=(
            "Will the WHO declare a new Public Health Emergency of International Concern in 2026?"
        ),
        description="Resolves YES if WHO formally declares a new PHEIC by 2026-12-31.",
    )
    assert confidence(cls, market) == "inferred"


def test_low_when_sector_match_but_weak_overlap() -> None:
    cls = _make_class()
    market = MarketSummary(
        condition_id="0xdef",
        razor_sector="public_health",
        question="Will any vaccine see emergency authorization in 2026?",
    )
    # Sector matches; words like "emergency" overlap once. No PHEIC keyword.
    assert confidence(cls, market) == "low"


def test_low_when_sector_match_and_strong_overlap_but_no_temporal() -> None:
    cls = _make_class()
    market = MarketSummary(
        condition_id="0xghi",
        razor_sector="public_health",
        question=("Will WHO declare a Public Health Emergency of International Concern?"),
    )
    # Strong overlap but no temporal qualifier -> low (require_temporal default True).
    assert confidence(cls, market) == "low"


def test_inferred_when_overlap_threshold_lower_via_config() -> None:
    cls = _make_class()
    market = MarketSummary(
        condition_id="0xabc",
        razor_sector="public_health",
        question="Will WHO act in 2026?",  # 'who', 'act' — only one strong overlap
    )
    cfg = HeuristicConfig(min_keyword_overlap_for_inferred=1)
    assert confidence(cls, market, config=cfg) == "inferred"


def test_none_when_sector_mismatch() -> None:
    cls = _make_class(sector=Sector.PUBLIC_HEALTH)
    market = MarketSummary(
        condition_id="0xjkl",
        razor_sector="commodity",
        question="Will Brent crude exceed $100 in 2026?",
    )
    assert confidence(cls, market) is None


def test_none_when_market_has_no_sector() -> None:
    cls = _make_class()
    market = MarketSummary(
        condition_id="0xnone",
        razor_sector=None,
        question="Will WHO declare PHEIC in 2026?",
    )
    assert confidence(cls, market) is None


def test_cross_cutting_class_does_not_auto_map() -> None:
    """CROSS_CUTTING classes never auto-map; require operator action."""
    cls = _make_class(sector=Sector.CROSS_CUTTING)
    market = MarketSummary(
        condition_id="0xcross",
        razor_sector="cross_cutting",
        question="Will WHO declare PHEIC in 2026?",
    )
    assert confidence(cls, market) is None


def test_can_disable_temporal_requirement() -> None:
    cls = _make_class()
    market = MarketSummary(
        condition_id="0xnotemporal",
        razor_sector="public_health",
        question=("Will WHO declare Public Health Emergency of International Concern?"),
    )
    cfg = HeuristicConfig(require_temporal_qualifier_for_inferred=False)
    assert confidence(cls, market, config=cfg) == "inferred"
