"""T-RG-030..T-RG-033 — renderer tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from razor_rooster.position_engine.frame.linter import (
    ImperativeLanguageDetected,
    LinterCatalog,
    check_text,
)
from razor_rooster.report_generator.models import SectionContent
from razor_rooster.report_generator.renderer import markdown, terminal
from razor_rooster.report_generator.renderer.shared import (
    disclaimer_block,
    disclaimer_version_hash,
    equal_prominence_blocks,
    section_divider,
    thin_divider,
    warnings_block,
)

# -- shared helpers ---------------------------------------------------------


def test_disclaimer_block_includes_header() -> None:
    block = disclaimer_block("Some disclaimer text.")
    assert block.startswith("DISCLAIMER:")
    assert "Some disclaimer text." in block


def test_disclaimer_version_hash_is_stable() -> None:
    h1 = disclaimer_version_hash("text")
    h2 = disclaimer_version_hash("text\n")
    assert h1 == h2  # whitespace-stripped before hashing
    assert len(h1) == 64  # SHA-256 hex


def test_equal_prominence_blocks_pads_shorter_side() -> None:
    text = equal_prominence_blocks(
        model_label="Model:",
        model_bullets=["m1", "m2", "m3"],
        market_label="Market:",
        market_bullets=["k1"],
    )
    # Both blocks have three bullet lines.
    model_block, market_block = text.split("\n\n")
    model_bullets = [ln for ln in model_block.split("\n") if ln.startswith("  - ")]
    market_bullets = [ln for ln in market_block.split("\n") if ln.startswith("  - ")]
    assert len(model_bullets) == 3
    assert len(market_bullets) == 3
    assert "(no specific items identified)" in market_block


def test_dividers_have_correct_widths() -> None:
    assert section_divider().startswith("=")
    assert thin_divider().startswith("-")
    assert len(section_divider()) == 80


def test_warnings_block_empty_returns_empty_string() -> None:
    assert warnings_block([]) == ""


def test_warnings_block_lists_each() -> None:
    out = warnings_block(["low_liquidity", "stale_market_price"])
    assert "Warnings:" in out
    assert "low_liquidity" in out


# -- terminal renderer ------------------------------------------------------


def _baseline_header() -> dict[str, Any]:
    return {
        "type": "header",
        "report_id": "rep-1",
        "cycle_date": "2026-05-15",
        "library_version": 7,
        "since_ts": datetime(2026, 5, 14, tzinfo=UTC),
        "until_ts": datetime(2026, 5, 15, tzinfo=UTC),
        "stale_source_count": 0,
        "library_age_days": None,
        "disabled_sections": (),
    }


def _baseline_footer() -> dict[str, Any]:
    return {
        "type": "footer",
        "disclaimer_text": (
            "This report is decision-support analysis. The system "
            "surfaces patterns, comparisons, and analyses; it does "
            "not place trades, recommend specific actions, or claim "
            "certainty about future events."
        ),
        "system_version": "0.1.0",
        "report_id": "rep-1",
        "completed_at": datetime(2026, 5, 15, 14, tzinfo=UTC),
    }


def test_terminal_renders_minimal_report() -> None:
    body = (
        SectionContent(
            name="system_health",
            content={
                "type": "system_health",
                "stale_sources": [],
                "errored_subsystems": [],
                "suppressed_breakdown": {},
                "library_age_days": None,
            },
        ),
    )
    text = terminal.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    assert "RAZOR-ROOSTER REPORT" in text
    assert "SYSTEM HEALTH" in text
    assert "DISCLAIMER:" in text
    assert "Razor-Rooster 0.1.0" in text


def test_terminal_renders_section_error_placeholder() -> None:
    body = (SectionContent(name="surfaced", content=None, error="boom"),)
    text = terminal.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    assert "section error: boom" in text


def test_terminal_renders_empty_section_message() -> None:
    body = (SectionContent(name="surfaced", content={"type": "surfaced", "comparisons": []}),)
    text = terminal.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    assert "No comparisons surfaced" in text


def test_terminal_surfaced_includes_balanced_cases() -> None:
    body = (
        SectionContent(
            name="surfaced",
            content={
                "type": "surfaced",
                "comparisons": [
                    {
                        "comparison_id": "c1",
                        "class_title": "Test",
                        "domain_sector": "geopolitics",
                        "model_p": 0.30,
                        "model_ci": (0.20, 0.40),
                        "market_p": 0.10,
                        "market_spread_bps": 200,
                        "delta": 0.20,
                        "ev": 0.05,
                        "case_for_model": ["Model wins"],
                        "case_for_market": ["Market wins more"],
                        "warnings": [],
                        "ambiguity_factors": [],
                        "analysis": None,
                    }
                ],
            },
        ),
    )
    text = terminal.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    assert "Possible reasons the model may be right" in text
    assert "Possible reasons the market may be right" in text
    assert "Model wins" in text
    assert "Market wins more" in text


def test_terminal_calibration_renders_verdicts() -> None:
    body = (
        SectionContent(
            name="calibration",
            content={
                "type": "calibration",
                "resolutions": [
                    {
                        "comparison_id": "c1",
                        "class_title": "Class A",
                        "condition_id": "0xMARKET",
                        "resolution_outcome": "yes",
                        "model_probability": 0.85,
                        "market_probability": 0.10,
                        "polarity": "aligned",
                        "outcome_observed": 1,
                        "days_to_resolution": 14,
                        "predicted_band": "high",
                        "verdict_text": "Model said 0.85 → resolved YES; in line with predicted likelihood.",
                    }
                ],
            },
        ),
    )
    text = terminal.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    assert "Class A" in text
    assert "Model said 0.85" in text


def test_terminal_watched_includes_reasoning_text() -> None:
    body = (
        SectionContent(
            name="watched",
            content={
                "type": "watched",
                "follow_ups": [
                    {
                        "follow_up_id": "fu-1",
                        "analysis_id": "an-1",
                        "class_id": "cls",
                        "class_title": "Watched Class",
                        "primary_alert_tier": "material_shift",
                        "alert_tiers": ["material_shift"],
                        "analysis_model_p": 0.30,
                        "current_model_p": 0.42,
                        "analysis_market_p": 0.10,
                        "current_market_p": 0.12,
                        "model_shift_band": "material",
                        "market_shift_band": "minor",
                        "days_since_analysis": 5,
                        "days_to_resolution": 60,
                        "resolution_status": "unresolved",
                        "reasoning_text": "Synthetic reasoning text.",
                    }
                ],
            },
        ),
    )
    text = terminal.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    assert "Watched Class" in text
    assert "material_shift" in text
    assert "Synthetic reasoning text." in text


def test_terminal_watchlist_compact_omits_posterior() -> None:
    body = (
        SectionContent(
            name="watchlist",
            content={
                "type": "watchlist",
                "candidates": [
                    {
                        "scan_id": "s1",
                        "class_id": "c1",
                        "class_title": "Unmapped Class",
                        "domain_sector": "geopolitics",
                        "posterior": 0.40,
                        "base_rate": 0.10,
                        "log_odds_shift": 1.5,
                        "candidate_direction": "above",
                        "reason": "no_active_mapping",
                        "suggestion": "Consider mapping",
                        "verbosity": "compact",
                    }
                ],
            },
        ),
    )
    text = terminal.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    assert "Unmapped Class" in text
    assert "Consider mapping" in text
    assert "posterior" not in text  # compact mode omits posterior


# -- markdown renderer ------------------------------------------------------


def test_markdown_renders_minimal_report() -> None:
    body = (
        SectionContent(
            name="system_health",
            content={
                "type": "system_health",
                "stale_sources": [],
                "errored_subsystems": [],
                "suppressed_breakdown": {},
            },
        ),
    )
    text = markdown.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    assert text.startswith("# Razor-Rooster Report — 2026-05-15")
    assert "## System Health" in text
    assert "## Disclaimer" in text


def test_markdown_calibration_uses_table() -> None:
    body = (
        SectionContent(
            name="calibration",
            content={
                "type": "calibration",
                "resolutions": [
                    {
                        "class_title": "Class A",
                        "condition_id": "0xMARKET",
                        "resolution_outcome": "yes",
                        "model_probability": 0.85,
                        "polarity": "aligned",
                        "days_to_resolution": 14,
                        "predicted_band": "high",
                        "verdict_text": "Model said 0.85 → resolved YES; in line.",
                    }
                ],
            },
        ),
    )
    text = markdown.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    assert "| Class | Venue | Outcome | Predicted p | Days to Resolution | Verdict |" in text
    assert "Class A" in text
    assert "YES" in text


def test_markdown_surfaced_uses_h3_subsections() -> None:
    body = (
        SectionContent(
            name="surfaced",
            content={
                "type": "surfaced",
                "comparisons": [
                    {
                        "comparison_id": "c1",
                        "class_title": "Test Class",
                        "domain_sector": "geopolitics",
                        "model_p": 0.30,
                        "model_ci": (0.20, 0.40),
                        "market_p": 0.10,
                        "case_for_model": ["A"],
                        "case_for_market": ["B"],
                        "warnings": ["low_liquidity"],
                        "ambiguity_factors": [],
                        "analysis": None,
                    }
                ],
            },
        ),
    )
    text = markdown.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    assert "### Test Class (geopolitics)" in text
    assert "**Possible reasons the model may be right:**" in text
    assert "**Possible reasons the market may be right:**" in text
    assert "low_liquidity" in text


def test_markdown_balanced_cases_pads_shorter_side() -> None:
    body = (
        SectionContent(
            name="surfaced",
            content={
                "type": "surfaced",
                "comparisons": [
                    {
                        "comparison_id": "c1",
                        "class_title": "T",
                        "domain_sector": "g",
                        "model_p": 0.30,
                        "model_ci": (0.20, 0.40),
                        "market_p": 0.10,
                        "case_for_model": ["one", "two", "three"],
                        "case_for_market": ["A"],
                        "warnings": [],
                        "ambiguity_factors": [],
                        "analysis": None,
                    }
                ],
            },
        ),
    )
    text = markdown.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    assert text.count("(no specific items identified)") == 2


def test_markdown_disclaimer_uses_blockquote() -> None:
    body: tuple[SectionContent, ...] = ()
    text = markdown.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    # Each line of the disclaimer should be prefixed with `> `.
    assert "> This report is decision-support analysis." in text


# -- linter integration -----------------------------------------------------


def test_linter_passes_clean_terminal_output() -> None:
    body: tuple[SectionContent, ...] = ()
    text = terminal.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    # Should not raise.
    check_text(text)


def test_linter_passes_clean_markdown_output() -> None:
    body: tuple[SectionContent, ...] = ()
    text = markdown.render(
        header=_baseline_header(),
        body_sections=body,
        footer=_baseline_footer(),
    )
    check_text(text)


def test_linter_rejects_imperative_phrase() -> None:
    """Adversarial: an injected imperative phrase is flagged (REQ-RG-FRAME-001)."""
    bad_text = "Razor-Rooster Report\n\nI recommend you take this position now."
    with pytest.raises(ImperativeLanguageDetected):
        check_text(bad_text)


def test_linter_rejects_certainty_claim() -> None:
    """REQ-RG-FRAME-004: phrases like 'guaranteed to' are forbidden."""
    bad_text = "The model is guaranteed to be correct on this one."
    with pytest.raises(ImperativeLanguageDetected):
        check_text(bad_text)


def test_linter_catalog_loads_from_default_path() -> None:
    """The shared catalog file is the same one position_engine uses."""
    catalog = LinterCatalog.from_yaml()
    # At minimum the seed phrases are present.
    assert any("recommend" in p for p in catalog.phrases)
