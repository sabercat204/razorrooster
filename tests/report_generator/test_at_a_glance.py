"""Tests for the at-a-glance report section (T-RG-COMPAT-GLANCE-001 v0.45.0).

Covers:
- Per-section extractors return the top item from each section's ordered list.
- Empty inputs produce empty facts.
- Generator integration: at_a_glance runs after other sections and lifts
  their content.
- Renderers (terminal + markdown + html) emit the facts cleanly.
- Output passes the imperative-language linter, including the editorial
  blocklist added in v0.45.0 (e.g. "particularly notable", "key takeaway").
- Adversarial test: synthetic content with editorial-flavored class titles
  doesn't propagate forbidden phrases.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.position_engine.frame.linter import (
    ImperativeLanguageDetected,
    check_text,
)
from razor_rooster.report_generator.config.loader import (
    ALL_SECTIONS,
    ReportConfig,
)
from razor_rooster.report_generator.engines.generator import generate
from razor_rooster.report_generator.engines.section_assemblers import (
    at_a_glance as at_a_glance_assembler,
)
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "glance.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_report_generator_migrations(c)
    yield s
    s.close()


# -- assembler: per-section extractors -----------------------------------


def test_empty_input_returns_no_facts() -> None:
    out = at_a_glance_assembler.assemble({})
    assert out == {"type": "at_a_glance", "facts": []}


def test_extracts_top_cross_venue_item() -> None:
    cross_venue_content = {
        "type": "cross_venue",
        "items": [
            {
                "class_title": "CPI 2.5%",
                "spread_bps": 1500,
                "venue_prices": [
                    {"venue": "polymarket"},
                    {"venue": "kalshi"},
                ],
            },
            {
                "class_title": "Smaller spread",
                "spread_bps": 600,
                "venue_prices": [{"venue": "polymarket"}, {"venue": "kalshi"}],
            },
        ],
    }
    out = at_a_glance_assembler.assemble({"cross_venue": cross_venue_content})
    facts = out["facts"]
    assert len(facts) == 1
    fact = facts[0]
    assert fact["section"] == "cross_venue"
    assert "CPI 2.5%" in fact["value"]
    assert "1500 bps" in fact["value"]
    assert "kalshi vs polymarket" in fact["value"] or "polymarket vs kalshi" in fact["value"]


def test_extracts_top_surfaced_comparison() -> None:
    surfaced_content = {
        "type": "surfaced",
        "comparisons": [
            {"class_title": "Top class", "venue": "polymarket", "delta": 0.123},
            {"class_title": "Second", "venue": "kalshi", "delta": -0.05},
        ],
    }
    out = at_a_glance_assembler.assemble({"surfaced": surfaced_content})
    fact = out["facts"][0]
    assert fact["section"] == "surfaced"
    assert "Top class" in fact["value"]
    assert "polymarket" in fact["value"]
    assert "+0.123" in fact["value"]


def test_extracts_top_watched_alert() -> None:
    watched_content = {
        "type": "watched",
        "follow_ups": [
            {
                "class_title": "Watched class",
                "primary_alert_tier": "material_shift",
            },
        ],
    }
    out = at_a_glance_assembler.assemble({"watched": watched_content})
    fact = out["facts"][0]
    assert fact["section"] == "watched"
    assert "Watched class" in fact["value"]
    assert "material_shift" in fact["value"]


def test_calibration_prefers_miscalibrated_sector() -> None:
    """When sector_brier_scores has a miscalibrated entry, surface it first."""
    cal_content = {
        "type": "calibration",
        "resolutions": [
            {"class_title": "Recent", "resolution_outcome": "yes"},
        ],
        "sector_brier_scores": [
            {"sector": "macroeconomic", "brier_score": 0.15, "miscalibrated": False},
            {"sector": "geopolitical", "brier_score": 0.32, "miscalibrated": True},
        ],
    }
    out = at_a_glance_assembler.assemble({"calibration": cal_content})
    fact = out["facts"][0]
    assert fact["label"] == "top miscalibrated sector"
    assert "geopolitical" in fact["value"]


def test_calibration_falls_back_to_first_resolution() -> None:
    """When no sector is miscalibrated, surface the first resolution."""
    cal_content = {
        "type": "calibration",
        "resolutions": [
            {"class_title": "Recent class", "resolution_outcome": "yes"},
        ],
        "sector_brier_scores": [
            {"sector": "macroeconomic", "brier_score": 0.15, "miscalibrated": False},
        ],
    }
    out = at_a_glance_assembler.assemble({"calibration": cal_content})
    fact = out["facts"][0]
    assert fact["label"] == "most-recent resolution"
    assert "Recent class" in fact["value"]
    assert "YES" in fact["value"]


def test_full_content_emits_one_fact_per_section() -> None:
    """All four section types present → 4 facts emitted in fixed order."""
    content_by_name = {
        "cross_venue": {"items": [{"class_title": "cv1", "spread_bps": 800, "venue_prices": []}]},
        "surfaced": {"comparisons": [{"class_title": "s1", "venue": "kalshi", "delta": 0.1}]},
        "watched": {"follow_ups": [{"class_title": "w1", "primary_alert_tier": "resolution"}]},
        "calibration": {
            "resolutions": [{"class_title": "c1", "resolution_outcome": "no"}],
            "sector_brier_scores": [],
        },
    }
    out = at_a_glance_assembler.assemble(content_by_name)
    facts = out["facts"]
    assert len(facts) == 4
    sections = [f["section"] for f in facts]
    assert sections == ["cross_venue", "surfaced", "watched", "calibration"]


def test_handles_failed_section_content() -> None:
    """When a section's content is None (it failed), skip its fact."""
    content_by_name = {
        "cross_venue": None,
        "surfaced": {"comparisons": [{"class_title": "s1", "venue": "kalshi", "delta": 0.1}]},
    }
    out = at_a_glance_assembler.assemble(content_by_name)
    facts = out["facts"]
    assert len(facts) == 1
    assert facts[0]["section"] == "surfaced"


# -- ALL_SECTIONS ordering -----------------------------------------------


def test_at_a_glance_first_in_all_sections() -> None:
    """at_a_glance is the first body section."""
    assert ALL_SECTIONS[0] == "at_a_glance"


# -- generator integration ----------------------------------------------


def test_generator_runs_at_a_glance_after_other_sections(store: DuckDBStore) -> None:
    """The at_a_glance section runs and produces empty content on a clean store."""
    cfg = ReportConfig(enabled_sections=("at_a_glance", "cross_venue", "surfaced"))
    result = generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        config=cfg,
        quiet=True,
        now=datetime(2026, 5, 15, 14, tzinfo=UTC),
    )
    # at_a_glance ran but with no upstream data, the section is empty.
    assert "at_a_glance" in result.sections_rendered
    # The section appears in the rendered text with the label.
    assert "AT A GLANCE" in result.rendered_terminal_text


# -- renderers ----------------------------------------------------------


def test_terminal_render_emits_key_value_lines() -> None:
    from razor_rooster.report_generator.renderer.terminal import (
        _render_at_a_glance,
    )

    content = {
        "type": "at_a_glance",
        "facts": [
            {
                "label": "top cross-venue spread",
                "value": "CPI 2.5% at 1500 bps (polymarket vs kalshi)",
                "section": "cross_venue",
            },
            {
                "label": "top surfaced comparison",
                "value": "Top class (polymarket) delta +0.123",
                "section": "surfaced",
            },
        ],
    }
    out = _render_at_a_glance(content)
    assert "top cross-venue spread:" in out
    assert "1500 bps" in out
    assert "top surfaced comparison:" in out
    assert "+0.123" in out


def test_terminal_render_empty_returns_empty_string() -> None:
    from razor_rooster.report_generator.renderer.terminal import (
        _render_at_a_glance,
    )

    assert _render_at_a_glance({"facts": []}) == ""


def test_markdown_render_emits_definition_list() -> None:
    from razor_rooster.report_generator.renderer.markdown import (
        _render_at_a_glance,
    )

    content = {
        "type": "at_a_glance",
        "facts": [
            {"label": "top spread", "value": "CPI at 1500 bps", "section": "cross_venue"},
        ],
    }
    out = _render_at_a_glance(content)
    assert "- **top spread**: CPI at 1500 bps" in out


def test_html_render_emits_dl_dt_dd() -> None:
    from razor_rooster.report_generator.renderer.html import _render_at_a_glance

    content = {
        "type": "at_a_glance",
        "facts": [
            {"label": "top spread", "value": "CPI at 1500 bps", "section": "cross_venue"},
        ],
    }
    out = _render_at_a_glance(content)
    assert "<dl>" in out
    assert "<dt>top spread</dt>" in out
    assert "<dd>CPI at 1500 bps</dd>" in out
    assert "</dl>" in out


# -- linter compatibility (the high-bar case) ----------------------------


def test_assembler_output_passes_linter() -> None:
    """A representative assembler output passes the shared linter."""
    content_by_name = {
        "cross_venue": {
            "items": [
                {
                    "class_title": "CPI 2.5%",
                    "spread_bps": 1500,
                    "venue_prices": [{"venue": "polymarket"}, {"venue": "kalshi"}],
                },
            ],
        },
        "surfaced": {
            "comparisons": [{"class_title": "Top class", "venue": "polymarket", "delta": 0.123}],
        },
    }
    out = at_a_glance_assembler.assemble(content_by_name)
    from razor_rooster.report_generator.renderer.terminal import (
        _render_at_a_glance,
    )

    rendered = _render_at_a_glance(out)
    # Linter should pass.
    check_text(rendered)


def test_editorial_phrases_in_content_trigger_linter() -> None:
    """If a class title or value contains an editorial phrase, the rendered
    output triggers the linter — this defends against drift entering via
    operator-supplied data."""
    from razor_rooster.report_generator.renderer.terminal import (
        _render_at_a_glance,
    )

    # Adversarial content: a class title contains "key takeaway" (newly
    # forbidden in v0.45.0).
    content = {
        "type": "at_a_glance",
        "facts": [
            {
                "label": "top",
                "value": "key takeaway: this is a class title",
                "section": "cross_venue",
            },
        ],
    }
    rendered = _render_at_a_glance(content)
    with pytest.raises(ImperativeLanguageDetected):
        check_text(rendered)


def test_new_editorial_phrases_in_forbidden_list() -> None:
    """The v0.45.0 editorial phrases are present in the catalog."""
    from razor_rooster.position_engine.frame.linter import LinterCatalog

    catalog = LinterCatalog.from_yaml()
    phrases = {p.lower() for p in catalog.phrases}
    assert "particularly notable" in phrases
    assert "key takeaway" in phrases
    assert "you might want to" in phrases
    assert "worth a look" in phrases
