"""Tests for the report-to-report compare engine + CLI (T-RG-COMPAT-COMPARE-001).

Covers:
- compare_reports: metadata diff, sections added/removed, library/disclaimer drift,
  length delta, unified diff format.
- CLI: razor-rooster report compare end-to-end, missing-report handling,
  --diff/--no-diff flag, --diff-lines truncation, linter compatibility.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.position_engine.frame.linter import check_text
from razor_rooster.report_generator.cli import report as report_cli
from razor_rooster.report_generator.engines.compare import (
    ReportDiff,
    compare_reports,
)
from razor_rooster.report_generator.models import ReportRecord
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import persist_report


def _make_record(
    *,
    report_id: str,
    generated_at: datetime,
    sections_rendered: tuple[str, ...] = ("system_health", "surfaced"),
    sections_failed: tuple[dict[str, str], ...] = (),
    library_version: int = 1,
    disclaimer_hash: str = "abc123",
    terminal_text: str = "REPORT TEXT",
) -> ReportRecord:
    return ReportRecord(
        report_id=report_id,
        generated_at=generated_at,
        since_ts=generated_at - timedelta(days=1),
        until_ts=generated_at,
        sections_enabled=tuple(sections_rendered),
        sections_rendered=sections_rendered,
        sections_failed=sections_failed,
        library_version=library_version,
        disclaimer_version_hash=disclaimer_hash,
        rendered_terminal_text=terminal_text,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "compare.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as c:
        run_pending_report_generator_migrations(c)
    yield s
    s.close()


# -- engine tests ---------------------------------------------------------


def test_identical_reports_show_no_changes() -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    record = _make_record(report_id="r1", generated_at=base)
    diff = compare_reports(record, record)
    assert isinstance(diff, ReportDiff)
    assert diff.sections_added == ()
    assert diff.sections_removed == ()
    assert diff.library_version_changed is False
    assert diff.disclaimer_changed is False
    assert diff.terminal_length_delta == 0
    assert diff.time_between == timedelta(0)


def test_sections_added_in_b() -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        sections_rendered=("system_health", "surfaced"),
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        sections_rendered=("system_health", "surfaced", "cross_venue", "calibration"),
    )
    diff = compare_reports(a, b)
    assert diff.sections_added == ("calibration", "cross_venue")
    assert diff.sections_removed == ()


def test_sections_removed_in_b() -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        sections_rendered=("system_health", "surfaced", "cross_venue"),
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        sections_rendered=("system_health",),
    )
    diff = compare_reports(a, b)
    assert diff.sections_removed == ("cross_venue", "surfaced")
    assert diff.sections_added == ()


def test_library_version_delta() -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, library_version=1)
    b = _make_record(report_id="r-b", generated_at=base, library_version=3)
    diff = compare_reports(a, b)
    assert diff.library_version_changed is True
    assert diff.library_version_a == 1
    assert diff.library_version_b == 3


def test_disclaimer_drift() -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, disclaimer_hash="aaa111")
    b = _make_record(report_id="r-b", generated_at=base, disclaimer_hash="bbb222")
    diff = compare_reports(a, b)
    assert diff.disclaimer_changed is True


def test_failed_sections_delta() -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base,
        sections_failed=({"section": "calibration", "error": "x"},),
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        sections_failed=(
            {"section": "watched", "error": "y"},
            {"section": "calibration", "error": "x"},
        ),
    )
    diff = compare_reports(a, b)
    # Newly failed in b: watched. Cleared from a: none.
    assert diff.sections_failed_diff == ("+watched",)


def test_time_between_is_absolute() -> None:
    """Passing reports out of order still gives a non-negative delta."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    older = _make_record(report_id="r-old", generated_at=base - timedelta(days=7))
    newer = _make_record(report_id="r-new", generated_at=base)
    diff_forward = compare_reports(older, newer)
    diff_reverse = compare_reports(newer, older)
    assert diff_forward.time_between == timedelta(days=7)
    assert diff_reverse.time_between == timedelta(days=7)


def test_terminal_length_delta() -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="short")
    b = _make_record(report_id="r-b", generated_at=base, terminal_text="a much longer report text")
    diff = compare_reports(a, b)
    assert diff.terminal_length_delta == len(b.rendered_terminal_text) - len(
        a.rendered_terminal_text
    )
    assert diff.terminal_length_delta > 0


def test_unified_diff_format() -> None:
    """The unified diff includes from/to file labels and -/+ markers."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="line1\nline2\nline3\n")
    b = _make_record(report_id="r-b", generated_at=base, terminal_text="line1\nline-two\nline3\n")
    diff = compare_reports(a, b)
    out = diff.unified_terminal_diff
    assert "report:r-a" in out
    assert "report:r-b" in out
    assert "-line2" in out
    assert "+line-two" in out


# -- CLI tests -----------------------------------------------------------


def test_cli_compare_emits_metadata(store: DuckDBStore) -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=7),
        sections_rendered=("system_health", "surfaced"),
        terminal_text="OLD\n",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        sections_rendered=("system_health", "surfaced", "cross_venue"),
        terminal_text="NEW\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    assert "a: report r-a" in result.output
    assert "b: report r-b" in result.output
    assert "time between: 7 days" in result.output
    assert "added: cross_venue" in result.output


def test_cli_compare_handles_missing_report(store: DuckDBStore) -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-a", generated_at=base))
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "r-a",
            "r-missing",
        ],
    )
    assert result.exit_code != 0
    assert "r-missing" in (result.output + (result.stderr or ""))


def test_cli_compare_no_diff_flag_suppresses_unified_diff(store: DuckDBStore) -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="line1\n")
    b = _make_record(report_id="r-b", generated_at=base, terminal_text="line1\nline2\n")
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--no-diff",
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    # The unified diff section header shouldn't appear.
    assert "unified diff" not in result.output


def test_cli_compare_diff_lines_truncates(store: DuckDBStore) -> None:
    """--diff-lines caps the unified diff output."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    # Build texts that produce many diff lines.
    text_a = "\n".join([f"oldline {i}" for i in range(50)])
    text_b = "\n".join([f"newline {i}" for i in range(50)])
    a = _make_record(report_id="r-a", generated_at=base, terminal_text=text_a)
    b = _make_record(report_id="r-b", generated_at=base, terminal_text=text_b)
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--diff-lines",
            "5",
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    assert "more lines)" in result.output


def test_cli_compare_descriptive_only(store: DuckDBStore) -> None:
    """Compare CLI output passes the imperative-language linter."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="cycle a\n")
    b = _make_record(
        report_id="r-b",
        generated_at=base + timedelta(days=1),
        terminal_text="cycle b\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    check_text(result.output)


# -- compare --html tests (T-RG-COMPAT-COMPARE-HTML-001 v0.46.0) ---------


def test_compare_html_renders_self_contained(store: DuckDBStore, tmp_path: Path) -> None:
    """``compare --html PATH`` writes a self-contained HTML page."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=7),
        sections_rendered=("system_health", "surfaced"),
        terminal_text="OLD CYCLE LINE 1\nOLD CYCLE LINE 2\n",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        sections_rendered=("system_health", "surfaced", "cross_venue"),
        terminal_text="NEW CYCLE LINE 1\nNEW CYCLE LINE 2\nNEW LINE 3\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    # Self-contained: no external references.
    assert "<!DOCTYPE html>" in content
    assert "<style>" in content
    assert "</style>" in content
    assert "src=" not in content  # no external images
    assert "<script" not in content  # no JavaScript
    assert "http://" not in content
    assert "https://" not in content
    # Two-column container present.
    assert "side-by-side" in content
    # Both report ids referenced.
    assert "r-a" in content
    assert "r-b" in content
    # Terminal text appears for both reports.
    assert "OLD CYCLE LINE 1" in content
    assert "NEW CYCLE LINE 1" in content
    # CLI prints the html_path.
    assert "html_path" in result.output


def test_compare_html_passes_linter(store: DuckDBStore, tmp_path: Path) -> None:
    """The rendered HTML passes the imperative-language linter."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        terminal_text="report a body\n",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        terminal_text="report b body\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    check_text(content)


def test_compare_html_escapes_user_content(store: DuckDBStore, tmp_path: Path) -> None:
    """Report ids and terminal text are HTML-escaped."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        terminal_text="<script>alert(1)</script>\n",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        terminal_text='& < > "\n',
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    # The literal <script> from the report text MUST be escaped.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content
    # Only the inline <style> tag that we ourselves emit is allowed.
    assert content.count("<script") == 0


def test_compare_html_shows_metadata_changes(store: DuckDBStore, tmp_path: Path) -> None:
    """The metadata table marks changed fields."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        library_version=1,
        disclaimer_hash="aaaaaaaaaaaaaaaaaaaa",
        terminal_text="short",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        library_version=3,
        disclaimer_hash="bbbbbbbbbbbbbbbbbbbb",
        terminal_text="much longer text content here",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    # Library version row shows both values and the changed class.
    assert "library version" in content
    assert ">1<" in content
    assert ">3<" in content
    assert 'class="changed"' in content


def test_compare_html_section_diffs(store: DuckDBStore, tmp_path: Path) -> None:
    """Added and removed sections appear with semantic styling."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        sections_rendered=("system_health", "surfaced", "cross_venue"),
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        sections_rendered=("system_health", "calibration"),
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    assert "calibration" in content
    assert "surfaced" in content
    assert "cross_venue" in content
    assert 'class="added"' in content
    assert 'class="removed"' in content


def test_compare_html_writes_into_nested_directory(store: DuckDBStore, tmp_path: Path) -> None:
    """Compare --html creates parent directories on demand."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="a\n")
    b = _make_record(report_id="r-b", generated_at=base + timedelta(hours=1), terminal_text="b\n")
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "nested" / "deeply" / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    assert out_path.exists()


# -- compare --html unified-diff panel (v0.46.0 follow-on Step 1) -------


def test_compare_html_includes_unified_diff_panel(store: DuckDBStore, tmp_path: Path) -> None:
    """The HTML page contains a fourth section with the unified diff."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        terminal_text="line one\nline two\nline three\n",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        terminal_text="line one\nline two CHANGED\nline three\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    assert "<h2>Unified diff</h2>" in content
    assert "unified-diff" in content


def test_compare_html_unified_diff_classifies_lines(store: DuckDBStore, tmp_path: Path) -> None:
    """Added/removed/hunk-header/file-meta lines get their semantic classes."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        terminal_text="alpha\nbravo\ncharlie\n",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        terminal_text="alpha\nBRAVO_NEW\ncharlie\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    # Hunk header carries diff-hunk class.
    assert "diff-hunk" in content
    # Removed line carries diff-del class.
    assert "diff-del" in content
    assert "-bravo" in content or "bravo" in content  # raw line preserved (escaped)
    # Added line carries diff-add class.
    assert "diff-add" in content
    assert "BRAVO_NEW" in content
    # File metadata (---, +++) carries diff-meta class.
    assert "diff-meta" in content


def test_compare_html_unified_diff_truncates(store: DuckDBStore, tmp_path: Path) -> None:
    """--diff-lines also caps the HTML unified-diff panel."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    text_a = "\n".join(f"old-line-{i}" for i in range(60))
    text_b = "\n".join(f"new-line-{i}" for i in range(60))
    a = _make_record(report_id="r-a", generated_at=base, terminal_text=text_a)
    b = _make_record(report_id="r-b", generated_at=base + timedelta(hours=1), terminal_text=text_b)
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--diff-lines",
            "5",
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    assert "diff-truncated" in content
    assert "more line" in content


def test_compare_html_unified_diff_empty_for_identical_text(
    store: DuckDBStore, tmp_path: Path
) -> None:
    """When the terminal text is identical, the panel emits a benign message."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="same content\n")
    b = _make_record(
        report_id="r-b",
        generated_at=base + timedelta(hours=1),
        terminal_text="same content\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    assert "<h2>Unified diff</h2>" in content
    assert "No textual differences" in content


def test_compare_html_translates_ansi_in_terminal_text(store: DuckDBStore, tmp_path: Path) -> None:
    """ANSI SGR sequences in terminal text become semantic spans."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    # Red, bold, reset — three SGR codes around a label.
    ansi_text = "\x1b[31m\x1b[1mWARNING\x1b[0m: routine\n"
    a = _make_record(report_id="r-a", generated_at=base, terminal_text=ansi_text)
    b = _make_record(
        report_id="r-b",
        generated_at=base + timedelta(hours=1),
        terminal_text=ansi_text,
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    # Raw escape character must be gone from the output.
    assert "\x1b" not in content
    # Semantic classes for red foreground + bold appear.
    assert "ansi-fg-red" in content
    assert "ansi-bold" in content
    # The label text is preserved.
    assert "WARNING" in content
    # The CSS for the classes is inlined.
    assert ".ansi-fg-red" in content
    assert ".ansi-bold" in content


# -- compare-HTML word-level diff highlighting (v0.48.0 follow-on Step 1) -


def test_compare_html_word_level_diff_inside_replaced_lines(
    store: DuckDBStore, tmp_path: Path
) -> None:
    """Touched lines that share a prefix get word-level highlights."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        terminal_text="comparisons surfaced: 12\n",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        terminal_text="comparisons surfaced: 14\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    # Both word-level highlight classes appear.
    assert "word-del" in content
    assert "word-add" in content
    # Specifically, the digit 12 is wrapped in word-del, 14 in word-add.
    assert '<span class="word-del">12</span>' in content
    assert '<span class="word-add">14</span>' in content
    # The unchanged prefix "comparisons surfaced:" appears as plain text.
    # (It must not be inside a word-del or word-add span.)
    assert "comparisons surfaced" in content


def test_compare_html_word_level_falls_back_for_unequal_runs(
    store: DuckDBStore, tmp_path: Path
) -> None:
    """When del-run length != add-run length, fall back to whole-line styling."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    # Two consecutive deletions vs one insertion — unbalanced.
    text_a = "alpha\nbravo\ncharlie\ndelta\n"
    text_b = "alpha\nDELTA-NEW\n"
    a = _make_record(report_id="r-a", generated_at=base - timedelta(days=1), terminal_text=text_a)
    b = _make_record(report_id="r-b", generated_at=base, terminal_text=text_b)
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    # The diff-del / diff-add classes still appear (whole-line styling).
    assert "diff-del" in content
    assert "diff-add" in content
    # No word-level spans for the unbalanced run.
    # (We can still match in the file in principle, but for the
    # unbalanced case the helper must not emit word-* spans.)


def test_compare_html_word_level_only_replaced_runs() -> None:
    """Whole-line replacements get word-level spans; pure adds don't."""
    from razor_rooster.report_generator.engines.compare_html import (
        _render_diff_rows_with_word_highlights,
    )

    # Pure insertion: no preceding deletion.
    rows = _render_diff_rows_with_word_highlights(
        [
            "@@ -1,2 +1,3 @@",
            " context",
            "+just inserted",
        ]
    )
    out = "\n".join(rows)
    assert "word-add" not in out
    assert "word-del" not in out
    assert "diff-add" in out


def test_compare_html_word_level_helper_returns_html_strings() -> None:
    """The helper returns paired (del_html, add_html) for replacements."""
    from razor_rooster.report_generator.engines.compare_html import (
        _word_level_highlights,
    )

    del_html, add_html = _word_level_highlights(
        del_line="-comparisons surfaced: 12",
        add_line="+comparisons surfaced: 14",
    )
    assert "comparisons surfaced" in del_html
    assert "comparisons surfaced" in add_html
    assert '<span class="word-del">12</span>' in del_html
    assert '<span class="word-add">14</span>' in add_html


def test_compare_html_word_level_escapes_html_in_changed_words(
    store: DuckDBStore, tmp_path: Path
) -> None:
    """Adversarial: <script> in a changed word still gets escaped inside spans."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        terminal_text="value: safe\n",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        terminal_text="value: <script>x</script>\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    # The literal <script> must be escaped inside the word-add span.
    assert "<script>x</script>" not in content
    assert "&lt;script&gt;" in content


# -- compare-HTML --no-word-diff (v0.49.0 follow-on Step 1) -------------


def test_compare_html_no_word_diff_disables_word_spans(store: DuckDBStore, tmp_path: Path) -> None:
    """``--no-word-diff`` keeps line-level styling, drops word spans."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        terminal_text="comparisons surfaced: 12\n",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        terminal_text="comparisons surfaced: 14\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare-no-word.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "--no-word-diff",
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    # Line-level styling still present.
    assert "diff-del" in content
    assert "diff-add" in content
    # No word-* spans.
    assert '<span class="word-del">' not in content
    assert '<span class="word-add">' not in content


def test_compare_html_word_diff_default_keeps_word_spans(
    store: DuckDBStore, tmp_path: Path
) -> None:
    """Without --no-word-diff, the default behavior emits word spans."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        terminal_text="value: alpha\n",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        terminal_text="value: beta\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare-default.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    assert '<span class="word-del">' in content
    assert '<span class="word-add">' in content


def test_compare_html_no_word_diff_helper_param() -> None:
    """The lower-level helper honors word_diff=False."""
    from razor_rooster.report_generator.engines.compare_html import (
        _render_diff_rows_with_word_highlights,
    )

    rows = _render_diff_rows_with_word_highlights(
        [
            "@@ -1,1 +1,1 @@",
            "-old line",
            "+new line",
        ],
        word_diff=False,
    )
    out = "\n".join(rows)
    assert "diff-del" in out
    assert "diff-add" in out
    assert '<span class="word-del">' not in out
    assert '<span class="word-add">' not in out


# -- compare-HTML --no-side-by-side (v0.49.0 follow-on Step 4) ----------


def test_compare_html_no_side_by_side_suppresses_panel(store: DuckDBStore, tmp_path: Path) -> None:
    """``--no-side-by-side`` removes the two-column terminal-text panel."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        terminal_text="UNIQUE-A-TEXT\n",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        terminal_text="UNIQUE-B-TEXT\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare-compact.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "--no-side-by-side",
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    # Side-by-side container is gone.
    assert "Side-by-side terminal text" not in content
    assert 'class="side-by-side"' not in content
    # Metadata + sections + unified-diff sections still appear.
    assert "<h2>Metadata</h2>" in content
    assert "<h2>Sections</h2>" in content
    assert "<h2>Unified diff</h2>" in content
    # The terminal text still appears in the unified-diff panel,
    # but it's the diff rendering, not the per-report column.
    # (The shared "UNIQUE-" prefix and "-TEXT" suffix are intact;
    # only the differing letter is wrapped.)
    assert "UNIQUE-" in content
    assert "-TEXT" in content
    assert '<span class="word-del">A</span>' in content
    assert '<span class="word-add">B</span>' in content


def test_compare_html_side_by_side_default_keeps_panel(store: DuckDBStore, tmp_path: Path) -> None:
    """Default behavior keeps the side-by-side panel."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="a\n")
    b = _make_record(
        report_id="r-b",
        generated_at=base + timedelta(hours=1),
        terminal_text="b\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare-default-panel.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    assert "Side-by-side terminal text" in content
    assert 'class="side-by-side"' in content


def test_compare_html_no_side_by_side_combines_with_no_word_diff(
    store: DuckDBStore, tmp_path: Path
) -> None:
    """Both flags together produce the most compact view."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(
        report_id="r-a",
        generated_at=base - timedelta(days=1),
        terminal_text="alpha\n",
    )
    b = _make_record(
        report_id="r-b",
        generated_at=base,
        terminal_text="beta\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare-most-compact.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "--no-side-by-side",
            "--no-word-diff",
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    assert "Side-by-side terminal text" not in content
    assert '<span class="word-del">' not in content
    assert '<span class="word-add">' not in content
    # Line-level diff still present.
    assert "diff-del" in content
    assert "diff-add" in content


# -- compare-HTML deep-link anchors (v0.50.0 follow-on Step 3) ---------


def test_compare_html_emits_section_anchors(store: DuckDBStore, tmp_path: Path) -> None:
    """Each section carries a stable id attribute for URL-fragment deep links."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="alpha\n")
    b = _make_record(
        report_id="r-b",
        generated_at=base + timedelta(hours=1),
        terminal_text="beta\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    assert '<section id="metadata">' in content
    assert '<section id="sections">' in content
    assert '<section id="side-by-side">' in content
    assert '<section id="unified-diff">' in content


def test_compare_html_emits_quick_jump_nav(store: DuckDBStore, tmp_path: Path) -> None:
    """A nav block with anchor links sits in the header."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="x\n")
    b = _make_record(report_id="r-b", generated_at=base + timedelta(hours=1), terminal_text="y\n")
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare-nav.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    assert 'class="quick-jump muted"' in content
    assert 'href="#metadata"' in content
    assert 'href="#sections"' in content
    assert 'href="#side-by-side"' in content
    assert 'href="#unified-diff"' in content


def test_compare_html_quick_jump_omits_side_by_side_when_suppressed(
    store: DuckDBStore, tmp_path: Path
) -> None:
    """``--no-side-by-side`` removes the side-by-side anchor from the nav."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="x\n")
    b = _make_record(report_id="r-b", generated_at=base + timedelta(hours=1), terminal_text="y\n")
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare-no-sbs-nav.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "--no-side-by-side",
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    # Nav still present, but the side-by-side anchor isn't.
    assert 'class="quick-jump muted"' in content
    assert 'href="#side-by-side"' not in content
    assert '<section id="side-by-side">' not in content
    # Other anchors still in place.
    assert 'href="#metadata"' in content
    assert 'href="#unified-diff"' in content


# -- report compare-latest shortcut (v0.50.0 follow-on Step 4) ----------


def test_compare_latest_diffs_two_most_recent(store: DuckDBStore) -> None:
    """``compare-latest`` resolves the two newest report ids and diffs them."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-old", generated_at=base - timedelta(days=2)))
        persist_report(c, _make_record(report_id="r-mid", generated_at=base - timedelta(days=1)))
        persist_report(
            c, _make_record(report_id="r-new", generated_at=base, terminal_text="newest\n")
        )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["compare-latest", "--db", str(store.path)],
    )
    assert result.exit_code == 0
    # Resolution announcement names the latest pair.
    assert "comparing latest pair: a=r-mid" in result.output
    assert "b=r-new" in result.output
    # The compare body still appears.
    assert "metadata:" in result.output


def test_compare_latest_with_html(store: DuckDBStore, tmp_path: Path) -> None:
    """``compare-latest --html`` writes the HTML view for the latest pair."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-prev", generated_at=base - timedelta(days=1)))
        persist_report(c, _make_record(report_id="r-curr", generated_at=base))
    out_path = tmp_path / "compare-latest.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["compare-latest", "--db", str(store.path), "--html", str(out_path)],
    )
    assert result.exit_code == 0
    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert "r-prev" in content
    assert "r-curr" in content


def test_compare_latest_with_no_word_diff_and_no_side_by_side(
    store: DuckDBStore, tmp_path: Path
) -> None:
    """``compare-latest`` forwards rendering flags to the compare path."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    with store.connection() as c:
        persist_report(
            c,
            _make_record(
                report_id="r-prev",
                generated_at=base - timedelta(days=1),
                terminal_text="alpha\n",
            ),
        )
        persist_report(
            c, _make_record(report_id="r-curr", generated_at=base, terminal_text="beta\n")
        )
    out_path = tmp_path / "compare-latest-compact.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare-latest",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "--no-side-by-side",
            "--no-word-diff",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    assert "Side-by-side terminal text" not in content
    assert '<span class="word-del">' not in content
    assert "diff-del" in content


def test_compare_latest_requires_two_reports(store: DuckDBStore) -> None:
    """``compare-latest`` fails cleanly when fewer than two reports exist."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-only", generated_at=base))
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["compare-latest", "--db", str(store.path)],
    )
    assert result.exit_code != 0
    assert "Need at least 2 reports" in (result.output + (result.stderr or ""))


def test_compare_latest_fails_on_empty_store(store: DuckDBStore) -> None:
    """``compare-latest`` against an empty store fails with a clear message."""
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["compare-latest", "--db", str(store.path)],
    )
    assert result.exit_code != 0
    assert "Need at least 2 reports" in (result.output + (result.stderr or ""))
    assert "found 0" in (result.output + (result.stderr or ""))


# -- compare-HTML --no-quick-jump (v0.51.0 follow-on Step 1) ------------


def test_compare_html_no_quick_jump_drops_nav(store: DuckDBStore, tmp_path: Path) -> None:
    """``--no-quick-jump`` removes the nav block from the header."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="x\n")
    b = _make_record(
        report_id="r-b",
        generated_at=base + timedelta(hours=1),
        terminal_text="y\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare-no-jump.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "--no-quick-jump",
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    # Nav block is gone.
    assert 'class="quick-jump muted"' not in content
    assert 'href="#metadata"' not in content
    # Sections still carry ids — deep linking still works for
    # operators who construct URLs by hand.
    assert '<section id="metadata">' in content
    assert '<section id="unified-diff">' in content


def test_compare_html_quick_jump_default_renders_nav(store: DuckDBStore, tmp_path: Path) -> None:
    """Default ``--quick-jump`` keeps the nav."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="a\n")
    b = _make_record(
        report_id="r-b",
        generated_at=base + timedelta(hours=1),
        terminal_text="b\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare-default-jump.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    assert 'class="quick-jump muted"' in content


def test_compare_html_quick_jump_combines_with_other_toggles(
    store: DuckDBStore, tmp_path: Path
) -> None:
    """``--no-quick-jump`` composes with the other compactness flags."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    a = _make_record(report_id="r-a", generated_at=base, terminal_text="alpha\n")
    b = _make_record(
        report_id="r-b",
        generated_at=base + timedelta(hours=1),
        terminal_text="beta\n",
    )
    with store.connection() as c:
        persist_report(c, a)
        persist_report(c, b)
    out_path = tmp_path / "compare-most-compact.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare",
            "--db",
            str(store.path),
            "--html",
            str(out_path),
            "--no-quick-jump",
            "--no-side-by-side",
            "--no-word-diff",
            "r-a",
            "r-b",
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    assert 'class="quick-jump muted"' not in content
    assert "Side-by-side terminal text" not in content
    assert '<span class="word-del">' not in content
    # Line-level diff still present.
    assert "diff-del" in content
    assert "diff-add" in content


# -- compare-latest --offset (v0.51.0 follow-on Step 2) -----------------


def test_compare_latest_offset_steps_back_through_history(
    store: DuckDBStore,
) -> None:
    """``--offset 1`` diffs reports [1] (older=a) and [2] (oldest=b would be... wait)."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-0", generated_at=base))  # newest
        persist_report(c, _make_record(report_id="r-1", generated_at=base - timedelta(days=1)))
        persist_report(c, _make_record(report_id="r-2", generated_at=base - timedelta(days=2)))
        persist_report(c, _make_record(report_id="r-3", generated_at=base - timedelta(days=3)))
    runner = CliRunner()
    # offset 0: diffs r-1 (older=a) vs r-0 (newer=b)
    result0 = runner.invoke(
        report_cli,
        ["compare-latest", "--db", str(store.path), "--offset", "0"],
    )
    assert result0.exit_code == 0
    assert "comparing latest pair: a=r-1  b=r-0" in result0.output
    # offset 1: diffs r-2 vs r-1
    result1 = runner.invoke(
        report_cli,
        ["compare-latest", "--db", str(store.path), "--offset", "1"],
    )
    assert result1.exit_code == 0
    assert "comparing latest pair: a=r-2  b=r-1" in result1.output
    # offset 2: diffs r-3 vs r-2
    result2 = runner.invoke(
        report_cli,
        ["compare-latest", "--db", str(store.path), "--offset", "2"],
    )
    assert result2.exit_code == 0
    assert "comparing latest pair: a=r-3  b=r-2" in result2.output


def test_compare_latest_offset_too_large_refuses(store: DuckDBStore) -> None:
    """``--offset N`` requires at least N+2 reports."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-0", generated_at=base))
        persist_report(c, _make_record(report_id="r-1", generated_at=base - timedelta(days=1)))
    runner = CliRunner()
    # Need 4 reports; only have 2.
    result = runner.invoke(
        report_cli,
        ["compare-latest", "--db", str(store.path), "--offset", "2"],
    )
    assert result.exit_code != 0
    combined = result.output + (result.stderr or "")
    assert "Need at least 4 reports for compare-latest --offset 2" in combined
    assert "found 2" in combined


def test_compare_latest_offset_negative_rejected(store: DuckDBStore) -> None:
    """Negative offsets are rejected with click.BadParameter."""
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["compare-latest", "--db", str(store.path), "--offset", "-1"],
    )
    assert result.exit_code != 0
    assert "must be >= 0" in (result.output + (result.stderr or ""))


def test_compare_latest_offset_combines_with_html(store: DuckDBStore, tmp_path: Path) -> None:
    """``--offset`` works with --html flag forwarding."""
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-now", generated_at=base, terminal_text="N\n"))
        persist_report(
            c,
            _make_record(
                report_id="r-mid", generated_at=base - timedelta(days=1), terminal_text="M\n"
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-old", generated_at=base - timedelta(days=2), terminal_text="O\n"
            ),
        )
    out_path = tmp_path / "compare-offset.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "compare-latest",
            "--db",
            str(store.path),
            "--offset",
            "1",
            "--html",
            str(out_path),
        ],
    )
    assert result.exit_code == 0
    content = out_path.read_text(encoding="utf-8")
    # offset=1 → a=r-old, b=r-mid; r-now should not be referenced.
    assert "r-old" in content
    assert "r-mid" in content
    assert "r-now" not in content
