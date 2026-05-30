"""Tests for ``razor-rooster report digest`` (T-RG-COMPAT-DIGEST-001 v0.46.0).

Covers:
- digest with no reports yet emits a benign message.
- Reports inside the window appear; reports outside don't.
- --days input validation rejects out-of-range values.
- Digest output passes the imperative-language linter.
- Metadata is one-line-per-report and includes md/html markers
  when the underlying ReportRecord persisted those paths.
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
    terminal_text: str = "REPORT TEXT",
    markdown_path: str | None = None,
    html_path: str | None = None,
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
        disclaimer_version_hash="abc123",
        rendered_terminal_text=terminal_text,
        markdown_path=markdown_path,
        html_path=html_path,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "digest.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as c:
        run_pending_report_generator_migrations(c)
    yield s
    s.close()


def test_digest_with_no_reports(store: DuckDBStore) -> None:
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path)])
    assert result.exit_code == 0
    assert "No reports in the last 7 day(s)." in result.output


def test_digest_default_window_seven_days(store: DuckDBStore) -> None:
    """Default --days is 7."""
    now = datetime.now(tz=UTC)
    inside = now - timedelta(days=3)
    outside = now - timedelta(days=10)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-inside", generated_at=inside))
        persist_report(c, _make_record(report_id="r-outside", generated_at=outside))
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path)])
    assert result.exit_code == 0
    assert "r-inside" in result.output
    assert "r-outside" not in result.output
    assert "reports in the last 7 day(s): 1" in result.output


def test_digest_custom_window(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-1d", generated_at=now - timedelta(days=1)))
        persist_report(c, _make_record(report_id="r-15d", generated_at=now - timedelta(days=15)))
        persist_report(c, _make_record(report_id="r-25d", generated_at=now - timedelta(days=25)))
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--days", "20"],
    )
    assert result.exit_code == 0
    assert "r-1d" in result.output
    assert "r-15d" in result.output
    assert "r-25d" not in result.output
    assert "reports in the last 20 day(s): 2" in result.output


def test_digest_rejects_days_below_one(store: DuckDBStore) -> None:
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--days", "0"],
    )
    assert result.exit_code != 0
    assert "out of range" in (result.output + (result.stderr or ""))


def test_digest_rejects_days_above_max(store: DuckDBStore) -> None:
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--days", "366"],
    )
    assert result.exit_code != 0
    assert "out of range" in (result.output + (result.stderr or ""))


def test_digest_includes_section_and_length_metadata(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c,
            _make_record(
                report_id="r-meta",
                generated_at=now - timedelta(days=1),
                sections_rendered=("system_health", "surfaced", "cross_venue"),
                sections_failed=({"section": "calibration", "error": "x"},),
                terminal_text="A" * 1234,
            ),
        )
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path)])
    assert result.exit_code == 0
    assert "r-meta" in result.output
    assert "sections=3/3" in result.output
    assert "failed=1" in result.output
    assert "terminal_chars=1234" in result.output


def test_digest_marks_md_and_html_outputs(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c,
            _make_record(
                report_id="r-md-only",
                generated_at=now - timedelta(days=1),
                markdown_path="/tmp/r-md-only.md",
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-both",
                generated_at=now - timedelta(hours=12),
                markdown_path="/tmp/r-both.md",
                html_path="/tmp/r-both.html",
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-plain",
                generated_at=now - timedelta(hours=6),
            ),
        )
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path)])
    assert result.exit_code == 0
    # r-md-only line has [md] but not html.
    md_only_line = [line for line in result.output.splitlines() if "r-md-only" in line]
    assert md_only_line
    assert "[md]" in md_only_line[0]
    assert "html" not in md_only_line[0]
    # r-both line has both markers.
    both_line = [line for line in result.output.splitlines() if "r-both" in line]
    assert both_line
    assert "md" in both_line[0]
    assert "html" in both_line[0]
    # r-plain has no markers.
    plain_line = [line for line in result.output.splitlines() if "r-plain" in line]
    assert plain_line
    assert "[" not in plain_line[0]


def test_digest_orders_reports_newest_first(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-old", generated_at=now - timedelta(days=5)))
        persist_report(c, _make_record(report_id="r-mid", generated_at=now - timedelta(days=3)))
        persist_report(c, _make_record(report_id="r-new", generated_at=now - timedelta(days=1)))
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path)])
    assert result.exit_code == 0
    new_idx = result.output.find("r-new")
    mid_idx = result.output.find("r-mid")
    old_idx = result.output.find("r-old")
    assert 0 <= new_idx < mid_idx < old_idx


def test_digest_output_passes_linter(store: DuckDBStore) -> None:
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c,
            _make_record(
                report_id="r-linter",
                generated_at=now - timedelta(days=1),
            ),
        )
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path)])
    assert result.exit_code == 0
    check_text(result.output)


# -- digest aggregation header (v0.46.0 follow-on Step 2) ---------------


def test_digest_emits_aggregation_header(store: DuckDBStore) -> None:
    """A small aggregate header sits above the per-row listing."""
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c,
            _make_record(
                report_id="r-1",
                generated_at=now - timedelta(days=1),
                sections_rendered=("system_health", "surfaced", "watched"),
                sections_failed=({"section": "calibration", "error": "x"},),
                terminal_text="A" * 1000,
                markdown_path="/tmp/r-1.md",
                html_path="/tmp/r-1.html",
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-2",
                generated_at=now - timedelta(days=2),
                sections_rendered=("system_health", "surfaced"),
                sections_failed=(),
                terminal_text="B" * 1500,
                markdown_path="/tmp/r-2.md",
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-3",
                generated_at=now - timedelta(days=3),
                sections_rendered=("system_health",),
                sections_failed=(),
                terminal_text="C" * 500,
            ),
        )
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path)])
    assert result.exit_code == 0
    # Top line: total reports.
    assert "reports in the last 7 day(s): 3" in result.output
    # Header: failures, markdown, html counts.
    assert "cycles with failures: 1" in result.output
    assert "with markdown: 2" in result.output
    assert "with html: 1" in result.output
    # Header: averages.
    # avg sections rendered = (3 + 2 + 1) / 3 = 2.0
    assert "avg sections rendered: 2.0" in result.output
    # avg terminal chars = (1000 + 1500 + 500) / 3 = 1000
    assert "avg terminal chars: 1000" in result.output


def test_digest_aggregation_with_zero_failures(store: DuckDBStore) -> None:
    """Aggregation header still renders cleanly with all-clean reports."""
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        for i in range(2):
            persist_report(
                c,
                _make_record(
                    report_id=f"r-clean-{i}",
                    generated_at=now - timedelta(hours=i + 1),
                    sections_rendered=("system_health", "surfaced"),
                    sections_failed=(),
                    terminal_text="x" * 100,
                ),
            )
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path)])
    assert result.exit_code == 0
    assert "cycles with failures: 0" in result.output
    assert "with markdown: 0" in result.output
    assert "with html: 0" in result.output


def test_digest_aggregation_passes_linter(store: DuckDBStore) -> None:
    """The aggregation header passes the imperative-language linter."""
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c,
            _make_record(
                report_id="r-linter-agg",
                generated_at=now - timedelta(hours=1),
                sections_rendered=("system_health", "surfaced", "calibration"),
                sections_failed=({"section": "watched", "error": "x"},),
                terminal_text="abc",
                markdown_path="/tmp/r.md",
                html_path="/tmp/r.html",
            ),
        )
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path)])
    assert result.exit_code == 0
    check_text(result.output)


# -- digest --json output (v0.48.0 follow-on Step 2) --------------------


def test_digest_json_emits_jsonlines(store: DuckDBStore) -> None:
    """``--json`` emits one JSON object per line + an aggregate object."""
    import json

    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c,
            _make_record(
                report_id="r-1",
                generated_at=now - timedelta(days=1),
                sections_rendered=("system_health", "surfaced"),
                terminal_text="A" * 100,
                markdown_path="/tmp/r-1.md",
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-2",
                generated_at=now - timedelta(days=2),
                sections_rendered=("system_health",),
                sections_failed=({"section": "calibration", "error": "x"},),
                terminal_text="B" * 200,
            ),
        )
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path), "--json"])
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    # 2 reports + 1 aggregate.
    assert len(lines) == 3
    objs = [json.loads(line) for line in lines]
    # First two are report objects in newest-first order.
    assert objs[0]["kind"] == "report"
    assert objs[0]["report_id"] == "r-1"
    assert objs[0]["sections_rendered"] == 2
    assert objs[0]["sections_failed"] == 0
    assert objs[0]["terminal_chars"] == 100
    assert objs[0]["markdown_path"] == "/tmp/r-1.md"
    assert objs[0]["html_path"] is None
    assert objs[1]["kind"] == "report"
    assert objs[1]["report_id"] == "r-2"
    assert objs[1]["sections_failed"] == 1
    # Last line is the aggregate.
    assert objs[2]["kind"] == "aggregate"
    assert objs[2]["report_count"] == 2
    assert objs[2]["cycles_with_failures"] == 1
    assert objs[2]["cycles_with_markdown"] == 1
    assert objs[2]["cycles_with_html"] == 0


def test_digest_json_empty_window_emits_aggregate_only(store: DuckDBStore) -> None:
    """``--json`` against an empty window still emits an aggregate object."""
    import json

    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--json", "--days", "1"],
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["kind"] == "aggregate"
    assert obj["report_count"] == 0
    assert obj["avg_sections_rendered"] is None
    assert obj["avg_terminal_chars"] is None


def test_digest_json_each_line_is_valid_json(store: DuckDBStore) -> None:
    """jsonlines convention: each output line parses cleanly on its own."""
    import json

    now = datetime.now(tz=UTC)
    with store.connection() as c:
        for i in range(3):
            persist_report(
                c,
                _make_record(
                    report_id=f"r-jsonl-{i}",
                    generated_at=now - timedelta(hours=i + 1),
                ),
            )
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path), "--json"])
    assert result.exit_code == 0
    for line in result.output.splitlines():
        line = line.strip()
        if not line:
            continue
        # Must be standalone-parseable.
        parsed = json.loads(line)
        assert "kind" in parsed


# -- digest --since window override (v0.48.0 follow-on Step 4) ----------


def test_digest_since_iso_window(store: DuckDBStore) -> None:
    """``--since ISO`` selects all reports at or after the timestamp."""
    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(days=2)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-after", generated_at=now - timedelta(days=1)))
        persist_report(c, _make_record(report_id="r-before", generated_at=now - timedelta(days=5)))
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--since", cutoff.isoformat()],
    )
    assert result.exit_code == 0
    assert "r-after" in result.output
    assert "r-before" not in result.output
    assert f"reports since {cutoff.isoformat()}" in result.output


def test_digest_days_and_since_mutually_exclusive(store: DuckDBStore) -> None:
    """Passing both --days and --since errors with a friendly message."""
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "digest",
            "--db",
            str(store.path),
            "--days",
            "7",
            "--since",
            "2026-05-01",
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in (result.output + (result.stderr or ""))


def test_digest_since_invalid_iso_rejected(store: DuckDBStore) -> None:
    """Non-ISO --since inputs are rejected with a clear message."""
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--since", "not-a-date"],
    )
    assert result.exit_code != 0
    assert "invalid --since" in (result.output + (result.stderr or ""))


def test_digest_since_naive_assumed_utc(store: DuckDBStore) -> None:
    """A naive ISO timestamp is interpreted as UTC."""
    now = datetime.now(tz=UTC)
    naive_cutoff = (now - timedelta(days=2)).replace(tzinfo=None).isoformat()
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-naive", generated_at=now - timedelta(days=1)))
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--since", naive_cutoff],
    )
    assert result.exit_code == 0
    assert "r-naive" in result.output


def test_digest_since_with_json_combo(store: DuckDBStore) -> None:
    """--since combines cleanly with --json."""
    import json

    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(days=2)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-combo", generated_at=now - timedelta(days=1)))
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "digest",
            "--db",
            str(store.path),
            "--since",
            cutoff.isoformat(),
            "--json",
        ],
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 2  # one report + aggregate
    aggregate = json.loads(lines[-1])
    assert aggregate["kind"] == "aggregate"
    assert aggregate["report_count"] == 1
    # The window field starts with "since".
    assert aggregate["window"].startswith("since ")


# -- digest --report-id PREFIX filter (v0.49.0 follow-on Step 3) --------


def test_digest_report_id_prefix_filters_terminal(store: DuckDBStore) -> None:
    """``--report-id PREFIX`` matches only reports whose id starts with the prefix."""
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c, _make_record(report_id="rpt-2026-05-16", generated_at=now - timedelta(hours=1))
        )
        persist_report(
            c, _make_record(report_id="rpt-2026-05-15", generated_at=now - timedelta(hours=2))
        )
        persist_report(
            c, _make_record(report_id="rpt-2026-04-30", generated_at=now - timedelta(hours=3))
        )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "digest",
            "--db",
            str(store.path),
            "--report-id",
            "rpt-2026-05",
            "--days",
            "30",
        ],
    )
    assert result.exit_code == 0
    assert "rpt-2026-05-16" in result.output
    assert "rpt-2026-05-15" in result.output
    assert "rpt-2026-04-30" not in result.output
    assert "filtered by report-id prefix 'rpt-2026-05'" in result.output


def test_digest_report_id_prefix_no_matches(store: DuckDBStore) -> None:
    """An unmatched prefix emits the prefix-aware empty message."""
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c, _make_record(report_id="rpt-2026-05-16", generated_at=now - timedelta(hours=1))
        )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--report-id", "no-such-prefix"],
    )
    assert result.exit_code == 0
    assert "No reports" in result.output
    assert "filtered by report-id prefix 'no-such-prefix'" in result.output


def test_digest_report_id_prefix_combines_with_json(store: DuckDBStore) -> None:
    """``--report-id`` combines cleanly with ``--json``."""
    import json

    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c, _make_record(report_id="rpt-2026-05-16", generated_at=now - timedelta(hours=1))
        )
        persist_report(
            c, _make_record(report_id="rpt-2026-04-30", generated_at=now - timedelta(hours=2))
        )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "digest",
            "--db",
            str(store.path),
            "--report-id",
            "rpt-2026-05",
            "--json",
            "--days",
            "30",
        ],
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    # 1 matching report + 1 aggregate.
    assert len(lines) == 2
    objs = [json.loads(line) for line in lines]
    # Filtered to rpt-2026-05 only.
    report_objs = [o for o in objs if o.get("kind") == "report"]
    assert len(report_objs) == 1
    assert report_objs[0]["report_id"] == "rpt-2026-05-16"
    aggregate = next(o for o in objs if o.get("kind") == "aggregate")
    assert aggregate["report_count"] == 1
    assert aggregate["report_id_prefix"] == "rpt-2026-05"


def test_digest_report_id_prefix_combines_with_since(store: DuckDBStore) -> None:
    """``--report-id`` and ``--since`` combine correctly."""
    now = datetime.now(tz=UTC)
    cutoff = now - timedelta(days=2)
    with store.connection() as c:
        persist_report(
            c, _make_record(report_id="rpt-prod-1", generated_at=now - timedelta(hours=1))
        )
        persist_report(
            c, _make_record(report_id="rpt-test-1", generated_at=now - timedelta(hours=2))
        )
        persist_report(
            c, _make_record(report_id="rpt-prod-old", generated_at=now - timedelta(days=5))
        )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "digest",
            "--db",
            str(store.path),
            "--since",
            cutoff.isoformat(),
            "--report-id",
            "rpt-prod",
        ],
    )
    assert result.exit_code == 0
    assert "rpt-prod-1" in result.output
    assert "rpt-test-1" not in result.output  # filtered by prefix
    assert "rpt-prod-old" not in result.output  # filtered by since


def test_digest_json_aggregate_includes_report_id_prefix_when_set(store: DuckDBStore) -> None:
    """JSON aggregate carries the prefix when --report-id is set."""
    import json

    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c, _make_record(report_id="rpt-2026-05-16", generated_at=now - timedelta(hours=1))
        )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--json", "--report-id", "rpt-2026"],
    )
    assert result.exit_code == 0
    aggregate = json.loads(result.output.splitlines()[-1])
    assert aggregate["report_id_prefix"] == "rpt-2026"


def test_digest_json_aggregate_prefix_null_when_unset(store: DuckDBStore) -> None:
    """JSON aggregate has report_id_prefix=null when --report-id is not set."""
    import json

    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--json", "--days", "1"],
    )
    assert result.exit_code == 0
    aggregate = json.loads(result.output.splitlines()[-1])
    assert aggregate["report_id_prefix"] is None


# -- digest --sort-by (v0.50.0 follow-on Step 2) ------------------------


def test_digest_sort_by_sections_failed_desc(store: DuckDBStore) -> None:
    """``--sort-by sections_failed`` surfaces highest-failure cycles first."""
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c,
            _make_record(
                report_id="r-clean",
                generated_at=now - timedelta(hours=1),
                sections_failed=(),
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-many-failed",
                generated_at=now - timedelta(hours=3),
                sections_failed=(
                    {"section": "a", "error": "x"},
                    {"section": "b", "error": "x"},
                    {"section": "c", "error": "x"},
                ),
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-one-failed",
                generated_at=now - timedelta(hours=2),
                sections_failed=({"section": "a", "error": "x"},),
            ),
        )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--sort-by", "sections_failed"],
    )
    assert result.exit_code == 0
    many_idx = result.output.find("r-many-failed")
    one_idx = result.output.find("r-one-failed")
    clean_idx = result.output.find("r-clean")
    # Highest first.
    assert 0 <= many_idx < one_idx < clean_idx


def test_digest_sort_by_terminal_chars_asc(store: DuckDBStore) -> None:
    """``--sort-by terminal_chars --sort-direction asc`` puts shortest first."""
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c,
            _make_record(
                report_id="r-long",
                generated_at=now - timedelta(hours=1),
                terminal_text="x" * 5000,
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-short",
                generated_at=now - timedelta(hours=2),
                terminal_text="x" * 100,
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-medium",
                generated_at=now - timedelta(hours=3),
                terminal_text="x" * 1000,
            ),
        )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "digest",
            "--db",
            str(store.path),
            "--sort-by",
            "terminal_chars",
            "--sort-direction",
            "asc",
        ],
    )
    assert result.exit_code == 0
    short_idx = result.output.find("r-short")
    medium_idx = result.output.find("r-medium")
    long_idx = result.output.find("r-long")
    assert 0 <= short_idx < medium_idx < long_idx


def test_digest_sort_by_generated_at_default(store: DuckDBStore) -> None:
    """Default ``--sort-by generated_at --sort-direction desc`` matches existing behavior."""
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-old", generated_at=now - timedelta(days=5)))
        persist_report(c, _make_record(report_id="r-new", generated_at=now - timedelta(days=1)))
        persist_report(c, _make_record(report_id="r-mid", generated_at=now - timedelta(days=3)))
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path)])
    assert result.exit_code == 0
    new_idx = result.output.find("r-new")
    mid_idx = result.output.find("r-mid")
    old_idx = result.output.find("r-old")
    assert 0 <= new_idx < mid_idx < old_idx


def test_digest_sort_rejects_unknown_field(store: DuckDBStore) -> None:
    """``click.Choice`` rejects bogus --sort-by values."""
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--sort-by", "library_version"],
    )
    assert result.exit_code != 0


def test_digest_sort_helper_breaks_ties_on_generated_at() -> None:
    """When primary keys tie, the secondary sort is generated_at desc."""
    from razor_rooster.report_generator.cli import _sort_digest_reports

    now = datetime.now(tz=UTC)
    a = _make_record(
        report_id="a",
        generated_at=now - timedelta(hours=1),
        terminal_text="x" * 100,
    )
    b = _make_record(
        report_id="b",
        generated_at=now - timedelta(hours=2),
        terminal_text="x" * 100,
    )
    out = _sort_digest_reports(
        (a, b),
        sort_by="terminal_chars",
        sort_direction="desc",
    )
    # Equal terminal_chars so secondary sort kicks in: newer first.
    assert out[0].report_id == "a"
    assert out[1].report_id == "b"


def test_digest_sort_with_json(store: DuckDBStore) -> None:
    """``--sort-by`` takes effect in --json output too."""
    import json

    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(
            c,
            _make_record(
                report_id="r-fail-2",
                generated_at=now - timedelta(hours=1),
                sections_failed=(
                    {"section": "x", "error": "e"},
                    {"section": "y", "error": "e"},
                ),
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-clean",
                generated_at=now - timedelta(hours=2),
                sections_failed=(),
            ),
        )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "digest",
            "--db",
            str(store.path),
            "--json",
            "--sort-by",
            "sections_failed",
        ],
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    # Reports come before the aggregate; first report should be r-fail-2.
    first = json.loads(lines[0])
    assert first["kind"] == "report"
    assert first["report_id"] == "r-fail-2"


# -- digest --top N (v0.51.0 follow-on Step 4) -------------------------


def test_digest_top_n_caps_listing(store: DuckDBStore) -> None:
    """``--top N`` limits the per-row listing to N reports."""
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        for i in range(5):
            persist_report(
                c,
                _make_record(
                    report_id=f"r-{i}",
                    generated_at=now - timedelta(hours=i + 1),
                    sections_failed=({"section": "x", "error": "e"},) * (i + 1),
                ),
            )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "digest",
            "--db",
            str(store.path),
            "--sort-by",
            "sections_failed",
            "--top",
            "2",
        ],
    )
    assert result.exit_code == 0
    # Only the top-2 most-failed reports show up.
    assert "r-4" in result.output  # 5 failures
    assert "r-3" in result.output  # 4 failures
    assert "r-2" not in result.output  # 3 failures, sliced
    # Aggregate header still reflects the FULL window.
    assert "reports in the last 7 day(s): 5" in result.output
    # Slice indicator is shown.
    assert "showing top 2 of 5" in result.output


def test_digest_top_n_aggregate_unaffected(store: DuckDBStore) -> None:
    """The aggregate header reflects the unsliced window."""
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        # 3 reports, 2 with failures, 1 with markdown.
        persist_report(
            c,
            _make_record(
                report_id="r-fail-1",
                generated_at=now - timedelta(hours=1),
                sections_failed=({"section": "a", "error": "x"},),
                markdown_path="/tmp/r-fail-1.md",
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-fail-2",
                generated_at=now - timedelta(hours=2),
                sections_failed=({"section": "b", "error": "x"},),
            ),
        )
        persist_report(
            c,
            _make_record(
                report_id="r-clean",
                generated_at=now - timedelta(hours=3),
                sections_failed=(),
            ),
        )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--top", "1"],
    )
    assert result.exit_code == 0
    # Aggregate over all 3 reports.
    assert "reports in the last 7 day(s): 3" in result.output
    assert "cycles with failures: 2" in result.output
    assert "with markdown: 1" in result.output


def test_digest_top_n_with_json(store: DuckDBStore) -> None:
    """``--top N`` slices the per-report JSON objects too."""
    import json

    now = datetime.now(tz=UTC)
    with store.connection() as c:
        for i in range(4):
            persist_report(
                c,
                _make_record(
                    report_id=f"r-jsonl-{i}",
                    generated_at=now - timedelta(hours=i + 1),
                    terminal_text="x" * (1000 * (4 - i)),  # r-jsonl-0 longest
                ),
            )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "digest",
            "--db",
            str(store.path),
            "--sort-by",
            "terminal_chars",
            "--top",
            "2",
            "--json",
        ],
    )
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if line.strip()]
    objs = [json.loads(line) for line in lines]
    report_objs = [o for o in objs if o.get("kind") == "report"]
    aggregate = next(o for o in objs if o.get("kind") == "aggregate")
    # Only 2 per-report objects.
    assert len(report_objs) == 2
    # Aggregate shows full count + slice metadata.
    assert aggregate["report_count"] == 4
    assert aggregate["top_n"] == 2
    assert aggregate["top_n_emitted"] == 2


def test_digest_top_n_out_of_range(store: DuckDBStore) -> None:
    """``--top`` values outside [1, 1000] are rejected."""
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--top", "0"],
    )
    assert result.exit_code != 0
    assert "out of range" in (result.output + (result.stderr or ""))
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--top", "1001"],
    )
    assert result.exit_code != 0


def test_digest_top_n_unset_lists_all(store: DuckDBStore) -> None:
    """Without ``--top``, all matching reports appear."""
    now = datetime.now(tz=UTC)
    with store.connection() as c:
        for i in range(3):
            persist_report(
                c,
                _make_record(
                    report_id=f"r-all-{i}",
                    generated_at=now - timedelta(hours=i + 1),
                ),
            )
    runner = CliRunner()
    result = runner.invoke(report_cli, ["digest", "--db", str(store.path)])
    assert result.exit_code == 0
    for i in range(3):
        assert f"r-all-{i}" in result.output
    # No "showing top" indicator when --top wasn't set.
    assert "showing top" not in result.output


def test_digest_top_n_json_aggregate_top_n_null_when_unset(store: DuckDBStore) -> None:
    """JSON aggregate's ``top_n`` is null when --top isn't passed."""
    import json

    now = datetime.now(tz=UTC)
    with store.connection() as c:
        persist_report(c, _make_record(report_id="r-1", generated_at=now - timedelta(hours=1)))
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        ["digest", "--db", str(store.path), "--json"],
    )
    assert result.exit_code == 0
    aggregate = json.loads(result.output.splitlines()[-1])
    assert aggregate["top_n"] is None
    assert aggregate["top_n_emitted"] is None
