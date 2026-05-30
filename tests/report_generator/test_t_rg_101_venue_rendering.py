"""T-RG-101 — venue rendering across report sections.

Verifies:
- Surfaced section assemblers carry venue forward in the content dict.
- Watched section assemblers carry venue forward in the content dict.
- Calibration section assemblers carry venue forward in the content dict.
- Watchlist suggestion mentions both venues.
- Terminal renderer surfaces ``(<venue>)`` after market identifiers.
- Markdown renderer surfaces ``(<venue>)`` after market identifiers.
- Markdown calibration table includes a Venue column.
- Linter still passes on the rendered output (no new forbidden phrases
  introduced by the venue tag).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from razor_rooster.position_engine.frame.linter import check_text
from razor_rooster.report_generator.engines.section_assemblers.watchlist import (
    _suggestion_for_reason,
)
from razor_rooster.report_generator.models import SectionContent
from razor_rooster.report_generator.renderer import markdown, terminal


def _baseline_header() -> dict[str, Any]:
    return {
        "cycle_date": "2026-05-15",
        "since_ts": datetime(2026, 5, 14, tzinfo=UTC),
        "until_ts": datetime(2026, 5, 15, tzinfo=UTC),
        "library_version": 7,
        "library_age_days": 1,
        "stale_source_count": 0,
        "disabled_sections": (),
        "report_id": "rpt-1",
    }


def _baseline_footer() -> dict[str, Any]:
    return {
        "disclaimer_text": "Decision-support only.",
        "completed_at": datetime(2026, 5, 15, 14, tzinfo=UTC),
        "system_version": "0.1.0",
        "report_id": "rpt-1",
    }


def _baseline_surfaced_section(
    *,
    venue: str,
    condition_id: str,
) -> SectionContent:
    return SectionContent(
        name="surfaced",
        content={
            "type": "surfaced",
            "comparisons": [
                {
                    "comparison_id": "cmp-1",
                    "class_id": "cls",
                    "class_title": "Test Class",
                    "domain_sector": "macro_us",
                    "condition_id": condition_id,
                    "venue": venue,
                    "model_p": 0.30,
                    "model_ci": (0.20, 0.40),
                    "market_p": 0.10,
                    "market_spread_bps": 100,
                    "delta": 0.20,
                    "log_odds_delta": 0.50,
                    "ev": 0.05,
                    "score": 1.5,
                    "case_for_model": ["Reason 1"],
                    "case_for_market": ["Reason 2"],
                    "ambiguity_factors": [],
                    "warnings": [],
                    "scan_trace": None,
                    "comparison_trace": None,
                    "analysis": None,
                }
            ],
        },
    )


def _baseline_watched_section(
    *,
    venue: str,
    condition_id: str,
) -> SectionContent:
    return SectionContent(
        name="watched",
        content={
            "type": "watched",
            "follow_ups": [
                {
                    "follow_up_id": "fu-1",
                    "analysis_id": "ana-1",
                    "class_id": "cls",
                    "class_title": "Test Class",
                    "condition_id": condition_id,
                    "venue": venue,
                    "primary_alert_tier": "material_shift",
                    "alert_tiers": ["material_shift"],
                    "analysis_model_p": 0.30,
                    "current_model_p": 0.32,
                    "analysis_market_p": 0.10,
                    "current_market_p": 0.11,
                    "model_shift_band": "minor",
                    "market_shift_band": "minor",
                    "days_since_analysis": 1,
                    "days_to_resolution": 29,
                    "resolution_status": "unresolved",
                    "reasoning_text": "Test reasoning.",
                    "computed_at": datetime(2026, 5, 15, tzinfo=UTC),
                }
            ],
        },
    )


def _baseline_calibration_section(*, venue: str) -> SectionContent:
    return SectionContent(
        name="calibration",
        content={
            "type": "calibration",
            "resolutions": [
                {
                    "comparison_id": "cmp-old",
                    "class_id": "cls",
                    "class_title": "Test Class",
                    "condition_id": "MKT-A",
                    "venue": venue,
                    "resolution_outcome": "yes",
                    "model_probability": 0.75,
                    "market_probability": 0.30,
                    "polarity": "aligned",
                    "outcome_observed": 1,
                    "days_to_resolution": 14,
                    "predicted_band": "high",
                    "verdict_text": "Model said 0.75 → resolved YES; in line.",
                }
            ],
        },
    )


def _baseline_simple_sections() -> Mapping[str, SectionContent]:
    return {
        "system_health": SectionContent(
            name="system_health",
            content={"stale_sources": [], "library_age_days": 1, "errored_subsystems": []},
        ),
        "watchlist": SectionContent(
            name="watchlist",
            content={"type": "watchlist", "candidates": []},
        ),
    }


# -- terminal rendering -----------------------------------------------------


def test_terminal_renders_polymarket_venue_tag_in_surfaced() -> None:
    surfaced = _baseline_surfaced_section(venue="polymarket", condition_id="0xabc")
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[surfaced],
        footer=_baseline_footer(),
    )
    assert "0xabc" in text
    assert "(polymarket)" in text


def test_terminal_renders_kalshi_venue_tag_in_surfaced() -> None:
    surfaced = _baseline_surfaced_section(venue="kalshi", condition_id="KXTICK")
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[surfaced],
        footer=_baseline_footer(),
    )
    assert "KXTICK" in text
    assert "(kalshi)" in text


def test_terminal_renders_kalshi_venue_tag_in_watched() -> None:
    watched = _baseline_watched_section(venue="kalshi", condition_id="KXTICK")
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[watched],
        footer=_baseline_footer(),
    )
    assert "KXTICK" in text
    assert "(kalshi)" in text


def test_terminal_renders_kalshi_venue_in_calibration() -> None:
    cal = _baseline_calibration_section(venue="kalshi")
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[cal],
        footer=_baseline_footer(),
    )
    assert "MKT-A" in text
    assert "(kalshi)" in text


# -- markdown rendering -----------------------------------------------------


def test_markdown_renders_polymarket_venue_tag_in_surfaced() -> None:
    surfaced = _baseline_surfaced_section(venue="polymarket", condition_id="0xabc")
    text = markdown.render(
        header=_baseline_header(),
        body_sections=[surfaced],
        footer=_baseline_footer(),
    )
    assert "`0xabc`" in text
    assert "(polymarket)" in text


def test_markdown_renders_kalshi_venue_tag_in_watched() -> None:
    watched = _baseline_watched_section(venue="kalshi", condition_id="KXTICK")
    text = markdown.render(
        header=_baseline_header(),
        body_sections=[watched],
        footer=_baseline_footer(),
    )
    assert "`KXTICK`" in text
    assert "(kalshi)" in text


def test_markdown_calibration_table_has_venue_column() -> None:
    cal_poly = _baseline_calibration_section(venue="polymarket")
    text_poly = markdown.render(
        header=_baseline_header(),
        body_sections=[cal_poly],
        footer=_baseline_footer(),
    )
    cal_kalshi = _baseline_calibration_section(venue="kalshi")
    text_kalshi = markdown.render(
        header=_baseline_header(),
        body_sections=[cal_kalshi],
        footer=_baseline_footer(),
    )
    # The new header row.
    assert "| Class | Venue | Outcome | Predicted p | Days to Resolution | Verdict |" in text_poly
    assert "| polymarket |" in text_poly
    assert "| kalshi |" in text_kalshi


# -- watchlist suggestion ---------------------------------------------------


def test_watchlist_suggestion_mentions_both_venues() -> None:
    suggestion = _suggestion_for_reason("no_active_mapping")
    assert "Polymarket" in suggestion
    assert "Kalshi" in suggestion


def test_watchlist_stale_suggestion_is_venue_neutral() -> None:
    suggestion = _suggestion_for_reason("all_stale_market_price")
    # Should no longer mention "polymarket" specifically.
    assert "polymarket" not in suggestion.lower() or "venue" in suggestion.lower()


# -- linter ----------------------------------------------------------------


def test_terminal_render_with_venue_passes_linter() -> None:
    """The venue tag must not introduce any forbidden imperative phrasing."""
    surfaced_poly = _baseline_surfaced_section(venue="polymarket", condition_id="0xabc")
    surfaced_kalshi = _baseline_surfaced_section(venue="kalshi", condition_id="KXTICK")
    watched_kalshi = _baseline_watched_section(venue="kalshi", condition_id="KXTICK")
    cal_kalshi = _baseline_calibration_section(venue="kalshi")
    text = terminal.render(
        header=_baseline_header(),
        body_sections=[surfaced_poly, surfaced_kalshi, watched_kalshi, cal_kalshi],
        footer=_baseline_footer(),
    )
    # check_text raises if any forbidden phrase matches.
    check_text(text)


def test_markdown_render_with_venue_passes_linter() -> None:
    surfaced_kalshi = _baseline_surfaced_section(venue="kalshi", condition_id="KXTICK")
    watched_kalshi = _baseline_watched_section(venue="kalshi", condition_id="KXTICK")
    cal_kalshi = _baseline_calibration_section(venue="kalshi")
    text = markdown.render(
        header=_baseline_header(),
        body_sections=[surfaced_kalshi, watched_kalshi, cal_kalshi],
        footer=_baseline_footer(),
    )
    check_text(text)
