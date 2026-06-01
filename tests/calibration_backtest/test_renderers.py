"""T-CB-029 / T-CB-030 — renderer tests.

Covers the four renderers introduced in Phase 5: terminal, markdown,
HTML (with native SVG embed), and JSON. Also exercises the framing
linter integration (``ImperativeLanguageDetected`` from a forbidden
phrase introduced into a sector name) and the HTML-escape contract
for operator-supplied strings.

The fixtures synthesise a fully-populated :class:`BacktestRun` with a
2-bin reliability diagram so the SVG renderer covers both the
non-empty-bin and empty-bin code paths. Determinism is asserted by
re-rendering the same input twice and comparing byte-for-byte.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from razor_rooster.calibration_backtest.frame import DISCLAIMER
from razor_rooster.calibration_backtest.models import (
    BacktestRun,
    BacktestStatus,
    ReliabilityBin,
    ReliabilityDiagram,
)
from razor_rooster.calibration_backtest.renderers import (
    render_html,
    render_json,
    render_markdown,
    render_reliability_svg,
    render_terminal,
)
from razor_rooster.position_engine.frame.linter import ImperativeLanguageDetected

# T-CB-022 parity reference. Imported here as well so the test file's
# bin-tuple shape matches the production scoring engine 1:1.
from razor_rooster.report_generator.engines.section_assemblers.reliability import (
    _equal_width_bins as _rg_equal_width_bins,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _diagram_two_bins() -> ReliabilityDiagram:
    """Build a 2-bin diagram exercising both populated and empty rows."""

    return ReliabilityDiagram(
        bin_count=2,
        bins=(
            ReliabilityBin(
                lower_p=0.0,
                upper_p=0.5,
                count=3,
                mean_predicted_p=0.25,
                empirical_rate=0.33,
            ),
            ReliabilityBin(
                lower_p=0.5,
                upper_p=1.0,
                count=0,
                mean_predicted_p=None,
                empirical_rate=None,
            ),
        ),
    )


def _summary_payload(*, sector: str = "public_health") -> dict[str, Any]:
    """Build a deterministic ``summary_json`` payload for a backtest run.

    Mirrors :meth:`ScoreSummary.as_mapping` so the renderer paths see
    the exact shape the persistence layer writes.
    """

    return {
        "fallback_polarity_count": 1,
        "fallback_polarity_rate": 0.125,
        "overall_brier": 0.21,
        "per_class_brier": {"flu_h2h": 0.22},
        "per_sector_brier": {sector: 0.18},
        "reliability_diagrams": {
            sector: {
                "bin_count": 2,
                "bins": [
                    {
                        "count": 3,
                        "empirical_rate": 0.33,
                        "lower_p": 0.0,
                        "mean_predicted_p": 0.25,
                        "upper_p": 0.5,
                    },
                    {
                        "count": 0,
                        "empirical_rate": None,
                        "lower_p": 0.5,
                        "mean_predicted_p": None,
                        "upper_p": 1.0,
                    },
                ],
            }
        },
        "zero_resolutions_classes": [],
        "zero_resolutions_sectors": [],
    }


def _make_run(
    *,
    sector: str = "public_health",
    overall_brier: float | None = 0.21,
    fallback_count: int = 1,
    summary: dict[str, Any] | None = None,
) -> BacktestRun:
    return BacktestRun(
        run_id="abc123def456ghi789",
        since_ts=datetime(2024, 1, 1, tzinfo=UTC),
        until_ts=datetime(2024, 6, 1, tzinfo=UTC),
        lag_days=7,
        class_ids=("flu_h2h",),
        sectors=(sector,),
        venues=("polymarket",),
        library_version=1,
        system_revision="deadbeefcafef00d1122",
        started_at=datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC),
        completed_at=datetime(2024, 6, 1, 0, 5, 0, tzinfo=UTC),
        status=BacktestStatus.COMPLETE,
        error_summary=None,
        predictions_total=10,
        predictions_scored=8,
        predictions_skipped=2,
        overall_brier=overall_brier,
        summary_json=summary if summary is not None else _summary_payload(sector=sector),
        bin_count_global=10,
        bin_count_per_sector={sector: 5},
        fallback_polarity_count=fallback_count,
        allow_recent=False,
        disclaimer_version="v1",
    )


# ---------------------------------------------------------------------------
# Terminal renderer
# ---------------------------------------------------------------------------


def test_terminal_renders_disclaimer_and_run_header() -> None:
    """Terminal output carries the disclaimer, header keys, and counts."""

    run = _make_run()
    out = render_terminal(run)
    assert "Disclaimer:" in out
    assert "Run header" in out
    assert run.run_id[:12] in out
    assert "lag_days         : 7" in out
    assert "predictions_total" not in out  # we use plain 'total' label
    assert "total            : 10" in out
    assert "scored           : 8" in out
    assert "skipped          : 2" in out
    assert "overall_brier    : 0.2100" in out


def test_terminal_includes_per_sector_and_per_class_tables() -> None:
    """Terminal output renders the Brier breakdown tables."""

    run = _make_run()
    out = render_terminal(run)
    assert "Per-sector Brier" in out
    assert "public_health" in out
    assert "0.1800" in out
    assert "Per-class Brier" in out
    assert "flu_h2h" in out
    assert "0.2200" in out


def test_terminal_warns_on_high_fallback_rate() -> None:
    """Fallback rate > 5% surfaces an explicit operator note."""

    summary = _summary_payload()
    summary["fallback_polarity_rate"] = 0.15
    run = _make_run(fallback_count=4, summary=summary)
    out = render_terminal(run)
    assert "fallback rate exceeds 5%" in out


def test_terminal_no_warning_when_fallback_rate_low() -> None:
    """Fallback rate ≤ 5% does not surface the warn note."""

    summary = _summary_payload()
    summary["fallback_polarity_rate"] = 0.01
    run = _make_run(fallback_count=0, summary=summary)
    out = render_terminal(run)
    assert "fallback rate exceeds 5%" not in out


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def test_markdown_renders_tables_and_diagram_block() -> None:
    """Markdown output uses GFM tables and a fenced code block per diagram."""

    run = _make_run()
    out = render_markdown(run)
    assert out.startswith("# Calibration Backtest")
    assert "## Disclaimer" in out
    assert "| field | value |" in out
    assert "## Per-sector Brier" in out
    assert "| sector | brier |" in out
    assert "| public_health | 0.1800 |" in out
    assert "## Per-class Brier" in out
    assert "| class_id | brier |" in out
    assert "| flu_h2h | 0.2200 |" in out
    assert "## Reliability diagrams" in out
    assert "```text" in out
    assert "(empty)" in out  # the second bin has count=0


def test_markdown_does_not_embed_svg() -> None:
    """Markdown intentionally omits SVG (renderer compatibility)."""

    run = _make_run()
    out = render_markdown(run)
    assert "<svg" not in out


# ---------------------------------------------------------------------------
# HTML renderer + native SVG
# ---------------------------------------------------------------------------


def test_html_renders_full_document_with_inline_svg() -> None:
    """HTML output is a complete document with inline SVG diagrams."""

    run = _make_run()
    out = render_html(run)
    assert out.startswith("<!DOCTYPE html>")
    assert "<style>" in out
    assert 'class="disclaimer"' in out
    assert 'class="footer-note"' in out
    assert "<svg" in out
    assert "viewBox=" in out
    assert "</svg>" in out


def test_html_escapes_operator_supplied_sector_name() -> None:
    """Sector names are HTML-escaped (defends against ``<script>`` smuggling)."""

    summary = _summary_payload(sector="<script>")
    run = _make_run(sector="<script>", summary=summary)
    out = render_html(run)
    assert "&lt;script&gt;" in out
    assert "<script>public_health" not in out


def test_html_warn_banner_fires_above_threshold() -> None:
    """Fallback rate > 5% surfaces a warn-banner section in HTML."""

    summary = _summary_payload()
    summary["fallback_polarity_rate"] = 0.15
    run = _make_run(fallback_count=4, summary=summary)
    out = render_html(run)
    assert "warn-banner" in out
    assert "exceeds 5%" in out


# ---------------------------------------------------------------------------
# Native SVG helper
# ---------------------------------------------------------------------------


def test_render_reliability_svg_emits_valid_markup() -> None:
    """SVG output carries the xmlns + viewBox + closing tag contract."""

    diagram = _diagram_two_bins()
    svg = render_reliability_svg(diagram, width=240, height=240, padding=24)
    assert svg.startswith("<svg ")
    assert svg.endswith("</svg>")
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg
    assert 'viewBox="0 0 240 240"' in svg
    # 1 axis (bottom) + 1 axis (left) + 1 reference + 1 bar + 1 marker + 1 sparse rect = 6 lines/rects/circles.
    assert svg.count("<line ") == 3
    # bin 0 has count=3 → bar + marker.
    assert svg.count('class="bar"') == 1
    assert svg.count('class="marker"') == 1
    # bin 1 has count=0 → sparse placeholder.
    assert svg.count('class="sparse"') == 1


def test_render_reliability_svg_bin_tuple_count_matches_input() -> None:
    """Total bin-derived shapes equal ``len(diagram.bins)`` per shape kind.

    Each bin contributes either a bar+marker pair (count > 0) or one
    sparse rect (count == 0). The combined count of those shapes equals
    ``len(diagram.bins)``, which the reliability binning engine
    parity-locks to ``_equal_width_bins(bin_count)`` (T-CB-022).
    """

    diagram = _diagram_two_bins()
    svg = render_reliability_svg(diagram)
    bar_count = svg.count('class="bar"')
    sparse_count = svg.count('class="sparse"')
    assert bar_count + sparse_count == len(diagram.bins)
    # And the bin-tuple parity reference itself returns 2 entries for bin_count=2.
    assert len(_rg_equal_width_bins(2)) == 2


def test_render_reliability_svg_escapes_label() -> None:
    """Operator-supplied sector labels are HTML-escaped before embedding."""

    diagram = _diagram_two_bins()
    svg = render_reliability_svg(diagram, sector_label="<script>")
    assert "&lt;script&gt;" in svg
    assert "<text" in svg


# ---------------------------------------------------------------------------
# JSON renderer
# ---------------------------------------------------------------------------


def test_render_json_round_trip_preserves_schema() -> None:
    """Loaded JSON carries every persisted field plus a disclaimer string."""

    run = _make_run()
    raw = render_json(run)
    payload = json.loads(raw)
    assert payload["run_id"] == run.run_id
    assert payload["since_ts"] == run.since_ts.isoformat()
    assert payload["until_ts"] == run.until_ts.isoformat()
    assert payload["lag_days"] == 7
    assert payload["library_version"] == 1
    assert payload["system_revision"] == run.system_revision
    assert payload["status"] == "complete"
    assert payload["predictions_total"] == 10
    assert payload["predictions_scored"] == 8
    assert payload["predictions_skipped"] == 2
    assert payload["overall_brier"] == 0.21
    assert payload["fallback_polarity_count"] == 1
    assert payload["fallback_polarity_rate"] == pytest.approx(0.125)
    assert payload["bin_count_global"] == 10
    assert payload["bin_count_per_sector"] == {"public_health": 5}
    assert payload["disclaimer"] == DISCLAIMER
    # Summary JSON travels intact.
    assert payload["summary_json"]["per_sector_brier"]["public_health"] == 0.18


def test_render_json_is_deterministic() -> None:
    """Identical inputs produce byte-identical output (sort_keys=True gate)."""

    run = _make_run()
    first = render_json(run)
    second = render_json(run)
    assert first == second
    # sort_keys=True forces alphabetic order at the top level.
    payload = json.loads(first)
    keys = list(payload.keys())
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Linter integration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("renderer", [render_terminal, render_markdown, render_html])
def test_renderers_reject_forbidden_phrase_in_sector(renderer: Any) -> None:
    """A known forbidden YAML phrase in a sector name trips the linter.

    The ``you should buy`` phrase is on the seed catalog. Embedding it
    into the sector name makes it appear in the rendered Brier table —
    each renderer must propagate :class:`ImperativeLanguageDetected`
    rather than ship the offending output.
    """

    bad_sector = "you should buy"
    summary = _summary_payload(sector=bad_sector)
    run = _make_run(sector=bad_sector, summary=summary)
    with pytest.raises(ImperativeLanguageDetected):
        renderer(run)


def test_render_json_bypasses_linter() -> None:
    """JSON output is consumed by tooling and does not run the framing linter."""

    bad_sector = "you should buy"
    summary = _summary_payload(sector=bad_sector)
    run = _make_run(sector=bad_sector, summary=summary)
    # Must not raise even though the sector name carries an imperative
    # phrase — the disclaimer field carries the framing instead.
    raw = render_json(run)
    payload = json.loads(raw)
    assert "disclaimer" in payload


# ---------------------------------------------------------------------------
# Cross-renderer disclaimer presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("renderer", [render_terminal, render_markdown, render_html])
def test_renderers_include_disclaimer(renderer: Any) -> None:
    """Each non-JSON renderer surfaces a recognisable slice of the disclaimer."""

    run = _make_run()
    out = renderer(run)
    # A unique-enough fragment from the disclaimer to anchor on.
    assert "decision-support calibration evidence" in out


def test_html_escapes_disclaimer_safely() -> None:
    """The disclaimer text is embedded via the HTML escape helper.

    The disclaimer carries no markup, so the escape is a no-op for the
    payload itself; this test guards against a future regression where
    a raw ``<`` or ``>`` slips into ``DISCLAIMER`` and breaks the
    document.
    """

    run = _make_run()
    out = render_html(run)
    # No bare ``<`` or ``>`` from the disclaimer should leak as a raw
    # entity. We assert via a regex that any ``<`` / ``>`` originated
    # from genuine HTML tags by rejecting the pattern ``< [a-z]`` with a
    # space (which would only appear if the disclaimer had embedded
    # angle brackets).
    assert not re.search(r"<\s+[a-zA-Z]", out)
