"""T-RG-011 — report_generator persistence helpers tests."""

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
from razor_rooster.report_generator.models import ReportRecord
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import (
    get_report,
    list_reports,
    persist_report,
    query_last_report,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "rg_ops.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_report_generator_migrations(c)
        yield c
    store.close()


def _record(
    *,
    report_id: str = "rep-1",
    when: datetime | None = None,
    markdown_path: str | None = None,
) -> ReportRecord:
    ts = when or datetime(2026, 5, 15, 14, tzinfo=UTC)
    return ReportRecord(
        report_id=report_id,
        generated_at=ts,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=ts,
        sections_enabled=("system_health", "surfaced", "watched"),
        sections_rendered=("system_health", "surfaced", "watched"),
        sections_failed=(),
        library_version=1,
        disclaimer_version_hash="abc123",
        rendered_terminal_text="rendered text",
        rendered_markdown_text=None,
        markdown_path=markdown_path,
        duration_seconds=0.5,
    )


def test_persist_then_get_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    persist_report(conn, _record())
    fetched = get_report(conn, report_id="rep-1")
    assert fetched is not None
    assert fetched.report_id == "rep-1"
    assert fetched.library_version == 1
    assert fetched.sections_enabled == ("system_health", "surfaced", "watched")
    assert fetched.duration_seconds == 0.5


def test_persist_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    persist_report(conn, _record())
    persist_report(
        conn,
        _record(
            markdown_path="/tmp/r.md",
            when=datetime(2026, 5, 15, 14, 1, tzinfo=UTC),
        ),
    )
    fetched = get_report(conn, report_id="rep-1")
    assert fetched is not None
    assert fetched.markdown_path == "/tmp/r.md"
    rows = conn.execute("SELECT COUNT(*) FROM report_log WHERE report_id = 'rep-1'").fetchone()
    assert rows is not None and rows[0] == 1


def test_query_last_report(conn: duckdb.DuckDBPyConnection) -> None:
    persist_report(
        conn,
        _record(report_id="rep-old", when=datetime(2026, 5, 13, tzinfo=UTC)),
    )
    persist_report(
        conn,
        _record(report_id="rep-new", when=datetime(2026, 5, 16, tzinfo=UTC)),
    )
    latest = query_last_report(conn)
    assert latest is not None
    assert latest.report_id == "rep-new"


def test_list_reports_filter_and_limit(conn: duckdb.DuckDBPyConnection) -> None:
    for i in range(3):
        persist_report(
            conn,
            _record(
                report_id=f"rep-{i}",
                when=datetime(2026, 5, 14 + i, tzinfo=UTC),
            ),
        )
    all_reports = list_reports(conn)
    assert len(all_reports) == 3
    # Newest first.
    assert all_reports[0].report_id == "rep-2"
    limited = list_reports(conn, limit=1)
    assert len(limited) == 1
    cutoff = datetime(2026, 5, 16, tzinfo=UTC)
    recent = list_reports(conn, since=cutoff)
    assert {r.report_id for r in recent} == {"rep-2"}


def test_query_last_report_empty(conn: duckdb.DuckDBPyConnection) -> None:
    assert query_last_report(conn) is None


def test_get_report_missing(conn: duckdb.DuckDBPyConnection) -> None:
    assert get_report(conn, report_id="nope") is None
