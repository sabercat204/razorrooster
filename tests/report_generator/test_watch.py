"""Tests for the report watch subcommand (T-RG-COMPAT-WATCH-001 v0.45.0).

Covers:
- --once runs a single cycle and exits.
- --max-cycles caps loop iterations.
- --html / --markdown paths get overwritten on each cycle.
- --interval validation rejects out-of-range values.
- Cycle failures are logged but don't terminate the loop.
- CLI output passes the imperative-language linter.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

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
from razor_rooster.report_generator.cli import report as report_cli
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "watch.duckdb"
    s = DuckDBStore(path)
    with s.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_report_generator_migrations(c)
    s.close()
    yield path


# -- --once behavior ------------------------------------------------------


def test_watch_once_runs_single_cycle(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
        ],
    )
    assert result.exit_code == 0
    assert "running report cycle (cycle 1)" in result.output
    assert "Watch exited after 1 cycle(s)." in result.output


def test_watch_once_writes_html(db_path: Path, tmp_path: Path) -> None:
    html_path = tmp_path / "out" / "report.html"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--html",
            str(html_path),
        ],
    )
    assert result.exit_code == 0
    assert html_path.exists()
    assert html_path.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_watch_once_writes_markdown(db_path: Path, tmp_path: Path) -> None:
    md_path = tmp_path / "out" / "report.md"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--markdown",
            str(md_path),
        ],
    )
    assert result.exit_code == 0
    assert md_path.exists()


# -- --max-cycles behavior -------------------------------------------------


def test_watch_max_cycles_caps_loop(db_path: Path) -> None:
    """--max-cycles 2 with a tiny interval runs exactly 2 cycles and exits.

    This test exists for documentation; the actual behavior verification
    happens in ``test_watch_max_cycles_with_patched_sleep`` which patches
    ``time.sleep`` so the loop runs fast.
    """
    # Without monkeypatching, this would sleep 60s after cycle 1. The
    # next test exercises the same code path with patched sleep.


def test_watch_max_cycles_with_patched_sleep(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patched time.sleep keeps the loop fast in tests."""
    sleep_calls: list[float] = []

    def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    monkeypatch.setattr("razor_rooster.report_generator.cli.time.sleep", fake_sleep)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--max-cycles",
            "3",
            "--interval",
            "60",
        ],
    )
    assert result.exit_code == 0
    assert "cycle 3" in result.output
    assert "Watch exited after 3 cycle(s)." in result.output
    # Sleep called between cycles but not after the last one.
    assert len(sleep_calls) == 2
    assert all(s == 60 for s in sleep_calls)


# -- --interval validation ------------------------------------------------


def test_watch_interval_below_minimum_rejected(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "30",  # below 60-second floor
        ],
    )
    assert result.exit_code != 0
    assert "out of range" in (result.output + (result.stderr or ""))


def test_watch_interval_above_maximum_rejected(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "100000",  # above 86400 ceiling
        ],
    )
    assert result.exit_code != 0
    assert "out of range" in (result.output + (result.stderr or ""))


# -- failure isolation ----------------------------------------------------


def test_watch_continues_after_cycle_failure(
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A generate() failure on one cycle is logged, the loop continues."""

    cycle_count = [0]

    def fake_generate(*args: object, **kwargs: object) -> object:
        from datetime import UTC, datetime

        from razor_rooster.report_generator.models import ReportResult

        cycle_count[0] += 1
        if cycle_count[0] == 1:
            raise RuntimeError("first cycle blew up")
        # Subsequent cycles succeed; return a minimal stand-in.
        now = datetime.now(tz=UTC)
        return ReportResult(
            report_id=f"r-{cycle_count[0]}",
            generated_at=now,
            since_ts=now,
            until_ts=now,
            sections_enabled=(),
            sections_rendered=(),
            sections_failed=(),
            rendered_terminal_text="",
        )

    monkeypatch.setattr("razor_rooster.report_generator.cli.generate", fake_generate)
    monkeypatch.setattr("razor_rooster.report_generator.cli.time.sleep", lambda _s: None)
    runner = CliRunner()
    with caplog.at_level(logging.ERROR, logger="razor_rooster.report_generator.cli"):
        result = runner.invoke(
            report_cli,
            [
                "watch",
                "--db",
                str(db_path),
                "--max-cycles",
                "2",
                "--interval",
                "60",
            ],
        )
    assert result.exit_code == 0
    # Both cycles ran; cycle 1 logged a failure, cycle 2 succeeded.
    assert "cycle 1" in result.output
    assert "cycle 2" in result.output
    assert "cycle failed" in result.output


# -- linter compatibility -------------------------------------------------


def test_watch_output_is_descriptive(db_path: Path) -> None:
    """The watch CLI's status output passes the imperative-language linter."""
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
        ],
    )
    assert result.exit_code == 0
    check_text(result.output)


# -- --on-change behavior (T-RG-COMPAT-WATCH-CHANGE-001 v0.46.0) ----------


def test_on_change_first_cycle_always_runs(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first cycle of an --on-change loop always runs (no prior fingerprint)."""
    monkeypatch.setattr("razor_rooster.report_generator.cli.time.sleep", lambda _s: None)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--on-change",
        ],
    )
    assert result.exit_code == 0
    assert "running report cycle (cycle 1)" in result.output
    assert "Watch exited after 1 cycle(s)" in result.output


def test_on_change_skips_when_fingerprint_unchanged(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second cycle with the same upstream state is skipped."""
    monkeypatch.setattr("razor_rooster.report_generator.cli.time.sleep", lambda _s: None)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--max-cycles",
            "3",
            "--interval",
            "60",
            "--on-change",
        ],
    )
    assert result.exit_code == 0
    # First cycle runs; cycles 2 and 3 should be skipped because nothing
    # changed.
    assert "running report cycle (cycle 1)" in result.output
    assert "skipping cycle (--on-change: upstream unchanged)" in result.output
    # The summary line shows both ran and skipped counts.
    assert "Watch exited after 1 cycle(s) (2 skipped)" in result.output


def test_on_change_runs_when_fingerprint_changes(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new tuning-log entry between cycles changes the fingerprint, triggering a run."""
    from razor_rooster.report_generator.persistence.operations import (
        persist_tuning_log_entry,
    )

    sleep_calls = [0]

    def fake_sleep(_s: float) -> None:
        # Between cycles 1 and 2: write a tuning-log entry that changes the
        # fingerprint, so cycle 2 runs.
        sleep_calls[0] += 1
        if sleep_calls[0] == 1:
            store = DuckDBStore(db_path)
            try:
                with store.connection() as c:
                    persist_tuning_log_entry(
                        c,
                        log_id=f"log-{sleep_calls[0]}",
                        applied_at=datetime(2026, 5, 15, 12, sleep_calls[0], tzinfo=UTC),
                        measurement_kind="cross_venue_spread_bps",
                        knob="cross_venue_spread_bps",
                        previous_value=500.0,
                        new_value=750.0,
                        target_percentile=0.70,
                        backup_path=None,
                        note="test",
                    )
            finally:
                store.close()

    # Imports inside the function so the local datetime/UTC stay clean.
    from datetime import UTC, datetime

    monkeypatch.setattr("razor_rooster.report_generator.cli.time.sleep", fake_sleep)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--max-cycles",
            "2",
            "--interval",
            "60",
            "--on-change",
        ],
    )
    assert result.exit_code == 0
    # Both cycles run because the fingerprint changed between them.
    assert "running report cycle (cycle 1)" in result.output
    assert "running report cycle (cycle 2)" in result.output
    assert "Watch exited after 2 cycle(s)" in result.output


def test_on_change_max_cycles_counts_total(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--max-cycles N caps the total iterations (run + skipped)."""
    monkeypatch.setattr("razor_rooster.report_generator.cli.time.sleep", lambda _s: None)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--max-cycles",
            "5",
            "--interval",
            "60",
            "--on-change",
        ],
    )
    assert result.exit_code == 0
    # 1 cycle runs (the first), 4 are skipped. Total 5.
    assert "Watch exited after 1 cycle(s) (4 skipped)" in result.output


# -- compute_upstream_fingerprint engine tests ----------------------------


def test_fingerprint_on_empty_store_returns_all_none(db_path: Path) -> None:
    from razor_rooster.report_generator.engines.change_detection import (
        compute_upstream_fingerprint,
    )

    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            fp = compute_upstream_fingerprint(c)
    finally:
        store.close()
    assert fp.latest_scan_id is None
    assert fp.latest_comparison_id is None
    assert fp.latest_follow_up_id is None
    assert fp.latest_tuning_log_id is None


def test_fingerprint_robust_to_missing_tables(tmp_path: Path) -> None:
    """A fresh DuckDB connection with no migrations returns all-None."""
    import duckdb

    from razor_rooster.report_generator.engines.change_detection import (
        compute_upstream_fingerprint,
    )

    conn = duckdb.connect(str(tmp_path / "raw.duckdb"))
    try:
        fp = compute_upstream_fingerprint(conn)
    finally:
        conn.close()
    assert fp.latest_scan_id is None
    assert fp.latest_tuning_log_id is None


def test_fingerprint_picks_up_tuning_log_change(db_path: Path) -> None:
    """Adding a tuning-log entry changes the fingerprint."""
    from razor_rooster.report_generator.engines.change_detection import (
        compute_upstream_fingerprint,
    )
    from razor_rooster.report_generator.persistence.operations import (
        persist_tuning_log_entry,
    )

    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            before = compute_upstream_fingerprint(c)
            persist_tuning_log_entry(
                c,
                log_id="log-fp-1",
                applied_at=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
                measurement_kind="cross_venue_spread_bps",
                knob="cross_venue_spread_bps",
                previous_value=500.0,
                new_value=750.0,
                target_percentile=0.70,
                backup_path=None,
                note=None,
            )
            after = compute_upstream_fingerprint(c)
    finally:
        store.close()
    assert before.latest_tuning_log_id is None
    assert after.latest_tuning_log_id == "log-fp-1"
    assert not before.is_same_as(after)


def test_fingerprint_is_same_as_on_identical() -> None:
    from razor_rooster.report_generator.engines.change_detection import (
        UpstreamFingerprint,
    )

    a = UpstreamFingerprint(
        latest_scan_id="s1",
        latest_comparison_id="c1",
        latest_follow_up_id="f1",
        latest_tuning_log_id="t1",
    )
    b = UpstreamFingerprint(
        latest_scan_id="s1",
        latest_comparison_id="c1",
        latest_follow_up_id="f1",
        latest_tuning_log_id="t1",
    )
    assert a.is_same_as(b)
    c = UpstreamFingerprint(
        latest_scan_id="s2",
        latest_comparison_id="c1",
        latest_follow_up_id="f1",
        latest_tuning_log_id="t1",
    )
    assert not a.is_same_as(c)


# -- watch --on-change resume summary (v0.46.0 follow-on Step 3) --------


def test_watch_on_change_resume_summary_names_changed_field(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the loop resumes, it logs which fingerprint field changed."""
    # Patch sleep so the loop runs at full speed.
    import razor_rooster.report_generator.cli as cli_module
    from razor_rooster.report_generator.persistence.operations import (
        persist_tuning_log_entry,
    )

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)

    # Pre-seed a tuning-log entry so the first cycle's fingerprint is non-empty.
    s = DuckDBStore(db_path)
    with s.connection() as c:
        persist_tuning_log_entry(
            c,
            log_id="log-resume-1",
            applied_at=datetime(2026, 5, 15, 10, 0, tzinfo=UTC),
            measurement_kind="cross_venue_spread_bps",
            knob="cross_venue_spread_bps",
            previous_value=500.0,
            new_value=750.0,
            target_percentile=0.70,
            backup_path=None,
            note=None,
        )
    s.close()

    # Track cycle count via a closure so we can mutate the upstream
    # state mid-run; the watch loop itself runs synchronously.
    cycle_call_count = {"n": 0}

    def fake_generate(*args: object, **kwargs: object) -> object:
        cycle_call_count["n"] += 1
        # On the second invocation (after at least one skipped cycle),
        # add a new tuning-log entry so the fingerprint changes.
        # But this won't be observed by the loop because changes happen
        # before the *next* cycle's fingerprint computation.
        from razor_rooster.report_generator.models import ReportResult

        return ReportResult(
            report_id=f"rpt-{cycle_call_count['n']}",
            generated_at=datetime.now(tz=UTC),
            sections_enabled=("system_health",),
            sections_rendered=("system_health",),
            sections_failed=(),
            since=None,
            until=datetime.now(tz=UTC),
            terminal_text="report",
            markdown_path=None,
            html_path=None,
            duration_seconds=0.001,
        )

    monkeypatch.setattr(cli_module, "generate", fake_generate)

    # Set up the watch invocation to run 3 cycles. Cycle 1 runs (first
    # always runs); cycles 2 and 3 should skip if upstream is unchanged.
    # Then we'll add a tuning-log entry between cycles to verify the
    # resume note. To exercise the resume path deterministically, we
    # interleave the upstream mutation via a counter callback wired
    # into compute_upstream_fingerprint.
    from razor_rooster.report_generator.engines import change_detection

    real_compute = change_detection.compute_upstream_fingerprint
    fingerprint_call = {"n": 0}

    def stepped_compute(conn: object) -> change_detection.UpstreamFingerprint:
        fingerprint_call["n"] += 1
        # Cycle 1: real fingerprint (has log-resume-1).
        # Cycle 2: same fingerprint (skip).
        # Cycle 3: simulate a new tuning-log entry by adding one.
        if fingerprint_call["n"] == 3:
            persist_tuning_log_entry(
                conn,  # type: ignore[arg-type]
                log_id="log-resume-2",
                applied_at=datetime(2026, 5, 15, 11, 0, tzinfo=UTC),
                measurement_kind="cross_venue_spread_bps",
                knob="cross_venue_spread_bps",
                previous_value=750.0,
                new_value=900.0,
                target_percentile=0.80,
                backup_path=None,
                note=None,
            )
        return real_compute(conn)  # type: ignore[arg-type]

    monkeypatch.setattr(
        cli_module,
        "compute_upstream_fingerprint",
        stepped_compute,
        raising=False,
    )
    # The watch_cmd imports the function locally; patch on the module
    # too so the local-import fetch returns our stepped helper.
    monkeypatch.setattr(change_detection, "compute_upstream_fingerprint", stepped_compute)

    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--max-cycles",
            "3",
            "--interval",
            "60",
            "--on-change",
        ],
    )
    assert result.exit_code == 0
    # Cycle 1 ran (seeded baseline).
    assert "cycle 1" in result.output
    # Cycle 2 was skipped.
    assert "skipping cycle" in result.output
    # Cycle 3 resumed with a note naming the changed field.
    assert "resume after 1 skipped" in result.output
    assert "tuning_log changed" in result.output


def test_watch_on_change_no_resume_note_on_first_cycle(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first cycle never carries a resume note."""
    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
            "--on-change",
        ],
    )
    assert result.exit_code == 0
    assert "resume after" not in result.output


def test_diff_fingerprint_fields_helper() -> None:
    """The helper enumerates which fingerprint fields differ."""
    from razor_rooster.report_generator.cli import _diff_fingerprint_fields
    from razor_rooster.report_generator.engines.change_detection import (
        UpstreamFingerprint,
    )

    a = UpstreamFingerprint(
        latest_scan_id="s1",
        latest_comparison_id="c1",
        latest_follow_up_id="f1",
        latest_tuning_log_id="t1",
    )
    # No changes.
    assert _diff_fingerprint_fields(a, a) == []
    # Single field changed.
    b = UpstreamFingerprint(
        latest_scan_id="s2",
        latest_comparison_id="c1",
        latest_follow_up_id="f1",
        latest_tuning_log_id="t1",
    )
    assert _diff_fingerprint_fields(a, b) == ["scan"]
    # Multiple fields changed (order follows the declared order).
    c = UpstreamFingerprint(
        latest_scan_id="s2",
        latest_comparison_id="c1",
        latest_follow_up_id="f2",
        latest_tuning_log_id="t2",
    )
    assert _diff_fingerprint_fields(a, c) == ["scan", "follow_up", "tuning_log"]


# -- watch-loop summary report (v0.48.0 follow-on Step 3) ---------------


def test_watch_summary_emits_avg_cycle_duration(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit summary includes avg cycle duration when at least one cycle ran."""
    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
        ],
    )
    assert result.exit_code == 0
    assert "Watch exited after 1 cycle(s)" in result.output
    assert "avg cycle duration:" in result.output
    assert "cycles failed: 0" in result.output


def test_watch_summary_reports_failed_cycle(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed cycles are counted in the exit summary."""
    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic cycle failure")

    monkeypatch.setattr(cli_module, "generate", boom)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
        ],
    )
    assert result.exit_code == 0
    assert "cycles failed: 1" in result.output


def test_watch_summary_reports_total_skip_time(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When cycles are skipped, the summary names the total skip time."""
    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--max-cycles",
            "5",
            "--interval",
            "60",
            "--on-change",
        ],
    )
    assert result.exit_code == 0
    assert "Watch exited after 1 cycle(s) (4 skipped)" in result.output
    # Total skip = 4 * 60 = 240
    assert "total skip time: ~240s" in result.output
    assert "4 cycle(s) x 60s interval" in result.output


def test_watch_summary_reports_distinct_changed_fields(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit summary names distinct fingerprint fields seen across the loop."""
    import razor_rooster.report_generator.cli as cli_module
    from razor_rooster.report_generator.engines import change_detection
    from razor_rooster.report_generator.persistence.operations import (
        persist_tuning_log_entry,
    )

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)

    # Pre-seed a tuning-log entry so the fingerprint is non-empty
    # at the very first cycle.
    s = DuckDBStore(db_path)
    with s.connection() as c:
        persist_tuning_log_entry(
            c,
            log_id="log-summary-1",
            applied_at=datetime(2026, 5, 15, 10, 0, tzinfo=UTC),
            measurement_kind="cross_venue_spread_bps",
            knob="cross_venue_spread_bps",
            previous_value=500.0,
            new_value=700.0,
            target_percentile=0.70,
            backup_path=None,
            note=None,
        )
    s.close()

    fingerprint_call = {"n": 0}
    real_compute = change_detection.compute_upstream_fingerprint

    def stepped_compute(conn: object) -> change_detection.UpstreamFingerprint:
        fingerprint_call["n"] += 1
        if fingerprint_call["n"] == 2:
            persist_tuning_log_entry(
                conn,  # type: ignore[arg-type]
                log_id="log-summary-2",
                applied_at=datetime(2026, 5, 15, 11, 0, tzinfo=UTC),
                measurement_kind="cross_venue_spread_bps",
                knob="cross_venue_spread_bps",
                previous_value=700.0,
                new_value=900.0,
                target_percentile=0.80,
                backup_path=None,
                note=None,
            )
        return real_compute(conn)  # type: ignore[arg-type]

    monkeypatch.setattr(change_detection, "compute_upstream_fingerprint", stepped_compute)

    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--max-cycles",
            "2",
            "--interval",
            "60",
            "--on-change",
        ],
    )
    assert result.exit_code == 0
    # The summary lists tuning_log as a distinct changed field.
    assert "fingerprint fields changed during loop:" in result.output
    assert "tuning_log" in result.output


def test_watch_summary_no_skip_section_when_no_skips(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no cycles were skipped, the total-skip-time line is absent."""
    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
        ],
    )
    assert result.exit_code == 0
    assert "total skip time" not in result.output


# -- watch --summary-file (v0.49.0 follow-on Step 2) --------------------


def test_watch_summary_file_writes_plain_text(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--summary-file PATH`` writes plain-text summary to PATH."""
    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    summary_path = tmp_path / "watch-summary.txt"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
            "--summary-file",
            str(summary_path),
        ],
    )
    assert result.exit_code == 0
    assert summary_path.exists()
    content = summary_path.read_text(encoding="utf-8")
    assert "Watch exited after 1 cycle(s)" in content
    assert "avg cycle duration:" in content


def test_watch_summary_file_writes_json_when_suffix_json(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the path ends in .json, the file is a single JSON object."""
    import json

    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    summary_path = tmp_path / "watch-summary.json"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
            "--summary-file",
            str(summary_path),
        ],
    )
    assert result.exit_code == 0
    assert summary_path.exists()
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "watch_summary"
    assert payload["cycles_run"] == 1
    assert payload["cycles_skipped"] == 0
    assert payload["cycles_failed"] == 0
    assert payload["avg_cycle_duration_seconds"] is not None
    assert payload["interval_seconds"] == 60
    assert payload["fingerprint_fields_changed"] == []
    assert payload["total_skip_seconds"] == 0


def test_watch_summary_file_creates_parent_dirs(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parent directories are created on demand."""
    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    summary_path = tmp_path / "nested" / "deep" / "watch-summary.txt"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
            "--summary-file",
            str(summary_path),
        ],
    )
    assert result.exit_code == 0
    assert summary_path.exists()


def test_watch_summary_file_json_with_skips(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON payload includes total_skip_seconds and skip count when applicable."""
    import json

    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    summary_path = tmp_path / "watch-summary-skipped.json"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--max-cycles",
            "3",
            "--interval",
            "60",
            "--on-change",
            "--summary-file",
            str(summary_path),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["cycles_run"] == 1
    assert payload["cycles_skipped"] == 2
    # 2 skipped x 60s interval = 120s
    assert payload["total_skip_seconds"] == 120


# -- watch --summary-file rotation (v0.50.0 follow-on Step 1) -----------


def test_watch_summary_file_timestamp_placeholder_resolves(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``{timestamp}`` in the path is substituted with a UTC ISO timestamp."""
    import re

    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    template = str(tmp_path / "summary-{timestamp}.txt")
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
            "--summary-file",
            template,
        ],
    )
    assert result.exit_code == 0
    # The literal {timestamp} is no longer in the announced path.
    assert "summary written to:" in result.output
    assert "{timestamp}" not in result.output
    # A file matching the template (with a timestamp substituted) exists.
    matches = list(tmp_path.glob("summary-*.txt"))
    assert len(matches) == 1
    fname = matches[0].name
    # Filesystem-safe form: 2026-05-16T14-30-00+00-00 (no colons).
    assert ":" not in fname
    assert re.match(
        r"summary-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\+\d{2}-\d{2}\.txt",
        fname,
    )
    content = matches[0].read_text(encoding="utf-8")
    assert "Watch exited after 1 cycle(s)" in content


def test_watch_summary_file_timestamp_with_json_suffix(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``{timestamp}`` substitution works with the JSON suffix dispatch."""
    import json

    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    template = str(tmp_path / "summary-{timestamp}.json")
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
            "--summary-file",
            template,
        ],
    )
    assert result.exit_code == 0
    matches = list(tmp_path.glob("summary-*.json"))
    assert len(matches) == 1
    payload = json.loads(matches[0].read_text(encoding="utf-8"))
    assert payload["kind"] == "watch_summary"


def test_watch_summary_file_no_placeholder_keeps_path(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``{timestamp}`` the path is used unchanged (backward compat)."""
    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    summary_path = tmp_path / "no-placeholder.txt"
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
            "--summary-file",
            str(summary_path),
        ],
    )
    assert result.exit_code == 0
    assert summary_path.exists()
    # No "summary written to:" announcement when the path is literal.
    assert "summary written to:" not in result.output


def test_resolve_summary_path_helper() -> None:
    """The unit-level helper substitutes the placeholder."""
    import re

    from razor_rooster.report_generator.cli import _resolve_summary_path

    out = _resolve_summary_path(Path("/tmp/summary-{timestamp}.json"))
    fname = out.name
    assert ":" not in fname
    assert re.match(
        r"summary-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\+\d{2}-\d{2}\.json",
        fname,
    )
    # Without the placeholder the path is returned unchanged.
    same = _resolve_summary_path(Path("/tmp/no-placeholder.txt"))
    assert str(same) == "/tmp/no-placeholder.txt"


# -- watch --summary-retention (v0.51.0 follow-on Step 3) ---------------


def test_watch_summary_retention_prunes_old_files(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old summary files matching the same template are pruned."""
    import os
    import time as _time

    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)

    # Pre-create some old summary files.
    old_a = tmp_path / "watch-2026-04-01T00-00-00+00-00.json"
    old_b = tmp_path / "watch-2026-04-15T00-00-00+00-00.json"
    other_pattern = tmp_path / "other-2026-04-01.json"  # different pattern → kept
    old_a.write_text("{}", encoding="utf-8")
    old_b.write_text("{}", encoding="utf-8")
    other_pattern.write_text("{}", encoding="utf-8")
    # Backdate so the files look 60 days old.
    sixty_days_ago = _time.time() - (60 * 86400)
    os.utime(old_a, (sixty_days_ago, sixty_days_ago))
    os.utime(old_b, (sixty_days_ago, sixty_days_ago))
    os.utime(other_pattern, (sixty_days_ago, sixty_days_ago))

    template = str(tmp_path / "watch-{timestamp}.json")
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
            "--summary-file",
            template,
            "--summary-retention",
            "30",
        ],
    )
    assert result.exit_code == 0
    # The pre-created old files matching the watch-*.json pattern are gone.
    assert not old_a.exists()
    assert not old_b.exists()
    # Files with a different filename pattern are not pruned.
    assert other_pattern.exists()
    # The new file (just-written) is present.
    matches = list(tmp_path.glob("watch-2*.json"))
    assert len(matches) == 1
    # Pruning is announced.
    assert "summary retention: pruned 2 file(s)" in result.output


def test_watch_summary_retention_keeps_recent_files(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files newer than the retention window are kept."""
    import os
    import time as _time

    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)

    recent = tmp_path / "watch-2026-05-15T00-00-00+00-00.json"
    recent.write_text("{}", encoding="utf-8")
    # 1 day old.
    one_day_ago = _time.time() - 86400
    os.utime(recent, (one_day_ago, one_day_ago))

    template = str(tmp_path / "watch-{timestamp}.json")
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
            "--summary-file",
            template,
            "--summary-retention",
            "7",
        ],
    )
    assert result.exit_code == 0
    # Recent file kept (1 day < 7 day retention).
    assert recent.exists()
    # No "pruned" announcement.
    assert "summary retention: pruned" not in result.output


def test_watch_summary_retention_requires_timestamp_placeholder(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--summary-retention`` without ``{timestamp}`` is rejected."""
    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
            "--summary-file",
            str(tmp_path / "literal.txt"),
            "--summary-retention",
            "7",
        ],
    )
    assert result.exit_code != 0
    assert "{timestamp}" in (result.output + (result.stderr or ""))


def test_watch_summary_retention_out_of_range(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retention outside [1, 365] is rejected."""
    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
            "--summary-file",
            str(tmp_path / "watch-{timestamp}.json"),
            "--summary-retention",
            "0",
        ],
    )
    assert result.exit_code != 0
    assert "out of range" in (result.output + (result.stderr or ""))


def test_watch_summary_retention_does_not_prune_just_written(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The currently-just-written file is never pruned even with low retention."""
    import time as _time

    import razor_rooster.report_generator.cli as cli_module

    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)

    template = str(tmp_path / "watch-{timestamp}.json")
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "watch",
            "--db",
            str(db_path),
            "--once",
            "--interval",
            "60",
            "--summary-file",
            template,
            "--summary-retention",
            "1",
        ],
    )
    assert result.exit_code == 0
    matches = list(tmp_path.glob("watch-*.json"))
    assert len(matches) == 1
    assert matches[0].is_file()
    # Even if its mtime were older than 1 day, the keep_path
    # check would protect it. Verify by stat:
    age = _time.time() - matches[0].stat().st_mtime
    assert age < 60  # newly written


def test_prune_old_summaries_helper(tmp_path: Path) -> None:
    """The helper prunes by mtime and skips the keep_path."""
    import os
    import time as _time

    from razor_rooster.report_generator.cli import _prune_old_summaries

    template = str(tmp_path / "summary-{timestamp}.txt")
    keep = tmp_path / "summary-2026-05-16T12-00-00+00-00.txt"
    old = tmp_path / "summary-2026-04-01T00-00-00+00-00.txt"
    keep.write_text("keep", encoding="utf-8")
    old.write_text("old", encoding="utf-8")
    sixty_days_ago = _time.time() - (60 * 86400)
    os.utime(old, (sixty_days_ago, sixty_days_ago))

    removed = _prune_old_summaries(template=template, retention_days=30, keep_path=keep)
    assert removed == 1
    assert keep.exists()
    assert not old.exists()
