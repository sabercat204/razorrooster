"""T-011 verification — operational tables and freshness view.

Verifies:
- All operational tables (sources, backfill_state, ingest_anomalies, cycle_log,
  schema_migrations) are created and accept representative rows.
- License columns on ``sources`` round-trip including the
  ``commercial_use_recorded_grant`` and ``license_terms_hash`` fields added
  by the ACLED amendment.
- The ``freshness`` view classifies sources as fresh / stale / never-fetched.
- Operational indexes exist.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.operational_schemas import (
    all_operational_ddl,
    freshness_view_ddl,
    sources_ddl,
)


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    for stmt in all_operational_ddl():
        c.execute(stmt)
    return c


def _insert_source(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str,
    cadence: str = "daily",
    freshness_threshold_seconds: int = 172800,
    last_successful_fetch: datetime | None = None,
    license: str = "PUBLIC_DOMAIN",
    license_noncommercial_required: bool = False,
    commercial_use_recorded_grant: bool = False,
    license_terms_hash: str | None = None,
    license_acknowledged_at: datetime | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sources (
            source_id, source_type, cadence, freshness_threshold_seconds,
            last_successful_fetch, last_failed_fetch, last_failure_summary,
            license, license_terms_hash, license_acknowledged_at,
            license_noncommercial_required, commercial_use_recorded_grant,
            registered_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            source_id,
            "time_series",
            cadence,
            freshness_threshold_seconds,
            last_successful_fetch,
            None,
            None,
            license,
            license_terms_hash,
            license_acknowledged_at,
            license_noncommercial_required,
            commercial_use_recorded_grant,
            datetime.now(tz=UTC),
            None,
        ],
    )


def test_all_operational_tables_created(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()
    table_names = {r[0] for r in rows}
    assert {
        "sources",
        "backfill_state",
        "ingest_anomalies",
        "cycle_log",
        "schema_migrations",
    } <= table_names


def test_freshness_view_exists(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT view_name FROM duckdb_views() WHERE schema_name = 'main'"
    ).fetchall()
    view_names = {r[0] for r in rows}
    assert "freshness" in view_names


def test_operational_indexes_exist(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE schema_name = 'main'"
    ).fetchall()
    index_names = {r[0] for r in rows}
    assert {
        "idx_ingest_anomalies_source_id",
        "idx_ingest_anomalies_cycle_id",
        "idx_cycle_log_started_at",
    } <= index_names


def test_sources_round_trip_with_license_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """The ACLED amendment added license tracking; confirm round-trip including those columns."""
    ack_at = datetime(2026, 5, 14, 9, 30, tzinfo=UTC)
    _insert_source(
        conn,
        source_id="acled",
        cadence="daily",
        freshness_threshold_seconds=259200,
        license="ACLED_TERMS_VERSIONED",
        license_terms_hash="a" * 64,
        license_acknowledged_at=ack_at,
        license_noncommercial_required=True,
        commercial_use_recorded_grant=False,
    )
    row = conn.execute(
        """
        SELECT
            source_id, license, license_terms_hash,
            license_acknowledged_at, license_noncommercial_required,
            commercial_use_recorded_grant
        FROM sources WHERE source_id = ?
        """,
        ["acled"],
    ).fetchone()
    assert row is not None
    assert row[0] == "acled"
    assert row[1] == "ACLED_TERMS_VERSIONED"
    assert row[2] == "a" * 64
    assert row[3] == ack_at
    assert row[4] is True
    assert row[5] is False


def test_freshness_view_marks_never_fetched_as_stale(conn: duckdb.DuckDBPyConnection) -> None:
    _insert_source(conn, source_id="never_fetched")
    row = conn.execute(
        "SELECT is_stale, seconds_since_fetch FROM freshness WHERE source_id = ?",
        ["never_fetched"],
    ).fetchone()
    assert row is not None
    assert row[0] is True
    assert row[1] is None


def test_freshness_view_marks_recent_as_fresh(conn: duckdb.DuckDBPyConnection) -> None:
    recent = datetime.now(tz=UTC) - timedelta(seconds=60)
    _insert_source(
        conn,
        source_id="recent",
        freshness_threshold_seconds=172800,
        last_successful_fetch=recent,
    )
    row = conn.execute(
        "SELECT is_stale FROM freshness WHERE source_id = ?",
        ["recent"],
    ).fetchone()
    assert row is not None
    assert row[0] is False


def test_freshness_view_marks_old_fetch_as_stale(conn: duckdb.DuckDBPyConnection) -> None:
    old = datetime.now(tz=UTC) - timedelta(days=10)
    _insert_source(
        conn,
        source_id="stale",
        freshness_threshold_seconds=172800,  # 2 days
        last_successful_fetch=old,
    )
    row = conn.execute(
        "SELECT is_stale FROM freshness WHERE source_id = ?",
        ["stale"],
    ).fetchone()
    assert row is not None
    assert row[0] is True


def test_backfill_state_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    started = datetime.now(tz=UTC)
    conn.execute(
        """
        INSERT INTO backfill_state (
            source_id, started_at, last_resume_token, records_persisted,
            bytes_persisted, status, last_updated_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ["fred", started, "page=42", 100_000, 5_000_000, "IN_PROGRESS", started, None],
    )
    row = conn.execute(
        "SELECT source_id, last_resume_token, records_persisted, status FROM backfill_state"
    ).fetchone()
    assert row == ("fred", "page=42", 100_000, "IN_PROGRESS")


def test_ingest_anomalies_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    anomaly_id = str(uuid.uuid4())
    payload = {"detected_value": 42, "expected_range": [0, 10]}
    conn.execute(
        """
        INSERT INTO ingest_anomalies (
            anomaly_id, source_id, cycle_id, anomaly_type, detected_at, details_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            anomaly_id,
            "fred",
            None,
            "value_out_of_range",
            datetime.now(tz=UTC),
            json.dumps(payload),
        ],
    )
    row = conn.execute(
        "SELECT source_id, anomaly_type, "
        "json_extract_string(details_json, '$.detected_value') "
        "FROM ingest_anomalies WHERE anomaly_id = ?",
        [anomaly_id],
    ).fetchone()
    assert row == ("fred", "value_out_of_range", "42")


def test_cycle_log_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    cycle_id = str(uuid.uuid4())
    started = datetime.now(tz=UTC)
    summary = {"connectors": 12, "errors": 0}
    conn.execute(
        """
        INSERT INTO cycle_log (cycle_id, started_at, completed_at, log_path, summary_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            cycle_id,
            started,
            started + timedelta(seconds=120),
            "logs/cycles/cycle-2026-05-14.jsonl",
            json.dumps(summary),
        ],
    )
    row = conn.execute(
        "SELECT json_extract_string(summary_json, '$.connectors') FROM cycle_log "
        "WHERE cycle_id = ?",
        [cycle_id],
    ).fetchone()
    assert row == ("12",)


def test_schema_migrations_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
        [1, datetime.now(tz=UTC), "initial canonical schemas"],
    )
    row = conn.execute(
        "SELECT version, description FROM schema_migrations WHERE version = 1"
    ).fetchone()
    assert row == (1, "initial canonical schemas")


def test_sources_ddl_helper_returns_create_statement() -> None:
    ddl = sources_ddl()
    assert "CREATE TABLE IF NOT EXISTS sources" in ddl
    assert "license_terms_hash" in ddl
    assert "commercial_use_recorded_grant" in ddl


def test_freshness_view_ddl_helper_returns_view_statement() -> None:
    ddl = freshness_view_ddl()
    assert "CREATE OR REPLACE VIEW freshness" in ddl
