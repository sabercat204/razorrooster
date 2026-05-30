"""T-DI-101 — ``sources.acknowledged_posture`` migration acceptance tests.

Verifies:
- m0002 adds the column to fresh installs.
- m0002 backfills existing Polymarket acknowledgements as ``'read_only'``.
- ``register_source`` continues to work (column remains nullable for new rows).
- ``record_license_acknowledgement`` accepts the optional posture parameter.
- ``get_license_posture`` returns the new field.
- The migration is idempotent on re-run.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    applied_versions,
    run_pending_migrations,
)
from razor_rooster.data_ingest.persistence.provenance import (
    SourceLicensePosture,
    get_license_posture,
    record_license_acknowledgement,
    register_source,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "data_ingest_m0002.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    yield s
    s.close()


def test_m0002_records_version(store: DuckDBStore) -> None:
    with store.connection() as conn:
        versions = applied_versions(conn)
    assert 1 in versions
    assert 2 in versions


def test_acknowledged_posture_column_exists(store: DuckDBStore) -> None:
    with store.connection() as conn:
        rows = conn.execute("PRAGMA table_info('sources')").fetchall()
    column_names = {r[1] for r in rows}
    assert "acknowledged_posture" in column_names


def test_register_source_works_without_posture(store: DuckDBStore) -> None:
    """register_source should not require posture (columns is nullable)."""
    with store.connection() as conn:
        register_source(
            conn,
            source_id="test_src",
            source_type="test",
            cadence="daily",
            freshness_threshold_seconds=86400,
            license="TEST_LICENSE",
        )
        posture = get_license_posture(conn, "test_src")
    assert posture is not None
    assert posture.acknowledged_posture is None


def test_record_license_acknowledgement_with_posture(store: DuckDBStore) -> None:
    """The new acknowledged_posture parameter round-trips."""
    with store.connection() as conn:
        register_source(
            conn,
            source_id="kalshi_test",
            source_type="kalshi_market",
            cadence="every_30min",
            freshness_threshold_seconds=10800,
            license="KALSHI_TERMS_VERSIONED",
        )
        record_license_acknowledgement(
            conn,
            source_id="kalshi_test",
            terms_hash="abc123",
            acknowledged_posture="read_only",
        )
        posture = get_license_posture(conn, "kalshi_test")
    assert posture is not None
    assert posture.acknowledged_posture == "read_only"
    assert posture.license_terms_hash == "abc123"


def test_record_license_acknowledgement_backward_compat_no_posture(
    store: DuckDBStore,
) -> None:
    """Acknowledgement without posture stays None (backward compat for ACLED)."""
    with store.connection() as conn:
        register_source(
            conn,
            source_id="acled_test",
            source_type="event_stream",
            cadence="weekly",
            freshness_threshold_seconds=604800,
            license="ACLED_CC_BY_NC_4_0",
        )
        record_license_acknowledgement(
            conn,
            source_id="acled_test",
            terms_hash="xyz789",
        )
        posture = get_license_posture(conn, "acled_test")
    assert posture is not None
    assert posture.acknowledged_posture is None
    assert posture.license_terms_hash == "xyz789"


def test_record_license_acknowledgement_with_trading_posture(
    store: DuckDBStore,
) -> None:
    """The 'trading' posture is reserved for v2+ but the column accepts it."""
    with store.connection() as conn:
        register_source(
            conn,
            source_id="future_src",
            source_type="kalshi_market",
            cadence="daily",
            freshness_threshold_seconds=86400,
            license="KALSHI_TERMS_VERSIONED",
        )
        record_license_acknowledgement(
            conn,
            source_id="future_src",
            terms_hash="trading_hash",
            acknowledged_posture="trading",
        )
        posture = get_license_posture(conn, "future_src")
    assert posture is not None
    assert posture.acknowledged_posture == "trading"


def test_get_license_posture_unknown_value_falls_back_to_none(
    store: DuckDBStore,
) -> None:
    """Defensive: a corrupted column value coerces to None rather than crash."""
    with store.connection() as conn:
        register_source(
            conn,
            source_id="corrupt_src",
            source_type="test",
            cadence="daily",
            freshness_threshold_seconds=86400,
            license="TEST_LICENSE",
        )
        # Direct UPDATE injecting an invalid posture value.
        conn.execute(
            "UPDATE sources SET acknowledged_posture = ? WHERE source_id = ?",
            ["bogus_posture", "corrupt_src"],
        )
        posture = get_license_posture(conn, "corrupt_src")
    assert posture is not None
    assert posture.acknowledged_posture is None


def test_m0002_is_idempotent(store: DuckDBStore) -> None:
    """Re-running migrations is a no-op on already-applied versions."""
    with store.connection() as conn:
        before = applied_versions(conn)
        run_pending_migrations(conn)
        after = applied_versions(conn)
    assert before == after


def test_polymarket_backfill_to_read_only(tmp_path: Path) -> None:
    """T-DI-101 design contract: existing Polymarket acks get 'read_only'.

    Simulates a database that ran m0001 only (the ``acknowledged_posture``
    column not yet added), with a Polymarket acknowledgement already
    recorded. After m0002 runs, the column should exist and the row
    should be backfilled to ``'read_only'``.
    """
    import duckdb

    from razor_rooster.data_ingest.persistence.migrations.m0001_initial import (
        up as m0001_up,
    )
    from razor_rooster.data_ingest.persistence.migrations.m0002_add_acknowledged_posture import (
        up as m0002_up,
    )

    db_path = tmp_path / "backfill.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        m0001_up(conn)
        # Insert a synthetic Polymarket row with an acknowledgement, before
        # the column exists. This mirrors a real upgrade path.
        conn.execute(
            "INSERT INTO sources ("
            "source_id, source_type, cadence, freshness_threshold_seconds, "
            "license, license_terms_hash, license_acknowledged_at, "
            "license_noncommercial_required, commercial_use_recorded_grant, "
            "registered_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "polymarket",
                "polymarket_market",
                "hourly",
                21600,
                "POLYMARKET_TERMS_VERSIONED",
                "preexisting_hash",
                datetime(2026, 5, 1, tzinfo=UTC),
                False,
                False,
                datetime(2026, 5, 1, tzinfo=UTC),
            ],
        )
        # Run m0002.
        m0002_up(conn)
        # The Polymarket row should now carry 'read_only'.
        row = conn.execute(
            "SELECT acknowledged_posture FROM sources WHERE source_id = ?",
            ["polymarket"],
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "read_only"


def test_source_license_posture_dataclass_field_default() -> None:
    """The new field has a None default for backward compatibility."""
    posture = SourceLicensePosture(
        license="TEST",
        license_terms_hash=None,
        license_acknowledged_at=None,
        license_noncommercial_required=False,
        commercial_use_recorded_grant=False,
    )
    assert posture.acknowledged_posture is None
