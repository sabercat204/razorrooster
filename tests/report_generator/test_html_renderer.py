"""Tests for the HTML renderer (T-RG-COMPAT-HTML-001 v0.44.0).

Covers:
- HTML structure: doctype, html/head/body, viewport, charset.
- Self-containment: no external assets (no http://, no https://,
  no <script> tags, no <link rel> to external resources).
- Section dispatch: each section's content type renders cleanly.
- HTML escaping: special characters in operator text don't break output.
- Generator integration: --html PATH writes the file and persists.
- Linter compatibility: the rendered HTML passes the imperative-language linter.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
from razor_rooster.position_engine.frame.linter import check_text
from razor_rooster.report_generator.engines.generator import generate
from razor_rooster.report_generator.models import SectionContent
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import (
    query_last_report,
)
from razor_rooster.report_generator.renderer import html as html_renderer
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "html.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_report_generator_migrations(c)
    yield s
    s.close()


# -- structure ------------------------------------------------------------


def test_render_emits_full_html_document() -> None:
    html_text = html_renderer.render(
        header={"cycle_date": "2026-05-15"},
        body_sections=(),
        footer={},
    )
    assert html_text.startswith("<!DOCTYPE html>")
    assert '<html lang="en">' in html_text
    assert '<meta charset="utf-8">' in html_text
    assert '<meta name="viewport"' in html_text
    assert "</body>" in html_text
    assert "</html>" in html_text


def test_render_includes_inline_css() -> None:
    html_text = html_renderer.render(
        header={"cycle_date": "2026-05-15"},
        body_sections=(),
        footer={},
    )
    assert "<style>" in html_text
    assert "</style>" in html_text
    # Dark mode styles bundled inline.
    assert "prefers-color-scheme: dark" in html_text


def test_render_has_no_external_resources() -> None:
    """Self-contained: no script/link/img tags pointing outside the document."""
    html_text = html_renderer.render(
        header={"cycle_date": "2026-05-15"},
        body_sections=(),
        footer={},
    )
    # No <script> tags.
    assert "<script" not in html_text
    # No external stylesheets or images.
    assert "<link " not in html_text
    assert "http://" not in html_text
    assert "https://" not in html_text


def test_render_escapes_special_characters_in_header() -> None:
    """Operator text containing < > & doesn't break the HTML."""
    html_text = html_renderer.render(
        header={
            "cycle_date": "2026-05-15",
            "report_id": "abc<def>&ghi",
        },
        body_sections=(),
        footer={},
    )
    # The raw < and > shouldn't appear inside the report_id rendering.
    # They should be escaped as &lt; and &gt;.
    assert "abc&lt;def&gt;&amp;ghi" in html_text


# -- per-section rendering ------------------------------------------------


def test_render_system_health_section() -> None:
    html_text = html_renderer.render(
        header={"cycle_date": "2026-05-15"},
        body_sections=(
            SectionContent(
                name="system_health",
                content={
                    "stale_sources": [{"source_id": "fred", "days_stale": 3}],
                    "errored_subsystems": [],
                    "suppressed_breakdown": {"sub_edge_threshold": 5},
                },
            ),
        ),
        footer={},
    )
    assert "<h2>System Health</h2>" in html_text
    assert "<code>fred</code>" in html_text
    assert "<code>sub_edge_threshold</code>" in html_text


def test_render_recent_tuning_section() -> None:
    html_text = html_renderer.render(
        header={"cycle_date": "2026-05-15"},
        body_sections=(
            SectionContent(
                name="recent_tuning",
                content={
                    "type": "recent_tuning",
                    "entries": [
                        {
                            "log_id": "log-1",
                            "applied_at": datetime(2026, 5, 15, 12, tzinfo=UTC),
                            "measurement_kind": "cross_venue_spread_bps",
                            "knob": "cross_venue_spread_bps",
                            "previous_value": 500.0,
                            "new_value": 750.0,
                            "target_percentile": 0.70,
                            "note": "bumped after p70",
                        },
                    ],
                },
            ),
        ),
        footer={},
    )
    assert "<h2>Recent Threshold Changes</h2>" in html_text
    assert "<code>cross_venue_spread_bps</code>" in html_text
    assert "(p70)" in html_text


def test_render_calibration_section() -> None:
    html_text = html_renderer.render(
        header={"cycle_date": "2026-05-15"},
        body_sections=(
            SectionContent(
                name="calibration",
                content={
                    "type": "calibration",
                    "resolutions": [
                        {
                            "class_title": "macro1",
                            "venue": "kalshi",
                            "resolution_outcome": "yes",
                            "model_probability": 0.65,
                            "days_to_resolution": 7,
                            "verdict_text": "Model said 0.65 → resolved YES.",
                        },
                    ],
                    "sector_brier_scores": [
                        {
                            "sector": "macroeconomic",
                            "brier_score": 0.18,
                            "n_resolutions": 5,
                            "window_days": 90,
                            "miscalibrated": False,
                        },
                    ],
                },
            ),
        ),
        footer={},
    )
    assert "<h2>Calibration Log</h2>" in html_text
    assert "macro1" in html_text
    assert "kalshi" in html_text
    assert "Per-sector Brier scores" in html_text


def test_render_reliability_section_includes_chart() -> None:
    html_text = html_renderer.render(
        header={"cycle_date": "2026-05-15"},
        body_sections=(
            SectionContent(
                name="reliability",
                content={
                    "type": "reliability",
                    "min_resolutions_per_bin": 5,
                    "sectors": [
                        {
                            "sector": "macroeconomic",
                            "n_resolutions": 6,
                            "window_days": 90,
                            "bins": [
                                {
                                    "bin_lo": 0.4,
                                    "bin_hi": 0.5,
                                    "n": 6,
                                    "mean_predicted": 0.45,
                                    "empirical_rate": 0.50,
                                    "calibration_gap": 0.05,
                                    "sparse": False,
                                },
                            ],
                        },
                    ],
                },
            ),
        ),
        footer={},
    )
    assert "<h2>Reliability Diagram</h2>" in html_text
    # Calibration chart wrapped in <pre> with the diagonal glyphs.
    assert "<pre>" in html_text
    assert "perfect calibration" in html_text


# -- section error path --------------------------------------------------


def test_render_section_error_renders_warn_block() -> None:
    html_text = html_renderer.render(
        header={"cycle_date": "2026-05-15"},
        body_sections=(
            SectionContent(
                name="surfaced",
                content=None,
                error="RuntimeError: assembler blew up",
            ),
        ),
        footer={},
    )
    assert "Section error" in html_text
    assert "warn-list" in html_text


# -- empty section message -----------------------------------------------


def test_render_empty_section_emits_empty_message() -> None:
    html_text = html_renderer.render(
        header={"cycle_date": "2026-05-15"},
        body_sections=(
            SectionContent(
                name="cross_venue",
                content={"type": "cross_venue", "items": []},
            ),
        ),
        footer={},
    )
    assert "No cross-venue disagreements this cycle." in html_text
    assert "class='empty'" in html_text


# -- footer disclaimer ---------------------------------------------------


def test_render_footer_includes_disclaimer() -> None:
    html_text = html_renderer.render(
        header={"cycle_date": "2026-05-15"},
        body_sections=(),
        footer={
            "disclaimer_text": (
                "This report is decision-support analysis. The system surfaces "
                "patterns; it does not place trades."
            ),
            "system_version": "0.44.0",
            "report_id": "rpt-xyz",
            "completed_at": datetime(2026, 5, 15, 14, 30, tzinfo=UTC),
        },
    )
    assert "decision-support analysis" in html_text
    assert "version 0.44.0" in html_text
    assert "<code>rpt-xyz</code>" in html_text


# -- linter compatibility -----------------------------------------------


def test_html_render_passes_imperative_linter() -> None:
    """The HTML output must not contain forbidden imperative phrases."""
    html_text = html_renderer.render(
        header={"cycle_date": "2026-05-15"},
        body_sections=(
            SectionContent(
                name="recent_tuning",
                content={
                    "type": "recent_tuning",
                    "entries": [
                        {
                            "log_id": "log-1",
                            "applied_at": datetime(2026, 5, 15, 12, tzinfo=UTC),
                            "measurement_kind": "cross_venue_spread_bps",
                            "knob": "cross_venue_spread_bps",
                            "previous_value": 500.0,
                            "new_value": 750.0,
                            "target_percentile": 0.70,
                            "note": "bumped after p70 jumped",
                        },
                    ],
                },
            ),
        ),
        footer={
            "disclaimer_text": "This report is decision-support analysis.",
        },
    )
    check_text(html_text)


# -- generator integration ----------------------------------------------


def test_generator_writes_html_when_path_supplied(store: DuckDBStore, tmp_path: Path) -> None:
    html_path = tmp_path / "out" / "report.html"
    result = generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        html_path=html_path,
        quiet=True,
        now=datetime(2026, 5, 15, 14, tzinfo=UTC),
    )
    assert result.html_path == str(html_path)
    assert html_path.exists()
    on_disk = html_path.read_text(encoding="utf-8")
    assert on_disk.startswith("<!DOCTYPE html>")
    assert result.rendered_html_text == on_disk


def test_generator_persists_html_text(store: DuckDBStore, tmp_path: Path) -> None:
    """rendered_html_text round-trips through report_log."""
    html_path = tmp_path / "out" / "report.html"
    generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        html_path=html_path,
        quiet=True,
        now=datetime(2026, 5, 15, 14, tzinfo=UTC),
    )
    with store.connection() as c:
        record = query_last_report(c)
    assert record is not None
    assert record.rendered_html_text is not None
    assert record.rendered_html_text.startswith("<!DOCTYPE html>")
    assert record.html_path == str(html_path)


def test_generator_html_path_optional(store: DuckDBStore) -> None:
    """Without --html, no HTML is written or persisted."""
    result = generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 15, 14, tzinfo=UTC),
    )
    assert result.rendered_html_text is None
    assert result.html_path is None


def test_cli_generate_html_flag(tmp_path: Path) -> None:
    """The --html CLI flag wires through to generate()."""
    from click.testing import CliRunner

    from razor_rooster.report_generator.cli import report as report_cli

    db_path = tmp_path / "cli.duckdb"
    html_path = tmp_path / "out" / "report.html"
    s = DuckDBStore(db_path)
    with s.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_report_generator_migrations(c)
    s.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "generate",
            "--db",
            str(db_path),
            "--html",
            str(html_path),
            "--quiet",
            "--since",
            (datetime(2026, 5, 15, tzinfo=UTC) - timedelta(days=1)).isoformat(),
        ],
    )
    assert result.exit_code == 0
    assert html_path.exists()
    assert html_path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
