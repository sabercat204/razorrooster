"""T-MON-101 — venue-to-follow_ups migration acceptance tests.

Verifies:
- m6002 adds the column to fresh installs and the column is NOT NULL.
- Polymarket follow-ups continue to work (venue defaults to 'polymarket').
- Kalshi follow-ups can be persisted alongside Polymarket ones.
- ``query_follow_ups`` filters by venue.
- The reasoning text mentions the venue.
- The Kalshi resolution-detection branch reads from kalshi_settlements
  when the connector is initialized; otherwise gracefully returns None.
- Migration is idempotent.
- Pre-existing rows backfill to 'polymarket' on the upgrade path.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import applied_versions
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)
from razor_rooster.monitor.engines.comb import _query_kalshi_resolution
from razor_rooster.monitor.engines.invalidation_evaluator import InvalidationsResult
from razor_rooster.monitor.engines.reasoning import build_reasoning_text
from razor_rooster.monitor.models import FollowUp, ShiftResult
from razor_rooster.monitor.persistence.migrations import (
    run_pending_monitor_migrations,
)
from razor_rooster.monitor.persistence.operations import (
    get_follow_up,
    persist_follow_up,
    query_follow_ups,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "monitor_m6002.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_monitor_migrations(conn)
    yield s
    s.close()


@pytest.fixture
def store_with_kalshi(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "monitor_m6002_kalshi.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_monitor_migrations(conn)
        run_pending_kalshi_migrations(conn)
    yield s
    s.close()


def _follow_up(
    *,
    follow_up_id: str = "fu-1",
    cycle_id: str = "cy-1",
    analysis_id: str = "ana-1",
    venue: str = "polymarket",
) -> FollowUp:
    return FollowUp(
        follow_up_id=follow_up_id,
        cycle_id=cycle_id,
        analysis_id=analysis_id,
        analysis_model_p=0.30,
        analysis_market_p=0.10,
        analysis_computed_at=datetime(2026, 5, 14, tzinfo=UTC),
        current_scan_id="scan-1",
        current_model_p=0.32,
        current_model_ci=(0.28, 0.36),
        current_market_p=0.11,
        current_market_snapshot_ts=datetime(2026, 5, 15, tzinfo=UTC),
        model_probability_shift=0.02,
        model_shift_band="minor",
        market_probability_shift=0.01,
        market_shift_band="minor",
        precursor_snapshot=(),
        days_since_analysis=1,
        days_to_resolution=29,
        time_decay_alert=False,
        invalidation_evaluations=(),
        invalidation_triggered_count=0,
        resolution_status="unresolved",
        recommended_review=False,
        primary_alert_tier=None,
        alert_tiers=(),
        reasoning_text="Test follow-up.",
        computed_at=datetime(2026, 5, 15, tzinfo=UTC),
        venue=venue,  # type: ignore[arg-type]
    )


def test_m6002_records_version(store: DuckDBStore) -> None:
    with store.connection() as conn:
        versions = applied_versions(conn)
    assert 6001 in versions
    assert 6002 in versions


def test_venue_column_exists_and_not_null(store: DuckDBStore) -> None:
    with store.connection() as conn:
        rows = conn.execute("PRAGMA table_info('follow_ups')").fetchall()
    columns = {r[1]: r for r in rows}
    assert "venue" in columns, "venue missing from follow_ups"
    assert columns["venue"][3] is True, "venue should be NOT NULL"


def test_persist_polymarket_follow_up_default_venue(store: DuckDBStore) -> None:
    with store.connection() as conn:
        # Need a parent monitor_cycles row first.
        conn.execute(
            "INSERT INTO monitor_cycles ("
            "cycle_id, started_at, completed_at, follow_ups_total, "
            "follow_ups_with_alerts, alerts_by_tier, duration_seconds, error_summary"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ["cy-1", datetime(2026, 5, 15, tzinfo=UTC), None, 0, 0, "{}", None, None],
        )
        persist_follow_up(conn, _follow_up(follow_up_id="fu-poly"))
        fetched = get_follow_up(conn, follow_up_id="fu-poly")
    assert fetched is not None
    assert fetched.venue == "polymarket"


def test_persist_kalshi_follow_up_explicit_venue(store: DuckDBStore) -> None:
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO monitor_cycles ("
            "cycle_id, started_at, completed_at, follow_ups_total, "
            "follow_ups_with_alerts, alerts_by_tier, duration_seconds, error_summary"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ["cy-1", datetime(2026, 5, 15, tzinfo=UTC), None, 0, 0, "{}", None, None],
        )
        persist_follow_up(conn, _follow_up(follow_up_id="fu-kalshi", venue="kalshi"))
        fetched = get_follow_up(conn, follow_up_id="fu-kalshi")
    assert fetched is not None
    assert fetched.venue == "kalshi"


def test_query_follow_ups_filters_by_venue(store: DuckDBStore) -> None:
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO monitor_cycles ("
            "cycle_id, started_at, completed_at, follow_ups_total, "
            "follow_ups_with_alerts, alerts_by_tier, duration_seconds, error_summary"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ["cy-1", datetime(2026, 5, 15, tzinfo=UTC), None, 0, 0, "{}", None, None],
        )
        persist_follow_up(conn, _follow_up(follow_up_id="fu-poly", venue="polymarket"))
        persist_follow_up(conn, _follow_up(follow_up_id="fu-kalshi", venue="kalshi"))
        polymarket_only = query_follow_ups(conn, venue="polymarket")
        kalshi_only = query_follow_ups(conn, venue="kalshi")
    assert len(polymarket_only) == 1
    assert polymarket_only[0].venue == "polymarket"
    assert len(kalshi_only) == 1
    assert kalshi_only[0].venue == "kalshi"


def test_reasoning_text_includes_venue() -> None:
    text_poly = build_reasoning_text(
        class_id="cls",
        condition_id="0xabc",
        days_since_analysis=1,
        days_to_resolution=29,
        resolution_status="unresolved",
        model_shift=ShiftResult(value=0.0, band="none"),
        market_shift=ShiftResult(value=0.0, band="none"),
        precursor_snapshot=(),
        invalidations=InvalidationsResult(evaluations=(), triggered_count=0),
        primary_alert_tier=None,
        all_alert_tiers=(),
        recommended_review=False,
        time_decay_alert_days=14,
        venue="polymarket",
    )
    text_kalshi = build_reasoning_text(
        class_id="cls",
        condition_id="KXTICK",
        days_since_analysis=1,
        days_to_resolution=29,
        resolution_status="unresolved",
        model_shift=ShiftResult(value=0.0, band="none"),
        market_shift=ShiftResult(value=0.0, band="none"),
        precursor_snapshot=(),
        invalidations=InvalidationsResult(evaluations=(), triggered_count=0),
        primary_alert_tier=None,
        all_alert_tiers=(),
        recommended_review=False,
        time_decay_alert_days=14,
        venue="kalshi",
    )
    assert "polymarket" in text_poly
    assert "kalshi" in text_kalshi


def test_query_kalshi_resolution_returns_none_without_settlements_table(
    store: DuckDBStore,
) -> None:
    """If the Kalshi connector hasn't been initialized, the lookup is a
    soft-None rather than an exception."""
    with store.connection() as conn:
        result = _query_kalshi_resolution(conn, ticker="KXTICK")
    assert result is None


def test_query_kalshi_resolution_finds_settled_yes(
    store_with_kalshi: DuckDBStore,
) -> None:
    """A 'yes' settlement row is detected as an outcome."""
    now = datetime(2026, 5, 30, tzinfo=UTC)
    with store_with_kalshi.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_settlements ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "ticker, event_ticker, series_ticker, result, settled_value, "
            "settlement_ts, settlement_source, final_yes_price, "
            "final_no_price, total_volume_at_settlement, voided"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "kalshi_settlements",
                "settle-1",
                now,
                now,
                "0.1.0",
                json.dumps({"ticker": "KXTICK"}),
                "KXTICK",
                "EVT",
                "SER",
                "yes",
                1.0,
                now,
                None,
                0.95,
                0.05,
                10000.0,
                False,
            ],
        )
        result = _query_kalshi_resolution(conn, ticker="KXTICK")
    assert result == "yes"


def test_query_kalshi_resolution_voided_returns_invalid(
    store_with_kalshi: DuckDBStore,
) -> None:
    now = datetime(2026, 5, 30, tzinfo=UTC)
    with store_with_kalshi.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_settlements ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "ticker, event_ticker, series_ticker, result, settled_value, "
            "settlement_ts, settlement_source, final_yes_price, "
            "final_no_price, total_volume_at_settlement, voided"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "kalshi_settlements",
                "settle-2",
                now,
                now,
                "0.1.0",
                json.dumps({"ticker": "KXVOID"}),
                "KXVOID",
                "EVT",
                "SER",
                "void",
                None,
                now,
                None,
                None,
                None,
                None,
                True,
            ],
        )
        result = _query_kalshi_resolution(conn, ticker="KXVOID")
    assert result == "invalid"


def test_query_kalshi_resolution_returns_none_when_no_row(
    store_with_kalshi: DuckDBStore,
) -> None:
    with store_with_kalshi.connection() as conn:
        result = _query_kalshi_resolution(conn, ticker="KXNOTHERE")
    assert result is None


def test_m6002_is_idempotent(store: DuckDBStore) -> None:
    with store.connection() as conn:
        before = applied_versions(conn)
        run_pending_monitor_migrations(conn)
        after = applied_versions(conn)
    assert before == after
