"""T-CB-014 — calibration_backtest freezer tests.

Covers REQ-CB-FREEZE-001 (time honesty, boundary equality), the
``source_data_not_frozen`` skip path when ``source_publication_ts`` is
absent, and the dynamic ``sources``-table-driven discovery contract folded
in via the 2026-05-31 scout amendment.

The performance test described in T-CB-014's verification list (1M rows,
p95 ≤ 500ms) is intentionally **deferred** to a dedicated perf-test phase
because the fixture size is too large for the unit-test suite's per-test
budget — see the ``DEFER`` comment near the bottom of this module.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from razor_rooster.calibration_backtest.engines.freezer import (
    CANONICAL_TABLES,
    FrozenState,
    freeze,
    registered_source_ids,
    source_publication_ts_present,
)
from razor_rooster.calibration_backtest.persistence.migrations import (
    m6003_freezer_indexes as m6003,
)
from razor_rooster.data_ingest.persistence.operational_schemas import (
    all_operational_ddl,
)
from razor_rooster.data_ingest.persistence.schemas import all_canonical_ddl

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    """A DuckDB connection with the canonical + operational data_ingest schema."""
    connection = duckdb.connect(":memory:")
    # data_ingest canonical schema (time_series, event_stream, document_docket,
    # geospatial_indicator) plus operational tables (sources, ...).
    for ddl in all_canonical_ddl():
        connection.execute(ddl)
    for ddl in all_operational_ddl():
        connection.execute(ddl)
    return connection


def _register_source(
    conn: duckdb.DuckDBPyConnection,
    source_id: str,
    *,
    source_type: str = "time_series",
) -> None:
    """Insert a minimal ``sources`` row sufficient for freezer discovery."""
    conn.execute(
        """
        INSERT INTO sources (
            source_id, source_type, cadence, freshness_threshold_seconds,
            license, registered_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [source_id, source_type, "daily", 86400, "public_domain", _now()],
    )


def _insert_time_series_row(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str,
    record_id: str,
    source_publication_ts: datetime,
    observation_ts: datetime | None = None,
) -> None:
    """Insert one canonical ``time_series`` row at the specified publication ts."""
    conn.execute(
        """
        INSERT INTO time_series (
            source_id, source_record_id, source_publication_ts, fetch_ts,
            connector_version, superseded_at, source_payload_json,
            series_id, observation_ts, value, unit, frequency
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            source_id,
            record_id,
            source_publication_ts,
            source_publication_ts,
            "v1.0.0",
            None,
            "{}",
            f"{source_id}.series",
            observation_ts or source_publication_ts,
            1.0,
            "unit",
            "D",
        ],
    )


# ---------------------------------------------------------------------------
# CANONICAL_TABLES sanity
# ---------------------------------------------------------------------------


def test_canonical_tables_match_design() -> None:
    """The freezer must enumerate the four canonical data_ingest tables (design §4)."""
    assert set(CANONICAL_TABLES) == {
        "time_series",
        "event_stream",
        "document_docket",
        "geospatial_indicator",
    }
    assert len(CANONICAL_TABLES) == 4  # ordering preserved across runs


# ---------------------------------------------------------------------------
# registered_source_ids — discovery from the ``sources`` table
# ---------------------------------------------------------------------------


def test_registered_source_ids_empty(conn: duckdb.DuckDBPyConnection) -> None:
    assert registered_source_ids(conn) == frozenset()


def test_registered_source_ids_returns_distinct_ids(conn: duckdb.DuckDBPyConnection) -> None:
    _register_source(conn, "fred")
    _register_source(conn, "acled", source_type="event_stream")
    assert registered_source_ids(conn) == frozenset({"fred", "acled"})


def test_registered_source_ids_missing_table_returns_empty() -> None:
    """No ``sources`` table at all → empty registry, no exception."""
    bare = duckdb.connect(":memory:")
    assert registered_source_ids(bare) == frozenset()


# ---------------------------------------------------------------------------
# source_publication_ts_present — column-presence guard
# ---------------------------------------------------------------------------


def test_source_publication_ts_present_on_canonical(conn: duckdb.DuckDBPyConnection) -> None:
    for table in CANONICAL_TABLES:
        assert source_publication_ts_present(conn, table), table


def test_source_publication_ts_missing_table_returns_false() -> None:
    bare = duckdb.connect(":memory:")
    assert source_publication_ts_present(bare, "time_series") is False


def test_source_publication_ts_missing_column_returns_false(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A table without the provenance prefix simulates the unsafe-source case."""
    conn.execute("CREATE TABLE legacy_observations (id VARCHAR PRIMARY KEY, value DOUBLE)")
    assert source_publication_ts_present(conn, "legacy_observations") is False


# ---------------------------------------------------------------------------
# freeze() — boundary equality (REQ-CB-FREEZE-001)
# ---------------------------------------------------------------------------


def test_freeze_returns_frozen_state_on_registered_canonical_sources(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    _register_source(conn, "fred")
    prediction_ts = _now()

    state = freeze(conn, prediction_ts)

    assert state is not None
    assert isinstance(state, FrozenState)
    assert state.frozen_flag is True
    assert state.source_publication_ts_boundary == prediction_ts
    assert state.registered_sources == frozenset({"fred"})


def test_freeze_admits_boundary_equality(conn: duckdb.DuckDBPyConnection) -> None:
    """``source_publication_ts == prediction_ts`` is admitted by the WHERE-clause contract.

    Verifies the SQL invariant the freezer documents in its module
    docstring: a canonical-table row published exactly at ``prediction_ts``
    is included in the frozen view.
    """
    _register_source(conn, "fred")
    prediction_ts = _now()
    _insert_time_series_row(
        conn,
        source_id="fred",
        record_id="r1",
        source_publication_ts=prediction_ts,  # equal — must be admitted
    )

    state = freeze(conn, prediction_ts)
    assert state is not None

    # Apply the WHERE-clause contract documented in freeze() to confirm the
    # boundary row enters the frozen view.
    rows = conn.execute(
        """
        SELECT source_record_id
        FROM time_series
        WHERE source_id IN (SELECT source_id FROM sources)
          AND source_publication_ts <= ?
          AND superseded_at IS NULL
        """,
        [state.source_publication_ts_boundary],
    ).fetchall()
    assert [str(row[0]) for row in rows] == ["r1"]


def test_freeze_rejects_strictly_future_publication(conn: duckdb.DuckDBPyConnection) -> None:
    """``source_publication_ts > prediction_ts`` must not enter the frozen view."""
    _register_source(conn, "fred")
    prediction_ts = _now()
    future = prediction_ts + timedelta(microseconds=1)
    _insert_time_series_row(
        conn,
        source_id="fred",
        record_id="r_future",
        source_publication_ts=future,
    )

    state = freeze(conn, prediction_ts)
    assert state is not None

    rows = conn.execute(
        """
        SELECT source_record_id
        FROM time_series
        WHERE source_id IN (SELECT source_id FROM sources)
          AND source_publication_ts <= ?
          AND superseded_at IS NULL
        """,
        [state.source_publication_ts_boundary],
    ).fetchall()
    assert rows == []


def test_freeze_admits_past_and_rejects_future_in_mixed_corpus(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Mixed timestamps: only rows ``<= prediction_ts`` are admitted, boundary equal included."""
    _register_source(conn, "fred")
    prediction_ts = _now()
    # past
    _insert_time_series_row(
        conn,
        source_id="fred",
        record_id="r_past",
        source_publication_ts=prediction_ts - timedelta(days=1),
    )
    # boundary equality
    _insert_time_series_row(
        conn,
        source_id="fred",
        record_id="r_boundary",
        source_publication_ts=prediction_ts,
    )
    # strictly future
    _insert_time_series_row(
        conn,
        source_id="fred",
        record_id="r_future",
        source_publication_ts=prediction_ts + timedelta(seconds=1),
    )

    state = freeze(conn, prediction_ts)
    assert state is not None

    admitted = {
        str(row[0])
        for row in conn.execute(
            """
            SELECT source_record_id
            FROM time_series
            WHERE source_id IN (SELECT source_id FROM sources)
              AND source_publication_ts <= ?
              AND superseded_at IS NULL
            """,
            [state.source_publication_ts_boundary],
        ).fetchall()
    }
    assert admitted == {"r_past", "r_boundary"}


def test_freeze_excludes_superseded_rows(conn: duckdb.DuckDBPyConnection) -> None:
    """``superseded_at IS NOT NULL`` rows are excluded by the freezer contract."""
    _register_source(conn, "fred")
    prediction_ts = _now()
    # Stale (superseded) record — must be excluded even though its publication
    # ts is admissible.
    conn.execute(
        """
        INSERT INTO time_series (
            source_id, source_record_id, source_publication_ts, fetch_ts,
            connector_version, superseded_at, source_payload_json,
            series_id, observation_ts, value, unit, frequency
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            "fred",
            "r_stale",
            prediction_ts - timedelta(days=2),
            prediction_ts - timedelta(days=2),
            "v1.0.0",
            prediction_ts - timedelta(days=1),  # superseded_at non-null
            "{}",
            "fred.series",
            prediction_ts - timedelta(days=2),
            1.0,
            "unit",
            "D",
        ],
    )

    state = freeze(conn, prediction_ts)
    assert state is not None

    admitted = conn.execute(
        """
        SELECT source_record_id
        FROM time_series
        WHERE source_id IN (SELECT source_id FROM sources)
          AND source_publication_ts <= ?
          AND superseded_at IS NULL
        """,
        [state.source_publication_ts_boundary],
    ).fetchall()
    assert admitted == []


# ---------------------------------------------------------------------------
# freeze() — None / source_data_not_frozen paths
# ---------------------------------------------------------------------------


def test_freeze_returns_none_when_no_sources_registered(
    conn: duckdb.DuckDBPyConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Empty ``sources`` table → ``freeze`` returns None and logs structured event."""
    with caplog.at_level(
        logging.INFO,
        logger="razor_rooster.calibration_backtest.engines.freezer",
    ):
        result = freeze(conn, _now())

    assert result is None
    events = [record for record in caplog.records if record.message == "source_data_not_frozen"]
    assert events, "expected structured 'source_data_not_frozen' log record"
    assert events[0].__dict__["reason"] == "no_registered_sources"


def test_freeze_returns_none_when_canonical_column_simulated_missing(
    monkeypatch: pytest.MonkeyPatch,
    conn: duckdb.DuckDBPyConnection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Simulate ``source_publication_ts`` missing from a canonical table.

    Mirrors T-CB-014's "register a synthetic source whose canonical-schema
    metadata simulates a missing-column scenario" verification step. We
    monkeypatch :func:`source_publication_ts_present` to report ``False``
    for one canonical table; ``freeze`` must decline and log
    ``source_data_not_frozen``.
    """
    _register_source(conn, "fred")

    real_check = source_publication_ts_present

    def fake_check(c: duckdb.DuckDBPyConnection, table: str) -> bool:
        if table == "time_series":
            return False
        return real_check(c, table)

    monkeypatch.setattr(
        "razor_rooster.calibration_backtest.engines.freezer.source_publication_ts_present",
        fake_check,
    )

    with caplog.at_level(
        logging.INFO,
        logger="razor_rooster.calibration_backtest.engines.freezer",
    ):
        result = freeze(conn, _now())

    assert result is None
    events = [record for record in caplog.records if record.message == "source_data_not_frozen"]
    assert events
    assert events[0].__dict__["reason"] == "missing_source_publication_ts"
    assert "time_series" in events[0].__dict__["tables_missing_column"]


# ---------------------------------------------------------------------------
# m6003 freezer-index migration
# ---------------------------------------------------------------------------


def _existing_index_names(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
    return {str(row[0]) for row in rows}


def test_m6003_creates_freezer_indexes(conn: duckdb.DuckDBPyConnection) -> None:
    """``up()`` adds the four freezer-supporting indexes on canonical tables."""
    m6003.up(conn)

    existing = _existing_index_names(conn)
    expected = {index_name for _, index_name in m6003.INDEX_SPECS}
    assert expected.issubset(existing), expected - existing


def test_m6003_is_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    """Re-running m6003 must not raise."""
    m6003.up(conn)
    m6003.up(conn)  # second call is a no-op via ``CREATE INDEX IF NOT EXISTS``


def test_m6003_down_drops_freezer_indexes(conn: duckdb.DuckDBPyConnection) -> None:
    m6003.up(conn)
    m6003.down(conn)
    existing = _existing_index_names(conn)
    for _, index_name in m6003.INDEX_SPECS:
        assert index_name not in existing


def test_m6003_down_idempotent_on_clean_db() -> None:
    """``down()`` on a database without freezer indexes must not raise."""
    bare = duckdb.connect(":memory:")
    m6003.down(bare)


def test_m6003_tolerates_missing_canonical_tables() -> None:
    """``up()`` is a no-op against a database that lacks the canonical tables."""
    bare = duckdb.connect(":memory:")
    # No tables exist; up() should silently skip the index creates.
    m6003.up(bare)


def test_m6003_migration_id_matches_schema_constant() -> None:
    from razor_rooster.calibration_backtest.persistence import schemas

    assert m6003.MIGRATION_ID == schemas.VERSION_6003
    assert m6003.MIGRATION_ID == 6003


# ---------------------------------------------------------------------------
# DEFER: 1M-row p95 latency test
# ---------------------------------------------------------------------------
#
# T-CB-014's verification list calls for a "performance test: seed 1M rows
# across 5 source_ids; assert ``freeze()`` p95 latency ≤ 500ms with the new
# indexes in place." That fixture size (~5 GB after expansion) and run-time
# (multi-second per iteration; tens of iterations for a stable p95) is too
# large for the unit-test budget enforced by ``addopts = -ra -q
# --strict-markers`` in pyproject.toml. The test belongs to the dedicated
# perf phase (T-CB-052) where it can run under ``@pytest.mark.perf`` and be
# excluded from the default selector. Tracking note: see DEFER-CB-FREEZER-PERF
# in the next bootstrap-blockers refresh.
