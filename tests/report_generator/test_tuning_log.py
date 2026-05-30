"""Tests for the threshold-tuning log (T-RG-COMPAT-TUNINGLOG-001 v0.43.0).

Covers:
- Persistence helpers: round-trip insert + list with filters.
- Generator/CLI integration: a successful --apply writes a log row.
- CLI: razor-rooster report tuning-log lists entries.
- Apply path survives a tuning-log write failure.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.report_generator.cli import report as report_cli
from razor_rooster.report_generator.engines.measurements import compute_distribution
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import (
    ThresholdTuningLogEntry,
    list_tuning_log_entries,
    persist_threshold_measurement,
    persist_tuning_log_entry,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "tuning_log.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_report_generator_migrations(c)
    with store.connection() as c:
        yield c


def _write_yaml(tmp_path: Path, payload: dict[str, object]) -> Path:
    cfg_path = tmp_path / "report.yaml"
    cfg_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return cfg_path


# -- persistence round-trip -----------------------------------------------


def test_round_trip_persist_and_list(conn: duckdb.DuckDBPyConnection) -> None:
    applied_at = datetime(2026, 5, 15, 12, tzinfo=UTC)
    persist_tuning_log_entry(
        conn,
        log_id="log-1",
        applied_at=applied_at,
        measurement_kind="cross_venue_spread_bps",
        knob="cross_venue_spread_bps",
        previous_value=500.0,
        new_value=750.0,
        target_percentile=0.70,
        backup_path="/tmp/report.yaml.bak.20260515T120000Z",
        note="bumped after measuring p70 jumped",
    )
    entries = list_tuning_log_entries(conn)
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, ThresholdTuningLogEntry)
    assert entry.log_id == "log-1"
    assert entry.applied_at == applied_at
    assert entry.measurement_kind == "cross_venue_spread_bps"
    assert entry.knob == "cross_venue_spread_bps"
    assert entry.previous_value == 500.0
    assert entry.new_value == 750.0
    assert entry.target_percentile == 0.70
    assert entry.backup_path == "/tmp/report.yaml.bak.20260515T120000Z"
    assert entry.note == "bumped after measuring p70 jumped"


def test_persist_with_null_optionals(conn: duckdb.DuckDBPyConnection) -> None:
    """previous_value, target_percentile, backup_path, and note can all be None."""
    persist_tuning_log_entry(
        conn,
        log_id="log-2",
        applied_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
        measurement_kind="brier_per_sector",
        knob="brier_miscalibration",
        previous_value=None,
        new_value=0.18,
        target_percentile=None,
        backup_path=None,
        note=None,
    )
    entries = list_tuning_log_entries(conn)
    assert len(entries) == 1
    assert entries[0].previous_value is None
    assert entries[0].target_percentile is None
    assert entries[0].backup_path is None
    assert entries[0].note is None


def test_filter_by_kind(conn: duckdb.DuckDBPyConnection) -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    persist_tuning_log_entry(
        conn,
        log_id="log-cv",
        applied_at=base,
        measurement_kind="cross_venue_spread_bps",
        knob="cross_venue_spread_bps",
        previous_value=500.0,
        new_value=600.0,
        target_percentile=0.70,
        backup_path=None,
        note=None,
    )
    persist_tuning_log_entry(
        conn,
        log_id="log-brier",
        applied_at=base,
        measurement_kind="brier_per_sector",
        knob="brier_miscalibration",
        previous_value=0.25,
        new_value=0.18,
        target_percentile=0.50,
        backup_path=None,
        note=None,
    )
    cv_only = list_tuning_log_entries(conn, measurement_kind="cross_venue_spread_bps")
    assert len(cv_only) == 1
    assert cv_only[0].log_id == "log-cv"


def test_filter_by_since(conn: duckdb.DuckDBPyConnection) -> None:
    persist_tuning_log_entry(
        conn,
        log_id="log-old",
        applied_at=datetime(2026, 5, 1, 12, tzinfo=UTC),
        measurement_kind="cross_venue_spread_bps",
        knob="cross_venue_spread_bps",
        previous_value=500.0,
        new_value=600.0,
        target_percentile=0.70,
        backup_path=None,
        note=None,
    )
    persist_tuning_log_entry(
        conn,
        log_id="log-new",
        applied_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
        measurement_kind="cross_venue_spread_bps",
        knob="cross_venue_spread_bps",
        previous_value=600.0,
        new_value=700.0,
        target_percentile=0.70,
        backup_path=None,
        note=None,
    )
    recent = list_tuning_log_entries(conn, since=datetime(2026, 5, 10, tzinfo=UTC))
    assert len(recent) == 1
    assert recent[0].log_id == "log-new"


def test_results_ordered_newest_first(conn: duckdb.DuckDBPyConnection) -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    for i in range(3):
        persist_tuning_log_entry(
            conn,
            log_id=f"log-{i}",
            applied_at=base + timedelta(hours=i),
            measurement_kind="cross_venue_spread_bps",
            knob="cross_venue_spread_bps",
            previous_value=500.0 + i,
            new_value=600.0 + i,
            target_percentile=0.70,
            backup_path=None,
            note=None,
        )
    entries = list_tuning_log_entries(conn)
    assert [e.log_id for e in entries] == ["log-2", "log-1", "log-0"]


# -- CLI --apply integration ----------------------------------------------


def test_apply_writes_tuning_log_entry(tmp_path: Path) -> None:
    """A successful --apply persists a tuning-log row."""
    db_path = tmp_path / "apply_log.duckdb"
    cfg_path = _write_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([200.0, 600.0, 1100.0], threshold=500.0),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--target-pct",
            "0.70",
            "--apply",
            "--yes",
            "--config",
            str(cfg_path),
            "--note",
            "bumped after measuring p70 jumped",
        ],
    )
    assert result.exit_code == 0
    store2 = DuckDBStore(db_path)
    try:
        with store2.connection() as c:
            entries = list_tuning_log_entries(c)
    finally:
        store2.close()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.measurement_kind == "cross_venue_spread_bps"
    assert entry.knob == "cross_venue_spread_bps"
    assert entry.previous_value == 500.0
    assert entry.target_percentile == 0.70
    assert entry.note == "bumped after measuring p70 jumped"
    assert entry.backup_path is not None
    assert ".bak." in entry.backup_path


def test_apply_without_note_writes_null_note(tmp_path: Path) -> None:
    """--note is optional; tuning-log row is still written."""
    db_path = tmp_path / "apply_no_note.duckdb"
    cfg_path = _write_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([200.0, 600.0, 1100.0], threshold=500.0),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--target-pct",
            "0.70",
            "--apply",
            "--yes",
            "--config",
            str(cfg_path),
        ],
    )
    assert result.exit_code == 0
    store2 = DuckDBStore(db_path)
    try:
        with store2.connection() as c:
            entries = list_tuning_log_entries(c)
    finally:
        store2.close()
    assert len(entries) == 1
    assert entries[0].note is None


def test_skipped_apply_does_not_write_tuning_log(tmp_path: Path) -> None:
    """Answering "n" at the prompt skips both the apply and the log write."""
    db_path = tmp_path / "skip_log.duckdb"
    cfg_path = _write_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([200.0, 600.0, 1100.0], threshold=500.0),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--target-pct",
            "0.70",
            "--apply",
            "--config",
            str(cfg_path),
        ],
        input="n\n",
    )
    assert result.exit_code == 0
    store2 = DuckDBStore(db_path)
    try:
        with store2.connection() as c:
            entries = list_tuning_log_entries(c)
    finally:
        store2.close()
    assert entries == ()


# -- tuning-log CLI --------------------------------------------------------


def test_cli_tuning_log_lists_entries(tmp_path: Path) -> None:
    db_path = tmp_path / "tl_cli.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_tuning_log_entry(
                c,
                log_id="log-1",
                applied_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                measurement_kind="cross_venue_spread_bps",
                knob="cross_venue_spread_bps",
                previous_value=500.0,
                new_value=750.0,
                target_percentile=0.70,
                backup_path="/tmp/report.yaml.bak.20260515T120000Z",
                note="bumped after measuring p70",
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["tuning-log", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "kind=cross_venue_spread_bps" in result.output
    assert "knob=cross_venue_spread_bps" in result.output
    assert "previous: 500" in result.output
    assert "new: 750" in result.output
    assert "p70" in result.output
    assert "backup: /tmp/report.yaml.bak.20260515T120000Z" in result.output
    assert "note: bumped after measuring p70" in result.output


def test_cli_tuning_log_handles_empty_table(tmp_path: Path) -> None:
    db_path = tmp_path / "tl_empty.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["tuning-log", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "No tuning-log entries yet." in result.output


def test_cli_tuning_log_kind_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "tl_kind.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_tuning_log_entry(
                c,
                log_id="log-cv",
                applied_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                measurement_kind="cross_venue_spread_bps",
                knob="cross_venue_spread_bps",
                previous_value=500.0,
                new_value=750.0,
                target_percentile=0.70,
                backup_path=None,
                note=None,
            )
            persist_tuning_log_entry(
                c,
                log_id="log-brier",
                applied_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                measurement_kind="brier_per_sector",
                knob="brier_miscalibration",
                previous_value=0.25,
                new_value=0.18,
                target_percentile=0.50,
                backup_path=None,
                note=None,
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "tuning-log",
            "--db",
            str(db_path),
            "--kind",
            "brier_per_sector",
        ],
    )
    assert result.exit_code == 0
    assert "kind=brier_per_sector" in result.output
    assert "kind=cross_venue_spread_bps" not in result.output


def test_cli_tuning_log_descriptive_only(tmp_path: Path) -> None:
    """Tuning-log output passes the imperative-language linter."""
    from razor_rooster.position_engine.frame.linter import check_text

    db_path = tmp_path / "tl_lint.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_tuning_log_entry(
                c,
                log_id="log-1",
                applied_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                measurement_kind="cross_venue_spread_bps",
                knob="cross_venue_spread_bps",
                previous_value=500.0,
                new_value=750.0,
                target_percentile=0.70,
                backup_path=None,
                note="bumped after p70 jumped",
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["tuning-log", "--db", str(db_path)])
    assert result.exit_code == 0
    check_text(result.output)


# -- failure isolation -----------------------------------------------------


def test_apply_succeeds_even_if_log_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tuning-log persistence failure doesn't undo the config write."""
    db_path = tmp_path / "log_fail.duckdb"
    cfg_path = _write_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([200.0, 600.0, 1100.0], threshold=500.0),
            )
    finally:
        store.close()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("log persistence blew up")

    monkeypatch.setattr(
        "razor_rooster.report_generator.persistence.operations.persist_tuning_log_entry",
        boom,
    )
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--target-pct",
            "0.70",
            "--apply",
            "--yes",
            "--config",
            str(cfg_path),
        ],
    )
    # The apply still succeeds (exit 0).
    assert result.exit_code == 0
    assert "Applied." in result.output
    # And the YAML was actually updated.
    written = yaml.safe_load(cfg_path.read_text())
    assert written["thresholds"]["cross_venue_spread_bps"] != 500


# -- undo path (T-RG-COMPAT-UNDO-001 v0.44.0) ------------------------------


def test_undo_helper_round_trip(tmp_path: Path) -> None:
    """undo_tuning_log_entry restores from backup and writes a pre-undo backup."""
    from razor_rooster.report_generator.engines.suggestions import (
        undo_tuning_log_entry,
    )

    cfg_path = _write_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 750}},
    )
    backup_path = tmp_path / "report.yaml.bak.original"
    backup_path.write_text(
        yaml.safe_dump({"thresholds": {"cross_venue_spread_bps": 500}}),
        encoding="utf-8",
    )
    result = undo_tuning_log_entry(
        config_path=cfg_path,
        backup_path=backup_path,
        log_id="log-1",
        now=datetime(2026, 5, 16, 14, 30, 0, tzinfo=UTC),
    )
    # Live config now has the backup's contents.
    written = yaml.safe_load(cfg_path.read_text())
    assert written["thresholds"]["cross_venue_spread_bps"] == 500
    # Pre-undo backup captured the v0.42.0 state.
    pre_undo = yaml.safe_load(result.current_backup_path.read_text())
    assert pre_undo["thresholds"]["cross_venue_spread_bps"] == 750
    assert "20260516T143000" in result.current_backup_path.name
    assert result.current_backup_path.name.endswith("Z")
    assert result.restored_from == backup_path
    assert result.log_id_undone == "log-1"


def test_undo_helper_refuses_missing_config(tmp_path: Path) -> None:
    from razor_rooster.report_generator.engines.suggestions import (
        ApplyError,
        undo_tuning_log_entry,
    )

    backup_path = tmp_path / "report.yaml.bak.x"
    backup_path.write_text("thresholds: {}", encoding="utf-8")
    with pytest.raises(ApplyError, match="config file not found"):
        undo_tuning_log_entry(
            config_path=tmp_path / "missing.yaml",
            backup_path=backup_path,
            log_id="log-1",
        )


def test_undo_helper_refuses_missing_backup(tmp_path: Path) -> None:
    from razor_rooster.report_generator.engines.suggestions import (
        ApplyError,
        undo_tuning_log_entry,
    )

    cfg_path = _write_yaml(tmp_path, {"thresholds": {}})
    with pytest.raises(ApplyError, match="no longer exists"):
        undo_tuning_log_entry(
            config_path=cfg_path,
            backup_path=tmp_path / "nonexistent.bak",
            log_id="log-1",
        )


# -- CLI -------------------------------------------------------------------


def test_cli_undo_round_trip(tmp_path: Path) -> None:
    """End-to-end: apply, then undo, restores the original config + logs the undo."""
    db_path = tmp_path / "undo_e2e.duckdb"
    cfg_path = _write_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([200.0, 600.0, 1100.0], threshold=500.0),
            )
    finally:
        store.close()
    runner = CliRunner()
    # Apply.
    apply_result = runner.invoke(
        report_cli,
        [
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--target-pct",
            "0.70",
            "--apply",
            "--yes",
            "--config",
            str(cfg_path),
        ],
    )
    assert apply_result.exit_code == 0
    # Find the log entry we just created.
    store2 = DuckDBStore(db_path)
    try:
        with store2.connection() as c:
            entries_after_apply = list_tuning_log_entries(c)
    finally:
        store2.close()
    assert len(entries_after_apply) == 1
    log_id = entries_after_apply[0].log_id
    # Confirm config changed.
    after_apply = yaml.safe_load(cfg_path.read_text())
    assert after_apply["thresholds"]["cross_venue_spread_bps"] != 500
    # Undo.
    undo_result = runner.invoke(
        report_cli,
        [
            "tuning-log-undo",
            "--db",
            str(db_path),
            "--config",
            str(cfg_path),
            "--yes",
            log_id,
        ],
    )
    assert undo_result.exit_code == 0
    assert "Undone." in undo_result.output
    # Config restored.
    after_undo = yaml.safe_load(cfg_path.read_text())
    assert after_undo["thresholds"]["cross_venue_spread_bps"] == 500
    # A new tuning-log entry recorded the undo.
    store3 = DuckDBStore(db_path)
    try:
        with store3.connection() as c:
            entries_after_undo = list_tuning_log_entries(c)
    finally:
        store3.close()
    assert len(entries_after_undo) == 2
    # Newest is the undo entry.
    undo_entry = entries_after_undo[0]
    assert undo_entry.note is not None
    assert f"undo of log_id={log_id}" in undo_entry.note
    assert undo_entry.backup_path is not None


def test_cli_undo_refuses_unknown_log_id(tmp_path: Path) -> None:
    db_path = tmp_path / "undo_unknown.duckdb"
    cfg_path = _write_yaml(tmp_path, {"thresholds": {}})
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "tuning-log-undo",
            "--db",
            str(db_path),
            "--config",
            str(cfg_path),
            "--yes",
            "log-does-not-exist",
        ],
    )
    assert result.exit_code != 0
    assert "No tuning-log entry found" in (result.output + (result.stderr or ""))


def test_cli_undo_refuses_entry_without_backup_path(tmp_path: Path) -> None:
    """Pre-v0.43.0 entries (or rows where the backup got deleted) fail cleanly."""
    db_path = tmp_path / "undo_no_backup.duckdb"
    cfg_path = _write_yaml(tmp_path, {"thresholds": {}})
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_tuning_log_entry(
                c,
                log_id="log-orphan",
                applied_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                measurement_kind="cross_venue_spread_bps",
                knob="cross_venue_spread_bps",
                previous_value=500.0,
                new_value=750.0,
                target_percentile=0.70,
                backup_path=None,
                note=None,
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "tuning-log-undo",
            "--db",
            str(db_path),
            "--config",
            str(cfg_path),
            "--yes",
            "log-orphan",
        ],
    )
    assert result.exit_code != 0
    assert "no backup_path" in (result.output + (result.stderr or ""))


def test_cli_undo_negative_prompt_skips(tmp_path: Path) -> None:
    """Answering "n" to the undo prompt leaves config unchanged."""
    db_path = tmp_path / "undo_skip.duckdb"
    cfg_path = _write_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 750}},
    )
    backup_path = tmp_path / "report.yaml.bak.original"
    backup_path.write_text(
        yaml.safe_dump({"thresholds": {"cross_venue_spread_bps": 500}}),
        encoding="utf-8",
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_tuning_log_entry(
                c,
                log_id="log-1",
                applied_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                measurement_kind="cross_venue_spread_bps",
                knob="cross_venue_spread_bps",
                previous_value=500.0,
                new_value=750.0,
                target_percentile=0.70,
                backup_path=str(backup_path),
                note=None,
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "tuning-log-undo",
            "--db",
            str(db_path),
            "--config",
            str(cfg_path),
            "log-1",
        ],
        input="n\n",
    )
    assert result.exit_code == 0
    assert "Skipped (no change applied)." in result.output
    written = yaml.safe_load(cfg_path.read_text())
    assert written["thresholds"]["cross_venue_spread_bps"] == 750


def test_cli_undo_descriptive_only(tmp_path: Path) -> None:
    """The undo CLI output passes the imperative-language linter."""
    from razor_rooster.position_engine.frame.linter import check_text

    db_path = tmp_path / "undo_lint.duckdb"
    cfg_path = _write_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 750}},
    )
    backup_path = tmp_path / "report.yaml.bak.original"
    backup_path.write_text(
        yaml.safe_dump({"thresholds": {"cross_venue_spread_bps": 500}}),
        encoding="utf-8",
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_tuning_log_entry(
                c,
                log_id="log-1",
                applied_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                measurement_kind="cross_venue_spread_bps",
                knob="cross_venue_spread_bps",
                previous_value=500.0,
                new_value=750.0,
                target_percentile=0.70,
                backup_path=str(backup_path),
                note=None,
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "tuning-log-undo",
            "--db",
            str(db_path),
            "--config",
            str(cfg_path),
            "--yes",
            "log-1",
        ],
    )
    assert result.exit_code == 0
    check_text(result.output)
