"""T-MON-011 — persistence helpers acceptance tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.monitor.models import (
    FollowUp,
    MonitorCycle,
)
from razor_rooster.monitor.persistence.migrations import (
    run_pending_monitor_migrations,
)
from razor_rooster.monitor.persistence.operations import (
    add_note,
    complete_cycle,
    get_follow_up,
    persist_follow_up,
    query_alerts,
    query_cycle,
    query_follow_ups,
    query_notes,
    query_trajectory,
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
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "monitor_ops.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_polymarket_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_position_engine_migrations(c)
        run_pending_monitor_migrations(c)
        yield c
    store.close()


def _cycle(cycle_id: str = "cy-1") -> MonitorCycle:
    return MonitorCycle(
        cycle_id=cycle_id,
        started_at=datetime(2026, 5, 15, 14, tzinfo=UTC),
        completed_at=None,
        follow_ups_total=0,
        follow_ups_with_alerts=0,
        alerts_by_tier={},
    )


def _follow_up(
    follow_up_id: str = "fu-1",
    *,
    cycle_id: str = "cy-1",
    analysis_id: str = "an-1",
    model_shift_band: str | None = "minor",
    primary_alert_tier: str | None = None,
    recommended_review: bool = False,
    resolution_status: str = "unresolved",
    days_since: int = 5,
    when: datetime | None = None,
) -> FollowUp:
    return FollowUp(
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
        model_shift_band=model_shift_band,  # type: ignore[arg-type]
        market_probability_shift=0.02,
        market_shift_band="minor",
        precursor_snapshot=(
            {
                "variable_id": "v1",
                "title": "Synthetic precursor",
                "threshold": 5.0,
                "direction": "high_signals_event",
                "analysis_value": 8.0,
                "current_value": 9.0,
                "analysis_fired": True,
                "current_fired": True,
                "threshold_crossed": False,
            },
        ),
        days_since_analysis=days_since,
        days_to_resolution=180,
        time_decay_alert=False,
        invalidation_evaluations=(
            {
                "criterion": {"type": "precursor_shift", "variable_id": "v1"},
                "status": "not_triggered",
            },
        ),
        invalidation_triggered_count=0,
        resolution_status=resolution_status,  # type: ignore[arg-type]
        recommended_review=recommended_review,
        primary_alert_tier=primary_alert_tier,  # type: ignore[arg-type]
        alert_tiers=(primary_alert_tier,) if primary_alert_tier else (),  # type: ignore[arg-type]
        reasoning_text="Synthetic follow-up reasoning text.",
        computed_at=when or datetime(2026, 5, 15, 14, 1, tzinfo=UTC),
    )


def test_write_then_query_cycle(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    fetched = query_cycle(conn, cycle_id="cy-1")
    assert fetched is not None
    assert fetched.cycle_id == "cy-1"


def test_complete_cycle_updates_aggregates(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    complete_cycle(
        conn,
        cycle_id="cy-1",
        completed_at=datetime(2026, 5, 15, 14, 5, tzinfo=UTC),
        follow_ups_total=3,
        follow_ups_with_alerts=1,
        alerts_by_tier={"material_shift": 1},
        duration_seconds=2.5,
    )
    fetched = query_cycle(conn, cycle_id="cy-1")
    assert fetched is not None
    assert fetched.follow_ups_total == 3
    assert fetched.follow_ups_with_alerts == 1
    assert dict(fetched.alerts_by_tier) == {"material_shift": 1}


def test_persist_follow_up_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_follow_up(conn, _follow_up())
    fetched = get_follow_up(conn, follow_up_id="fu-1")
    assert fetched is not None
    assert fetched.analysis_id == "an-1"
    assert fetched.model_shift_band == "minor"
    assert len(fetched.precursor_snapshot) == 1
    assert fetched.precursor_snapshot[0]["variable_id"] == "v1"


def test_persist_follow_up_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_follow_up(conn, _follow_up(model_shift_band="minor"))
    persist_follow_up(conn, _follow_up(model_shift_band="material"))
    fetched = get_follow_up(conn, follow_up_id="fu-1")
    assert fetched is not None
    assert fetched.model_shift_band == "material"
    rows = conn.execute("SELECT COUNT(*) FROM follow_ups WHERE follow_up_id = 'fu-1'").fetchone()
    assert rows is not None and rows[0] == 1


def test_query_alerts_orders_by_tier(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_follow_up(
        conn,
        _follow_up(
            follow_up_id="fu-time",
            primary_alert_tier="time_decay",
            recommended_review=True,
        ),
    )
    persist_follow_up(
        conn,
        _follow_up(
            follow_up_id="fu-resolution",
            primary_alert_tier="resolution",
            resolution_status="resolved_yes",
            recommended_review=True,
        ),
    )
    persist_follow_up(
        conn,
        _follow_up(
            follow_up_id="fu-shift",
            primary_alert_tier="material_shift",
            recommended_review=True,
        ),
    )
    persist_follow_up(
        conn,
        _follow_up(follow_up_id="fu-none", primary_alert_tier=None),
    )
    alerts = query_alerts(conn)
    # resolution comes first, then material_shift, then time_decay.
    assert [a.follow_up_id for a in alerts] == [
        "fu-resolution",
        "fu-shift",
        "fu-time",
    ]


def test_query_alerts_filter_by_tier(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_follow_up(
        conn,
        _follow_up(
            follow_up_id="fu-1", primary_alert_tier="material_shift", recommended_review=True
        ),
    )
    persist_follow_up(
        conn,
        _follow_up(follow_up_id="fu-2", primary_alert_tier="time_decay", recommended_review=True),
    )
    only_material = query_alerts(conn, tier="material_shift")
    assert {a.follow_up_id for a in only_material} == {"fu-1"}


def test_query_alerts_since_filter(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    old_when = datetime(2026, 5, 10, tzinfo=UTC)
    new_when = datetime(2026, 5, 20, tzinfo=UTC)
    persist_follow_up(
        conn,
        _follow_up(
            follow_up_id="fu-old",
            primary_alert_tier="material_shift",
            recommended_review=True,
            when=old_when,
        ),
    )
    persist_follow_up(
        conn,
        _follow_up(
            follow_up_id="fu-new",
            primary_alert_tier="material_shift",
            recommended_review=True,
            when=new_when,
        ),
    )
    cutoff = datetime(2026, 5, 15, tzinfo=UTC)
    recent = query_alerts(conn, since=cutoff)
    assert {a.follow_up_id for a in recent} == {"fu-new"}


def test_query_trajectory_chronological(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_follow_up(
        conn,
        _follow_up(
            follow_up_id="fu-d1",
            analysis_id="an-target",
            when=datetime(2026, 5, 11, tzinfo=UTC),
        ),
    )
    persist_follow_up(
        conn,
        _follow_up(
            follow_up_id="fu-d2",
            analysis_id="an-target",
            when=datetime(2026, 5, 12, tzinfo=UTC),
        ),
    )
    persist_follow_up(
        conn,
        _follow_up(
            follow_up_id="fu-d3",
            analysis_id="an-target",
            when=datetime(2026, 5, 13, tzinfo=UTC),
        ),
    )
    persist_follow_up(
        conn,
        _follow_up(
            follow_up_id="fu-other",
            analysis_id="an-other",
            when=datetime(2026, 5, 13, tzinfo=UTC),
        ),
    )
    trajectory = query_trajectory(conn, analysis_id="an-target")
    assert [f.follow_up_id for f in trajectory] == ["fu-d1", "fu-d2", "fu-d3"]


def test_query_follow_ups_filter(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle("cy-1"))
    write_cycle(conn, _cycle("cy-2"))
    persist_follow_up(conn, _follow_up(follow_up_id="fu-1", cycle_id="cy-1"))
    persist_follow_up(
        conn,
        _follow_up(follow_up_id="fu-2", cycle_id="cy-2", analysis_id="an-2"),
    )
    by_cycle = query_follow_ups(conn, cycle_id="cy-1")
    by_analysis = query_follow_ups(conn, analysis_id="an-2")
    assert len(by_cycle) == 1
    assert by_cycle[0].follow_up_id == "fu-1"
    assert {f.follow_up_id for f in by_analysis} == {"fu-2"}


def test_add_note_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_follow_up(conn, _follow_up())
    note = add_note(
        conn,
        follow_up_id="fu-1",
        note_text="Reviewed; deciding to hold.",
        when=datetime(2026, 5, 15, 15, tzinfo=UTC),
    )
    assert note.note_id
    notes = query_notes(conn, follow_up_id="fu-1")
    assert len(notes) == 1
    assert "Reviewed" in notes[0].note_text


def test_notes_returned_chronologically_descending(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_follow_up(conn, _follow_up())
    add_note(
        conn,
        follow_up_id="fu-1",
        note_text="first",
        when=datetime(2026, 5, 15, 15, tzinfo=UTC),
    )
    add_note(
        conn,
        follow_up_id="fu-1",
        note_text="second",
        when=datetime(2026, 5, 16, 10, tzinfo=UTC),
    )
    notes = query_notes(conn, follow_up_id="fu-1")
    assert notes[0].note_text == "second"
    assert notes[1].note_text == "first"
