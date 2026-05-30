"""Tests for the recent-tuning report section (T-RG-COMPAT-RECENT-001 v0.44.0).

Covers:
- Empty input → empty section.
- Tuning log entries since the cycle window are surfaced newest-first.
- Older entries are excluded.
- Section is opt-in via enabled_sections.
- Renderers (terminal + markdown) emit the entries cleanly.
- Output passes the imperative-language linter.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.position_engine.frame.linter import check_text
from razor_rooster.report_generator.engines.section_assemblers import (
    recent_tuning as recent_tuning_assembler,
)
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import (
    persist_tuning_log_entry,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "recent_tuning.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_report_generator_migrations(c)
    with store.connection() as c:
        yield c


def _seed(
    conn: duckdb.DuckDBPyConnection,
    *,
    log_id: str,
    applied_at: datetime,
    note: str | None = None,
) -> None:
    persist_tuning_log_entry(
        conn,
        log_id=log_id,
        applied_at=applied_at,
        measurement_kind="cross_venue_spread_bps",
        knob="cross_venue_spread_bps",
        previous_value=500.0,
        new_value=750.0,
        target_percentile=0.70,
        backup_path="/tmp/report.yaml.bak.x",
        note=note,
    )


# -- assembler ------------------------------------------------------------


def test_empty_table_returns_empty_section(conn: duckdb.DuckDBPyConnection) -> None:
    out = recent_tuning_assembler.assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert out["type"] == "recent_tuning"
    assert out["entries"] == []


def test_entries_within_window_are_surfaced(conn: duckdb.DuckDBPyConnection) -> None:
    base = datetime(2026, 5, 15, tzinfo=UTC)
    _seed(conn, log_id="log-1", applied_at=base - timedelta(hours=12))
    out = recent_tuning_assembler.assemble(
        conn,
        since_ts=base - timedelta(days=1),
        until_ts=base,
    )
    assert len(out["entries"]) == 1
    assert out["entries"][0]["log_id"] == "log-1"
    assert out["entries"][0]["measurement_kind"] == "cross_venue_spread_bps"


def test_entries_before_window_are_excluded(conn: duckdb.DuckDBPyConnection) -> None:
    base = datetime(2026, 5, 15, tzinfo=UTC)
    # One inside, one outside.
    _seed(conn, log_id="log-old", applied_at=base - timedelta(days=10))
    _seed(conn, log_id="log-new", applied_at=base - timedelta(hours=12))
    out = recent_tuning_assembler.assemble(
        conn,
        since_ts=base - timedelta(days=1),
        until_ts=base,
    )
    log_ids = [e["log_id"] for e in out["entries"]]
    assert log_ids == ["log-new"]


def test_entries_newest_first(conn: duckdb.DuckDBPyConnection) -> None:
    base = datetime(2026, 5, 15, tzinfo=UTC)
    _seed(conn, log_id="log-a", applied_at=base - timedelta(hours=4))
    _seed(conn, log_id="log-b", applied_at=base - timedelta(hours=1))
    _seed(conn, log_id="log-c", applied_at=base - timedelta(hours=8))
    out = recent_tuning_assembler.assemble(
        conn,
        since_ts=base - timedelta(days=1),
        until_ts=base,
    )
    assert [e["log_id"] for e in out["entries"]] == ["log-b", "log-a", "log-c"]


def test_assembler_robust_to_missing_table(tmp_path: Path) -> None:
    """When the tuning-log table doesn't exist (pre-m7003), return empty."""
    # Create a connection without running the m7003 migration.
    db_path = tmp_path / "no_table.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        out = recent_tuning_assembler.assemble(
            conn,
            since_ts=datetime(2026, 5, 14, tzinfo=UTC),
            until_ts=datetime(2026, 5, 15, tzinfo=UTC),
        )
        assert out["entries"] == []
    finally:
        conn.close()


# -- terminal rendering ---------------------------------------------------


def test_terminal_render_emits_entry_summary() -> None:
    from razor_rooster.report_generator.renderer.terminal import (
        _render_recent_tuning,
    )

    content = {
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
    }
    out = _render_recent_tuning(content)
    assert "cross_venue_spread_bps" in out
    assert "500" in out
    assert "750" in out
    assert "p70" in out
    assert "bumped after p70 jumped" in out


def test_terminal_render_handles_missing_optional_fields() -> None:
    from razor_rooster.report_generator.renderer.terminal import (
        _render_recent_tuning,
    )

    content = {
        "type": "recent_tuning",
        "entries": [
            {
                "log_id": "log-2",
                "applied_at": datetime(2026, 5, 15, 12, tzinfo=UTC),
                "measurement_kind": "brier_per_sector",
                "knob": "brier_miscalibration",
                "previous_value": None,
                "new_value": 0.18,
                "target_percentile": None,
                "note": None,
            },
        ],
    }
    out = _render_recent_tuning(content)
    assert "(unset)" in out
    assert "brier_miscalibration" in out


def test_terminal_render_empty_returns_empty_string() -> None:
    from razor_rooster.report_generator.renderer.terminal import (
        _render_recent_tuning,
    )

    assert _render_recent_tuning({"entries": []}) == ""


# -- markdown rendering ----------------------------------------------------


def test_markdown_render_emits_table() -> None:
    from razor_rooster.report_generator.renderer.markdown import (
        _render_recent_tuning,
    )

    content = {
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
    }
    out = _render_recent_tuning(content)
    assert "| Applied at | Kind | Knob | Previous → New | Note |" in out
    assert "cross_venue_spread_bps" in out
    assert "500" in out
    assert "750" in out
    assert "p70" in out


def test_markdown_render_empty_returns_empty_string() -> None:
    from razor_rooster.report_generator.renderer.markdown import (
        _render_recent_tuning,
    )

    assert _render_recent_tuning({"entries": []}) == ""


# -- linter compatibility -------------------------------------------------


def test_terminal_render_passes_linter() -> None:
    from razor_rooster.report_generator.renderer.terminal import (
        _render_recent_tuning,
    )

    content = {
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
    }
    check_text(_render_recent_tuning(content))


def test_markdown_render_passes_linter() -> None:
    from razor_rooster.report_generator.renderer.markdown import (
        _render_recent_tuning,
    )

    content = {
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
    }
    check_text(_render_recent_tuning(content))


# -- ALL_SECTIONS ordering ------------------------------------------------


def test_recent_tuning_in_all_sections_after_system_health() -> None:
    from razor_rooster.report_generator.config.loader import ALL_SECTIONS

    sh_idx = ALL_SECTIONS.index("system_health")
    rt_idx = ALL_SECTIONS.index("recent_tuning")
    surfaced_idx = ALL_SECTIONS.index("surfaced")
    assert sh_idx < rt_idx < surfaced_idx


# -- generator integration ------------------------------------------------


def test_generator_renders_recent_tuning_when_enabled(tmp_path: Path) -> None:
    """A full generate() with recent_tuning enabled emits the section."""
    from razor_rooster.data_ingest.persistence.migrations import (
        run_pending_migrations as run_pending_data_ingest_migrations,
    )
    from razor_rooster.mispricing_detector.persistence.migrations import (
        run_pending_mispricing_migrations,
    )
    from razor_rooster.pattern_library.persistence.migrations import (
        run_pending_pattern_library_migrations,
    )
    from razor_rooster.report_generator.config.loader import ReportConfig
    from razor_rooster.report_generator.engines.generator import generate
    from razor_rooster.signal_scanner.persistence.migrations import (
        run_pending_signal_scanner_migrations,
    )

    db_path = tmp_path / "rt_full.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_report_generator_migrations(c)
        # Seed a tuning-log entry.
        _seed(
            c,
            log_id="log-1",
            applied_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
            note="bumped after p70",
        )
    cfg = ReportConfig(enabled_sections=("recent_tuning",))
    result = generate(
        store,
        since=datetime(2026, 5, 15, tzinfo=UTC),
        config=cfg,
        quiet=True,
        now=datetime(2026, 5, 15, 14, tzinfo=UTC),
    )
    assert "recent_tuning" in result.sections_rendered
    assert "RECENT THRESHOLD CHANGES" in result.rendered_terminal_text
    assert "cross_venue_spread_bps" in result.rendered_terminal_text
