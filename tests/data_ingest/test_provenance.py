"""T-015 verification — provenance helpers.

Verifies:
- ``register_source`` is idempotent on duplicate calls.
- ``update_last_successful_fetch`` clears any prior failure summary.
- ``update_last_failed_fetch`` records the failure without erasing the
  prior success timestamp.
- ``record_anomaly`` writes a row with a generated id and JSON details.
- ``record_license_acknowledgement`` writes hash + timestamp; rejects
  unknown sources.
- ``get_license_posture`` returns the recorded values.
- ``query_freshness`` returns typed dataclasses; filters by source_id;
  returns rows in source_id order when unfiltered.
- ``freshness_for`` returns ``None`` for unknown sources.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.provenance import (
    FreshnessRow,
    SourceLicensePosture,
    freshness_for,
    get_license_posture,
    query_freshness,
    record_anomaly,
    record_license_acknowledgement,
    register_source,
    update_last_failed_fetch,
    update_last_successful_fetch,
)


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    run_pending_migrations(c)
    return c


def test_register_source_writes_a_row(conn: duckdb.DuckDBPyConnection) -> None:
    register_source(
        conn,
        source_id="fred",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
    )
    row = conn.execute(
        "SELECT source_id, source_type, cadence, license, "
        "license_noncommercial_required, commercial_use_recorded_grant "
        "FROM sources WHERE source_id = 'fred'"
    ).fetchone()
    assert row is not None
    assert row[0] == "fred"
    assert row[1] == "time_series"
    assert row[2] == "daily"
    assert row[3] == "PUBLIC_DOMAIN"
    assert row[4] is False
    assert row[5] is False


def test_register_source_is_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    register_source(
        conn,
        source_id="fred",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
    )
    # Second registration should not duplicate or raise.
    register_source(
        conn,
        source_id="fred",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
        notes="this should not overwrite",
    )
    rows = conn.execute("SELECT COUNT(*) FROM sources WHERE source_id = 'fred'").fetchone()
    assert rows is not None and rows[0] == 1


def test_register_source_records_noncommercial_flag(conn: duckdb.DuckDBPyConnection) -> None:
    register_source(
        conn,
        source_id="acled",
        source_type="event_stream",
        cadence="daily",
        freshness_threshold_seconds=259200,
        license="ACLED_TERMS_VERSIONED",
        license_noncommercial_required=True,
    )
    posture = get_license_posture(conn, "acled")
    assert posture is not None
    assert posture.license_noncommercial_required is True
    assert posture.commercial_use_recorded_grant is False
    assert posture.license_acknowledged_at is None
    assert posture.license_terms_hash is None


def test_update_last_successful_fetch_records_timestamp(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    register_source(
        conn,
        source_id="fred",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
    )
    when = datetime(2026, 5, 14, 9, 30, tzinfo=UTC)
    update_last_successful_fetch(conn, "fred", when=when)
    row = conn.execute(
        "SELECT last_successful_fetch FROM sources WHERE source_id = 'fred'"
    ).fetchone()
    assert row is not None
    assert row[0] == when


def test_update_last_successful_fetch_clears_prior_failure(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    register_source(
        conn,
        source_id="fred",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
    )
    update_last_failed_fetch(conn, "fred", error_summary="429 rate limited")
    update_last_successful_fetch(conn, "fred")
    row = conn.execute(
        "SELECT last_failed_fetch, last_failure_summary FROM sources WHERE source_id = 'fred'"
    ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None


def test_update_last_failed_fetch_preserves_prior_success(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    register_source(
        conn,
        source_id="fred",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
    )
    success_at = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)
    update_last_successful_fetch(conn, "fred", when=success_at)
    update_last_failed_fetch(conn, "fred", error_summary="connection refused")
    row = conn.execute(
        "SELECT last_successful_fetch, last_failed_fetch, last_failure_summary "
        "FROM sources WHERE source_id = 'fred'"
    ).fetchone()
    assert row is not None
    assert row[0] == success_at
    assert row[1] is not None
    assert row[2] == "connection refused"


def test_record_anomaly_writes_a_row(conn: duckdb.DuckDBPyConnection) -> None:
    register_source(
        conn,
        source_id="fred",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
    )
    anomaly_id = record_anomaly(
        conn,
        source_id="fred",
        anomaly_type="value_out_of_range",
        details={"observed": 999, "expected_max": 100},
    )
    assert isinstance(anomaly_id, str)
    assert len(anomaly_id) > 0

    row = conn.execute(
        "SELECT anomaly_type, json_extract_string(details_json, '$.observed') "
        "FROM ingest_anomalies WHERE anomaly_id = ?",
        [anomaly_id],
    ).fetchone()
    assert row == ("value_out_of_range", "999")


def test_record_anomaly_with_cycle_id(conn: duckdb.DuckDBPyConnection) -> None:
    register_source(
        conn,
        source_id="fred",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
    )
    record_anomaly(
        conn,
        source_id="fred",
        anomaly_type="schema_mismatch",
        details={"got_field": "x", "expected_field": "y"},
        cycle_id="cycle-abc",
    )
    row = conn.execute("SELECT cycle_id FROM ingest_anomalies WHERE source_id = 'fred'").fetchone()
    assert row == ("cycle-abc",)


def test_record_license_acknowledgement_writes_hash_and_ts(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    register_source(
        conn,
        source_id="acled",
        source_type="event_stream",
        cadence="daily",
        freshness_threshold_seconds=259200,
        license="ACLED_TERMS_VERSIONED",
        license_noncommercial_required=True,
    )
    when = datetime(2026, 5, 14, 9, 30, tzinfo=UTC)
    record_license_acknowledgement(
        conn,
        source_id="acled",
        terms_hash="a" * 64,
        when=when,
    )
    posture = get_license_posture(conn, "acled")
    assert posture is not None
    assert posture.license_terms_hash == "a" * 64
    assert posture.license_acknowledged_at == when
    assert posture.license_noncommercial_required is True
    assert posture.commercial_use_recorded_grant is False


def test_record_license_acknowledgement_with_commercial_grant(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    register_source(
        conn,
        source_id="public_src",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
    )
    record_license_acknowledgement(
        conn,
        source_id="public_src",
        terms_hash="b" * 64,
        commercial_use_recorded_grant=True,
    )
    posture = get_license_posture(conn, "public_src")
    assert posture is not None
    assert posture.commercial_use_recorded_grant is True


def test_record_license_acknowledgement_rejects_unknown_source(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    with pytest.raises(ValueError, match="not registered"):
        record_license_acknowledgement(
            conn,
            source_id="nonexistent",
            terms_hash="c" * 64,
        )


def test_get_license_posture_returns_none_for_unknown_source(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    assert get_license_posture(conn, "nonexistent") is None


def test_query_freshness_returns_typed_rows(conn: duckdb.DuckDBPyConnection) -> None:
    register_source(
        conn,
        source_id="fred",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
    )
    register_source(
        conn,
        source_id="acled",
        source_type="event_stream",
        cadence="daily",
        freshness_threshold_seconds=259200,
        license="ACLED_TERMS_VERSIONED",
        license_noncommercial_required=True,
    )
    rows = query_freshness(conn)
    assert all(isinstance(r, FreshnessRow) for r in rows)
    # Default order is by source_id.
    assert [r.source_id for r in rows] == ["acled", "fred"]
    # Both never-fetched → both stale.
    assert all(r.is_stale for r in rows)


def test_query_freshness_filters_by_source_id(conn: duckdb.DuckDBPyConnection) -> None:
    register_source(
        conn,
        source_id="fred",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
    )
    register_source(
        conn,
        source_id="acled",
        source_type="event_stream",
        cadence="daily",
        freshness_threshold_seconds=259200,
        license="ACLED_TERMS_VERSIONED",
    )
    rows = query_freshness(conn, source_id="fred")
    assert len(rows) == 1
    assert rows[0].source_id == "fred"


def test_query_freshness_classifies_recent_fetch_as_fresh(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    register_source(
        conn,
        source_id="fred",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
    )
    update_last_successful_fetch(
        conn,
        "fred",
        when=datetime.now(tz=UTC) - timedelta(seconds=60),
    )
    rows = query_freshness(conn, source_id="fred")
    assert len(rows) == 1
    assert rows[0].is_stale is False
    assert rows[0].seconds_since_fetch is not None
    assert rows[0].seconds_since_fetch < 300  # well under threshold


def test_freshness_for_returns_none_for_unknown_source(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    assert freshness_for(conn, "nonexistent") is None


def test_freshness_for_returns_one_row(conn: duckdb.DuckDBPyConnection) -> None:
    register_source(
        conn,
        source_id="fred",
        source_type="time_series",
        cadence="daily",
        freshness_threshold_seconds=172800,
        license="PUBLIC_DOMAIN",
    )
    row = freshness_for(conn, "fred")
    assert isinstance(row, FreshnessRow)
    assert row.source_id == "fred"
    assert row.is_stale is True


def test_source_license_posture_dataclass_round_trip() -> None:
    """The dataclass is frozen and slot-based; basic shape sanity check."""
    posture = SourceLicensePosture(
        license="PUBLIC_DOMAIN",
        license_terms_hash=None,
        license_acknowledged_at=None,
        license_noncommercial_required=False,
        commercial_use_recorded_grant=False,
    )
    assert posture.license == "PUBLIC_DOMAIN"
    with pytest.raises(AttributeError):
        posture.license = "OTHER"  # type: ignore[misc]
