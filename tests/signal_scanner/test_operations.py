"""T-SCAN-011 — persistence helpers acceptance tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.signal_scanner.models import ScanRecord, ScanSummary, Trace
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)
from razor_rooster.signal_scanner.persistence.operations import (
    PruneConfirmationError,
    complete_summary,
    persist_record,
    persist_trace,
    prune_before,
    query_recent_candidates,
    query_scan_records,
    query_scan_summary,
    query_trace,
    write_summary,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "scan_ops.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        yield c
    store.close()


def _summary(scan_id: str, *, started: datetime) -> ScanSummary:
    return ScanSummary(
        scan_id=scan_id,
        scan_started_at=started,
        scan_completed_at=None,
        pattern_library_version=1,
        classes_total=8,
        classes_succeeded=0,
        classes_failed=0,
        classes_skipped=0,
        candidates_count=0,
    )


def _record(
    scan_id: str,
    class_id: str,
    *,
    started: datetime,
    is_candidate: bool = False,
    posterior: float = 0.1,
    base_rate: float = 0.05,
) -> ScanRecord:
    return ScanRecord(
        scan_id=scan_id,
        class_id=class_id,
        class_definition_version=1,
        pattern_library_version=1,
        data_as_of=started,
        scan_started_at=started,
        scan_completed_at=started + timedelta(seconds=1),
        base_rate=base_rate,
        base_rate_ci_lower=base_rate * 0.5,
        base_rate_ci_upper=base_rate * 1.5,
        posterior=posterior,
        posterior_ci_lower=posterior * 0.7,
        posterior_ci_upper=posterior * 1.3,
        log_odds_shift=0.6,
        is_candidate=is_candidate,
        candidate_direction=("elevated" if is_candidate else None),
    )


def test_write_summary_then_query(conn: duckdb.DuckDBPyConnection) -> None:
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    write_summary(conn, _summary("scan-001", started=started))
    fetched = query_scan_summary(conn, scan_id="scan-001")
    assert fetched is not None
    assert fetched.scan_id == "scan-001"
    assert fetched.classes_total == 8
    assert fetched.scan_completed_at is None


def test_write_summary_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    write_summary(conn, _summary("scan-002", started=started))
    write_summary(conn, _summary("scan-002", started=started))
    rows = conn.execute("SELECT COUNT(*) FROM scan_summaries WHERE scan_id = 'scan-002'").fetchone()
    assert rows is not None and rows[0] == 1


def test_complete_summary_updates_aggregates(conn: duckdb.DuckDBPyConnection) -> None:
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    write_summary(conn, _summary("scan-003", started=started))
    completed = started + timedelta(minutes=2)
    complete_summary(
        conn,
        scan_id="scan-003",
        completed_at=completed,
        classes_succeeded=7,
        classes_failed=1,
        classes_skipped=0,
        candidates_count=2,
    )
    fetched = query_scan_summary(conn, scan_id="scan-003")
    assert fetched is not None
    assert fetched.scan_completed_at == completed
    assert fetched.classes_succeeded == 7
    assert fetched.classes_failed == 1
    assert fetched.candidates_count == 2


def test_persist_record_roundtrip(conn: duckdb.DuckDBPyConnection) -> None:
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    write_summary(conn, _summary("scan-004", started=started))
    persist_record(conn, _record("scan-004", "class-a", started=started))
    persist_record(conn, _record("scan-004", "class-b", started=started, is_candidate=True))
    records = query_scan_records(conn, scan_id="scan-004")
    assert len(records) == 2
    assert {r.class_id for r in records} == {"class-a", "class-b"}
    candidate = next(r for r in records if r.class_id == "class-b")
    assert candidate.is_candidate is True
    assert candidate.candidate_direction == "elevated"


def test_persist_record_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    write_summary(conn, _summary("scan-005", started=started))
    persist_record(conn, _record("scan-005", "class-a", started=started, posterior=0.2))
    # Re-insert with a different posterior — should update, not duplicate.
    persist_record(conn, _record("scan-005", "class-a", started=started, posterior=0.3))
    rows = conn.execute(
        "SELECT COUNT(*), MAX(posterior) FROM scan_records "
        "WHERE scan_id = 'scan-005' AND class_id = 'class-a'"
    ).fetchone()
    assert rows is not None
    assert rows[0] == 1
    assert rows[1] == pytest.approx(0.3)


def test_persist_trace_and_query(conn: duckdb.DuckDBPyConnection) -> None:
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    write_summary(conn, _summary("scan-006", started=started))
    payload = {
        "class_id": "class-a",
        "prior": {"point": 0.05, "ci": [0.02, 0.10]},
        "precursors": [{"variable_id": "v1", "fired": True}],
    }
    persist_trace(conn, Trace(scan_id="scan-006", class_id="class-a", payload=payload))
    fetched = query_trace(conn, scan_id="scan-006", class_id="class-a")
    assert fetched is not None
    assert fetched.payload["class_id"] == "class-a"
    assert fetched.payload["precursors"][0]["fired"] is True


def test_query_recent_candidates_filters_correctly(conn: duckdb.DuckDBPyConnection) -> None:
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    write_summary(conn, _summary("scan-007", started=started))
    persist_record(conn, _record("scan-007", "non-cand", started=started))
    persist_record(conn, _record("scan-007", "cand-1", started=started, is_candidate=True))
    persist_record(conn, _record("scan-007", "cand-2", started=started, is_candidate=True))
    candidates = query_recent_candidates(conn)
    assert {c.class_id for c in candidates} == {"cand-1", "cand-2"}
    candidates = query_recent_candidates(conn, since=started + timedelta(days=1))
    assert candidates == ()


def test_prune_requires_confirm(conn: duckdb.DuckDBPyConnection) -> None:
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    write_summary(conn, _summary("scan-008", started=started))
    with pytest.raises(PruneConfirmationError):
        prune_before(conn, before=started + timedelta(days=1), confirm=False)


def test_prune_deletes_old_scans(conn: duckdb.DuckDBPyConnection) -> None:
    started_old = datetime(2025, 1, 1, 8, tzinfo=UTC)
    started_new = datetime(2026, 5, 15, 8, tzinfo=UTC)
    write_summary(conn, _summary("scan-old", started=started_old))
    write_summary(conn, _summary("scan-new", started=started_new))
    persist_record(conn, _record("scan-old", "class-a", started=started_old))
    persist_record(conn, _record("scan-new", "class-a", started=started_new))
    persist_trace(conn, Trace(scan_id="scan-old", class_id="class-a", payload={"x": 1}))
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    deleted = prune_before(conn, before=cutoff, confirm=True)
    assert deleted == 1
    # scan-old gone, scan-new remains
    summary_old = query_scan_summary(conn, scan_id="scan-old")
    summary_new = query_scan_summary(conn, scan_id="scan-new")
    assert summary_old is None
    assert summary_new is not None
    # Records and traces also cleaned.
    rec_old = conn.execute(
        "SELECT COUNT(*) FROM scan_records WHERE scan_id = 'scan-old'"
    ).fetchone()
    trace_old = conn.execute(
        "SELECT COUNT(*) FROM scan_traces WHERE scan_id = 'scan-old'"
    ).fetchone()
    assert rec_old is not None and rec_old[0] == 0
    assert trace_old is not None and trace_old[0] == 0


def test_persist_record_immutability_across_scans(conn: duckdb.DuckDBPyConnection) -> None:
    """Two scans on the same date produce two distinct rows (REQ-SCAN-PERSIST-003)."""
    started_a = datetime(2026, 5, 15, 8, tzinfo=UTC)
    started_b = datetime(2026, 5, 15, 14, tzinfo=UTC)
    write_summary(conn, _summary("scan-a", started=started_a))
    write_summary(conn, _summary("scan-b", started=started_b))
    persist_record(conn, _record("scan-a", "class-a", started=started_a, posterior=0.1))
    persist_record(conn, _record("scan-b", "class-a", started=started_b, posterior=0.2))
    rows = conn.execute("SELECT COUNT(*) FROM scan_records WHERE class_id = 'class-a'").fetchone()
    assert rows is not None and rows[0] == 2
