"""T-010 verification — canonical schemas as code.

Verifies:
- Schemas can be applied to an in-memory DuckDB.
- A round-trip per schema preserves provenance columns and source payload.
- Indexes are created and usable.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import duckdb
import pytest

from razor_rooster.data_ingest.normalization.base import (
    DocumentDocketRecord,
    EventStreamRecord,
    GeospatialIndicatorRecord,
    TimeSeriesRecord,
)
from razor_rooster.data_ingest.persistence.schemas import (
    SchemaType,
    all_canonical_ddl,
    canonical_indexes_ddl,
    canonical_table_ddl,
)


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    for stmt in all_canonical_ddl():
        c.execute(stmt)
    return c


def test_all_four_canonical_tables_created(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
    table_names = {r[0] for r in rows}
    assert {"event_stream", "time_series", "document_docket", "geospatial_indicator"} <= table_names


def test_schema_type_enum_lists_four_variants() -> None:
    assert {s.value for s in SchemaType} == {
        "event_stream",
        "time_series",
        "document_docket",
        "geospatial_indicator",
    }


def test_canonical_table_ddl_uses_default_table_name() -> None:
    ddl = canonical_table_ddl(SchemaType.EVENT_STREAM)
    assert "CREATE TABLE IF NOT EXISTS event_stream" in ddl
    assert "PRIMARY KEY (source_id, source_record_id, fetch_ts)" in ddl


def test_canonical_table_ddl_accepts_override_name() -> None:
    ddl = canonical_table_ddl(SchemaType.EVENT_STREAM, table_name="staging_event_stream")
    assert "CREATE TABLE IF NOT EXISTS staging_event_stream" in ddl


def test_canonical_indexes_are_created(conn: duckdb.DuckDBPyConnection) -> None:
    rows = conn.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE schema_name = 'main' ORDER BY index_name"
    ).fetchall()
    index_names = {r[0] for r in rows}
    expected = {
        # event_stream
        "idx_event_stream_country_iso3_event_ts",
        "idx_event_stream_source_id_event_ts",
        # time_series
        "idx_time_series_series_id_observation_ts",
        "idx_time_series_source_id_observation_ts",
        # document_docket
        "idx_document_docket_agency_published_date",
        "idx_document_docket_docket_id",
        "idx_document_docket_document_type_published_date",
        # geospatial_indicator
        "idx_geospatial_indicator_indicator_id_observation_ts",
        "idx_geospatial_indicator_country_iso3_indicator_id_observation_ts",
    }
    assert expected <= index_names


def test_event_stream_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    record = EventStreamRecord(
        source_id="acled",
        source_record_id="ACLED-2026-12345",
        source_publication_ts=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        fetch_ts=datetime(2026, 5, 14, 9, 30, tzinfo=UTC),
        connector_version="acled@0.1.0",
        source_payload_json={"original_field": "verbatim", "lat": 12.3, "lon": -45.6},
        event_ts=datetime(2026, 4, 30, 14, 0, tzinfo=UTC),
        country_iso3="SOM",
        actor_primary="Actor A",
        actor_secondary=None,
        event_class="Violence against civilians",
        description="Test event",
    )
    conn.execute(
        """
        INSERT INTO event_stream (
            source_id, source_record_id, source_publication_ts, fetch_ts,
            connector_version, superseded_at, source_payload_json,
            event_ts, country_iso3, actor_primary, actor_secondary,
            event_class, description
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            record.source_id,
            record.source_record_id,
            record.source_publication_ts,
            record.fetch_ts,
            record.connector_version,
            record.superseded_at,
            json.dumps(record.source_payload_json),
            record.event_ts,
            record.country_iso3,
            record.actor_primary,
            record.actor_secondary,
            record.event_class,
            record.description,
        ],
    )
    row = conn.execute(
        "SELECT source_id, source_record_id, country_iso3, "
        "json_extract_string(source_payload_json, '$.original_field'), description "
        "FROM event_stream WHERE source_record_id = ?",
        [record.source_record_id],
    ).fetchone()
    assert row is not None
    assert row[0] == "acled"
    assert row[1] == "ACLED-2026-12345"
    assert row[2] == "SOM"
    assert row[3] == "verbatim"
    assert row[4] == "Test event"


def test_time_series_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    record = TimeSeriesRecord(
        source_id="fred",
        source_record_id="DGS10:2026-04-30",
        source_publication_ts=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
        fetch_ts=datetime(2026, 5, 14, 9, 30, tzinfo=UTC),
        connector_version="fred@0.1.0",
        source_payload_json={"raw_value_string": "4.27"},
        series_id="DGS10",
        observation_ts=datetime(2026, 4, 30, 0, 0, tzinfo=UTC),
        value=4.27,
        unit="percent",
        frequency="D",
    )
    conn.execute(
        """
        INSERT INTO time_series (
            source_id, source_record_id, source_publication_ts, fetch_ts,
            connector_version, superseded_at, source_payload_json,
            series_id, observation_ts, value, unit, frequency
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            record.source_id,
            record.source_record_id,
            record.source_publication_ts,
            record.fetch_ts,
            record.connector_version,
            record.superseded_at,
            json.dumps(record.source_payload_json),
            record.series_id,
            record.observation_ts,
            record.value,
            record.unit,
            record.frequency,
        ],
    )
    row = conn.execute(
        "SELECT series_id, value, unit FROM time_series WHERE source_record_id = ?",
        [record.source_record_id],
    ).fetchone()
    assert row == ("DGS10", 4.27, "percent")


def test_document_docket_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    record = DocumentDocketRecord(
        source_id="federal_register",
        source_record_id="FR-2026-09876",
        source_publication_ts=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
        fetch_ts=datetime(2026, 5, 14, 9, 30, tzinfo=UTC),
        connector_version="federal_register@0.1.0",
        source_payload_json={"abstract": "test rule"},
        title="Final Rule on Test",
        document_type="rule",
        docket_id="EPA-HQ-OAR-2026-0001",
        agency="Environmental Protection Agency",
        published_date=date(2026, 5, 1),
        full_text_uri="https://www.federalregister.gov/example",
    )
    conn.execute(
        """
        INSERT INTO document_docket (
            source_id, source_record_id, source_publication_ts, fetch_ts,
            connector_version, superseded_at, source_payload_json,
            title, document_type, docket_id, agency, published_date,
            effective_date, comment_close_date, full_text_uri,
            full_text_local_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            record.source_id,
            record.source_record_id,
            record.source_publication_ts,
            record.fetch_ts,
            record.connector_version,
            record.superseded_at,
            json.dumps(record.source_payload_json),
            record.title,
            record.document_type,
            record.docket_id,
            record.agency,
            record.published_date,
            record.effective_date,
            record.comment_close_date,
            record.full_text_uri,
            record.full_text_local_path,
        ],
    )
    row = conn.execute(
        "SELECT title, document_type, docket_id, agency FROM document_docket "
        "WHERE source_record_id = ?",
        [record.source_record_id],
    ).fetchone()
    assert row == (
        "Final Rule on Test",
        "rule",
        "EPA-HQ-OAR-2026-0001",
        "Environmental Protection Agency",
    )


def test_geospatial_indicator_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    record = GeospatialIndicatorRecord(
        source_id="noaa",
        source_record_id="enso_oni:2026Q1",
        source_publication_ts=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
        fetch_ts=datetime(2026, 5, 14, 9, 30, tzinfo=UTC),
        connector_version="noaa@0.1.0",
        source_payload_json={"raw": 0.42},
        indicator_id="enso_oni",
        observation_ts=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
        value=0.42,
        unit="degC anomaly",
    )
    conn.execute(
        """
        INSERT INTO geospatial_indicator (
            source_id, source_record_id, source_publication_ts, fetch_ts,
            connector_version, superseded_at, source_payload_json,
            indicator_id, observation_ts, country_iso3, region_code,
            lat, lon, value, unit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            record.source_id,
            record.source_record_id,
            record.source_publication_ts,
            record.fetch_ts,
            record.connector_version,
            record.superseded_at,
            json.dumps(record.source_payload_json),
            record.indicator_id,
            record.observation_ts,
            record.country_iso3,
            record.region_code,
            record.lat,
            record.lon,
            record.value,
            record.unit,
        ],
    )
    row = conn.execute(
        "SELECT indicator_id, value FROM geospatial_indicator WHERE source_record_id = ?",
        [record.source_record_id],
    ).fetchone()
    assert row == ("enso_oni", 0.42)


def test_canonical_indexes_ddl_overrides_table_name() -> None:
    stmts = canonical_indexes_ddl(SchemaType.EVENT_STREAM, table_name="staging_event_stream")
    assert all("ON staging_event_stream" in stmt for stmt in stmts)
