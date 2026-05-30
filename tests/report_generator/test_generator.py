"""T-RG-040 — generator orchestrator tests."""

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
from razor_rooster.monitor.persistence.migrations import (
    run_pending_monitor_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.position_engine.frame.linter import (
    ImperativeLanguageDetected,
)
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.report_generator.config.loader import (
    ReportConfig,
)
from razor_rooster.report_generator.engines.generator import generate
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import (
    list_reports,
    query_last_report,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "rg_generator.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_polymarket_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_position_engine_migrations(c)
        run_pending_monitor_migrations(c)
        run_pending_report_generator_migrations(c)
    yield s
    s.close()


def test_generate_empty_report(store: DuckDBStore) -> None:
    """Empty cycle: every section renders 'nothing to report'."""
    result = generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert result.report_id
    assert result.duration_seconds is not None
    assert result.duration_seconds >= 0
    text = result.rendered_terminal_text
    assert "RAZOR-ROOSTER REPORT" in text
    assert "DISCLAIMER:" in text
    assert "No comparisons surfaced" in text
    assert "No new resolutions" in text


def test_generate_persists_report(store: DuckDBStore) -> None:
    result = generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 15, tzinfo=UTC),
    )
    with store.connection() as conn:
        latest = query_last_report(conn)
    assert latest is not None
    assert latest.report_id == result.report_id
    assert latest.rendered_terminal_text == result.rendered_terminal_text


def test_generate_writes_markdown(store: DuckDBStore, tmp_path: Path) -> None:
    md_path = tmp_path / "out" / "report.md"
    result = generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        markdown_path=md_path,
        quiet=True,
        now=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert md_path.exists()
    on_disk = md_path.read_text(encoding="utf-8")
    assert on_disk == result.rendered_markdown_text
    assert on_disk.startswith("# Razor-Rooster Report")
    # Persisted record retains the markdown path.
    with store.connection() as conn:
        latest = query_last_report(conn)
    assert latest is not None
    assert latest.markdown_path == str(md_path)


def test_generate_resolves_since_from_prior_report(
    store: DuckDBStore,
) -> None:
    """When --since is omitted and a prior report exists, use its
    generated_at.
    """
    first = generate(
        store,
        since=datetime(2026, 5, 13, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 14, 14, tzinfo=UTC),
    )
    # Run again without explicit since.
    second = generate(
        store,
        quiet=True,
        now=datetime(2026, 5, 15, 14, tzinfo=UTC),
    )
    assert second.since_ts == first.generated_at


def test_generate_resolves_since_to_24h_when_no_prior(
    store: DuckDBStore,
) -> None:
    """When no prior report, default since is now - 24h."""
    now = datetime(2026, 5, 15, 14, tzinfo=UTC)
    result = generate(store, quiet=True, now=now)
    assert result.since_ts == now - timedelta(days=1)


def test_generate_disabled_section_appears_in_header(
    store: DuckDBStore,
) -> None:
    cfg = ReportConfig(
        enabled_sections=("system_health",),  # only one body section
        verbosity={},
    )
    result = generate(
        store,
        config=cfg,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 15, tzinfo=UTC),
    )
    text = result.rendered_terminal_text
    assert "Disabled sections (config):" in text
    # The disabled body sections should not appear as section headers.
    assert "SURFACED COMPARISONS" not in text
    assert "CALIBRATION LOG" not in text
    # The enabled one does.
    assert "SYSTEM HEALTH" in text


def test_generate_section_failure_is_isolated(
    store: DuckDBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a section assembler raises, the report still renders."""
    from razor_rooster.report_generator.engines.section_assemblers import (
        surfaced as surfaced_assembler,
    )

    def boom(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("synthetic surfaced failure")

    monkeypatch.setattr(surfaced_assembler, "assemble", boom)

    result = generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 15, tzinfo=UTC),
    )
    text = result.rendered_terminal_text
    assert "section error: RuntimeError: synthetic surfaced failure" in text
    # Other sections still rendered.
    assert "CALIBRATION LOG" in text
    # Sections_failed records the failure.
    assert any(f["section"] == "surfaced" for f in result.sections_failed)


def test_generate_linter_rejection_prevents_persistence(
    store: DuckDBStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A linter rejection raises and the report row is not written."""
    from razor_rooster.report_generator.engines.section_assemblers import (
        footer as footer_assembler,
    )

    monkeypatch.setattr(
        footer_assembler,
        "load_disclaimer_text",
        lambda *args, **kwargs: "I recommend you take this position now.",
    )

    with pytest.raises(ImperativeLanguageDetected):
        generate(
            store,
            since=datetime(2026, 5, 14, tzinfo=UTC),
            quiet=True,
            now=datetime(2026, 5, 15, tzinfo=UTC),
        )

    with store.connection() as conn:
        latest = query_last_report(conn)
    assert latest is None  # nothing persisted


def test_generate_quiet_does_not_print(
    store: DuckDBStore, capsys: pytest.CaptureFixture[str]
) -> None:
    generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 15, tzinfo=UTC),
    )
    captured = capsys.readouterr()
    assert captured.out == ""


def test_generate_loud_prints_terminal_text(
    store: DuckDBStore, capsys: pytest.CaptureFixture[str]
) -> None:
    result = generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=False,
        now=datetime(2026, 5, 15, tzinfo=UTC),
    )
    captured = capsys.readouterr()
    assert "RAZOR-ROOSTER REPORT" in captured.out
    assert captured.out.startswith(result.rendered_terminal_text.split("\n", 1)[0])


def test_multiple_reports_accumulate(store: DuckDBStore) -> None:
    for offset in range(3):
        generate(
            store,
            since=datetime(2026, 5, 13 + offset, tzinfo=UTC),
            quiet=True,
            now=datetime(2026, 5, 14 + offset, tzinfo=UTC),
        )
    with store.connection() as conn:
        all_reports = list_reports(conn)
    assert len(all_reports) == 3


def test_generate_records_disclaimer_version_hash(
    store: DuckDBStore,
) -> None:
    result = generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert len(result.disclaimer_version_hash) == 64
    with store.connection() as conn:
        latest = query_last_report(conn)
    assert latest is not None
    assert latest.disclaimer_version_hash == result.disclaimer_version_hash
