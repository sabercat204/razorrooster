"""T-MON-040 — CLI subcommand tests."""

from __future__ import annotations

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
from razor_rooster.monitor.cli import monitor
from razor_rooster.monitor.models import FollowUp, MonitorCycle
from razor_rooster.monitor.persistence.migrations import (
    run_pending_monitor_migrations,
)
from razor_rooster.monitor.persistence.operations import (
    persist_follow_up,
    write_cycle,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "monitor_cli.duckdb"
    store = DuckDBStore(path)
    with store.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        run_pending_mispricing_migrations(conn)
        run_pending_position_engine_migrations(conn)
        run_pending_monitor_migrations(conn)
    store.close()
    yield path


def _seed_follow_up(
    db_path: Path,
    *,
    cycle_id: str = "cy-1",
    follow_up_id: str = "fu-1",
    analysis_id: str = "an-1",
    primary_alert_tier: str | None = None,
    recommended_review: bool = False,
    resolution_status: str = "unresolved",
    when: datetime | None = None,
) -> None:
    store = DuckDBStore(db_path)
    try:
        with store.connection() as conn:
            write_cycle(
                conn,
                MonitorCycle(
                    cycle_id=cycle_id,
                    started_at=when or datetime(2026, 5, 15, tzinfo=UTC),
                    completed_at=None,
                    follow_ups_total=1,
                    follow_ups_with_alerts=1 if primary_alert_tier else 0,
                    alerts_by_tier={primary_alert_tier: 1} if primary_alert_tier else {},
                ),
            )
            persist_follow_up(
                conn,
                FollowUp(
                    follow_up_id=follow_up_id,
                    cycle_id=cycle_id,
                    analysis_id=analysis_id,
                    analysis_model_p=0.30,
                    analysis_market_p=0.10,
                    analysis_computed_at=datetime(2026, 5, 10, tzinfo=UTC),
                    current_scan_id="scan-current",
                    current_model_p=0.32,
                    current_model_ci=(0.20, 0.45),
                    current_market_p=0.12,
                    current_market_snapshot_ts=datetime(2026, 5, 15, 12, tzinfo=UTC),
                    model_probability_shift=0.02,
                    model_shift_band="minor",
                    market_probability_shift=0.02,
                    market_shift_band="minor",
                    precursor_snapshot=(),
                    days_since_analysis=5,
                    days_to_resolution=180,
                    time_decay_alert=False,
                    invalidation_evaluations=(),
                    invalidation_triggered_count=0,
                    resolution_status=resolution_status,  # type: ignore[arg-type]
                    recommended_review=recommended_review,
                    primary_alert_tier=primary_alert_tier,  # type: ignore[arg-type]
                    alert_tiers=(primary_alert_tier,)  # type: ignore[arg-type]
                    if primary_alert_tier
                    else (),
                    reasoning_text="Synthetic reasoning text for CLI test.",
                    computed_at=when or datetime(2026, 5, 15, 14, tzinfo=UTC),
                ),
            )
    finally:
        store.close()


def test_monitor_version_subcommand(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(monitor, ["version"])
    assert result.exit_code == 0
    assert "6001+" in result.output


def test_monitor_run_with_no_watched(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(monitor, ["run", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "follow_ups_total: 0" in result.output


def test_monitor_show_existing_follow_up(db_path: Path) -> None:
    _seed_follow_up(db_path)
    runner = CliRunner()
    result = runner.invoke(monitor, ["show", "fu-1", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "follow_up_id: fu-1" in result.output
    assert "Synthetic reasoning text" in result.output


def test_monitor_show_missing_follow_up(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(monitor, ["show", "nope", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "No follow-up found" in result.output


def test_monitor_list_alerts_empty(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(monitor, ["list-alerts", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "No alerts" in result.output


def test_monitor_list_alerts_with_filter(db_path: Path) -> None:
    _seed_follow_up(
        db_path,
        follow_up_id="fu-mat",
        primary_alert_tier="material_shift",
        recommended_review=True,
    )
    _seed_follow_up(
        db_path,
        cycle_id="cy-2",
        follow_up_id="fu-time",
        primary_alert_tier="time_decay",
        recommended_review=True,
    )
    runner = CliRunner()
    result = runner.invoke(
        monitor,
        ["list-alerts", "--tier", "material_shift", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    assert "fu-mat" in result.output
    assert "fu-time" not in result.output


def test_monitor_list_alerts_invalid_since(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        monitor,
        ["list-alerts", "--since", "not-a-date", "--db", str(db_path)],
    )
    assert result.exit_code == 1
    assert "Invalid --since" in result.output


def test_monitor_trajectory_chronological(db_path: Path) -> None:
    _seed_follow_up(
        db_path,
        cycle_id="cy-d1",
        follow_up_id="fu-d1",
        analysis_id="an-target",
        when=datetime(2026, 5, 11, 14, tzinfo=UTC),
    )
    _seed_follow_up(
        db_path,
        cycle_id="cy-d2",
        follow_up_id="fu-d2",
        analysis_id="an-target",
        when=datetime(2026, 5, 12, 14, tzinfo=UTC),
    )
    runner = CliRunner()
    result = runner.invoke(monitor, ["trajectory", "an-target", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    # Both follow-ups present, in chronological order.
    idx_d1 = result.output.find("fu-d1")
    idx_d2 = result.output.find("fu-d2")
    assert idx_d1 != -1 and idx_d2 != -1
    assert idx_d1 < idx_d2


def test_monitor_trajectory_no_history(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(monitor, ["trajectory", "ghost", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "No follow-ups found" in result.output


def test_monitor_note_appends(db_path: Path) -> None:
    _seed_follow_up(db_path)
    runner = CliRunner()
    result = runner.invoke(
        monitor,
        ["note", "fu-1", "Reviewed; deciding to hold.", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    assert "note_id:" in result.output
    # The note should now show up in `show`.
    show_result = runner.invoke(monitor, ["show", "fu-1", "--db", str(db_path)])
    assert "Reviewed; deciding to hold." in show_result.output


def test_monitor_note_missing_follow_up(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        monitor,
        ["note", "nope", "anything", "--db", str(db_path)],
    )
    assert result.exit_code == 1
    assert "No follow-up found" in result.output


def test_monitor_evaluate_missing_analysis(db_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(monitor, ["evaluate", "ghost", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "No analysis found" in result.output
