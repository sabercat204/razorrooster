"""Canonical schema definitions for ``data_ingest`` (T-010).

Four canonical schemas are defined per design §4:

- ``event_stream`` — point-in-time discrete events (ACLED incidents, GDELT
  events, WHO DON entries, Federal Register filings).
- ``time_series`` — numeric values at timestamps (FRED indices, NOAA readings,
  EIA stock levels).
- ``document_docket`` — structured documents (NRC ADAMS, regulations.gov
  dockets).
- ``geospatial_indicator`` — values indexed by geography and time (drought
  indices, ENSO state, wildfire risk by region).

Each schema shares the provenance prefix from design §4. Source-native payloads
are preserved verbatim in ``source_payload_json``.

The DDL strings are parameterized only by table name; downstream code uses
:func:`canonical_table_ddl` to produce the actual CREATE TABLE statements.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class SchemaType(StrEnum):
    """Enumeration of canonical schema types.

    Adding a new variant is a deliberate, versioned operation (REQ-EXT-002).
    """

    EVENT_STREAM = "event_stream"
    TIME_SERIES = "time_series"
    DOCUMENT_DOCKET = "document_docket"
    GEOSPATIAL_INDICATOR = "geospatial_indicator"


# Provenance prefix shared by every canonical table (design §4).
#
# Notes:
# - ``source_payload_json`` is JSON in DuckDB (uses native JSON type when
#   available; otherwise VARCHAR is acceptable).
# - ``superseded_at`` is non-NULL when a newer version of the same record
#   exists (REQ-PERSIST-004).
# - The triple ``(source_id, source_record_id, fetch_ts)`` is the primary key;
#   ``(source_id, source_record_id) WHERE superseded_at IS NULL`` is the
#   logical "current" row.
_PROVENANCE_PREFIX: Final[str] = """\
    source_id              VARCHAR     NOT NULL,
    source_record_id       VARCHAR     NOT NULL,
    source_publication_ts  TIMESTAMPTZ NOT NULL,
    fetch_ts               TIMESTAMPTZ NOT NULL,
    connector_version      VARCHAR     NOT NULL,
    superseded_at          TIMESTAMPTZ NULL,
    source_payload_json    JSON        NOT NULL"""


_EVENT_STREAM_BODY: Final[str] = """\
    event_ts               TIMESTAMPTZ NOT NULL,
    country_iso3           VARCHAR     NULL,
    actor_primary          VARCHAR     NULL,
    actor_secondary        VARCHAR     NULL,
    event_class            VARCHAR     NULL,
    description            TEXT        NULL"""


_TIME_SERIES_BODY: Final[str] = """\
    series_id              VARCHAR     NOT NULL,
    observation_ts         TIMESTAMPTZ NOT NULL,
    value                  DOUBLE      NULL,
    unit                   VARCHAR     NULL,
    frequency              VARCHAR     NULL"""


_DOCUMENT_DOCKET_BODY: Final[str] = """\
    title                  TEXT        NOT NULL,
    document_type          VARCHAR     NULL,
    docket_id              VARCHAR     NULL,
    agency                 VARCHAR     NULL,
    published_date         DATE        NULL,
    effective_date         DATE        NULL,
    comment_close_date     DATE        NULL,
    full_text_uri          VARCHAR     NULL,
    full_text_local_path   VARCHAR     NULL"""


_GEOSPATIAL_INDICATOR_BODY: Final[str] = """\
    indicator_id           VARCHAR     NOT NULL,
    observation_ts         TIMESTAMPTZ NOT NULL,
    country_iso3           VARCHAR     NULL,
    region_code            VARCHAR     NULL,
    lat                    DOUBLE      NULL,
    lon                    DOUBLE      NULL,
    value                  DOUBLE      NULL,
    unit                   VARCHAR     NULL"""


_SCHEMA_BODIES: Final[dict[SchemaType, str]] = {
    SchemaType.EVENT_STREAM: _EVENT_STREAM_BODY,
    SchemaType.TIME_SERIES: _TIME_SERIES_BODY,
    SchemaType.DOCUMENT_DOCKET: _DOCUMENT_DOCKET_BODY,
    SchemaType.GEOSPATIAL_INDICATOR: _GEOSPATIAL_INDICATOR_BODY,
}


# Per-schema secondary indexes. The primary key is the same on every table:
# (source_id, source_record_id, fetch_ts).
_SCHEMA_INDEXES: Final[dict[SchemaType, tuple[tuple[str, ...], ...]]] = {
    SchemaType.EVENT_STREAM: (
        ("country_iso3", "event_ts"),
        ("source_id", "event_ts"),
    ),
    SchemaType.TIME_SERIES: (
        ("series_id", "observation_ts"),
        ("source_id", "observation_ts"),
    ),
    SchemaType.DOCUMENT_DOCKET: (
        ("agency", "published_date"),
        ("docket_id",),
        ("document_type", "published_date"),
    ),
    SchemaType.GEOSPATIAL_INDICATOR: (
        ("indicator_id", "observation_ts"),
        ("country_iso3", "indicator_id", "observation_ts"),
    ),
}


def canonical_table_ddl(schema: SchemaType, table_name: str | None = None) -> str:
    """Return the ``CREATE TABLE IF NOT EXISTS`` DDL for a canonical schema.

    The table name defaults to the schema's value (e.g. ``event_stream``).
    """
    name = table_name or schema.value
    body = _SCHEMA_BODIES[schema]
    return (
        f"CREATE TABLE IF NOT EXISTS {name} (\n"
        f"{_PROVENANCE_PREFIX},\n"
        f"{body},\n"
        f"    PRIMARY KEY (source_id, source_record_id, fetch_ts)\n"
        ");"
    )


def canonical_indexes_ddl(schema: SchemaType, table_name: str | None = None) -> tuple[str, ...]:
    """Return the ``CREATE INDEX IF NOT EXISTS`` statements for a canonical schema."""
    name = table_name or schema.value
    statements = []
    for cols in _SCHEMA_INDEXES[schema]:
        index_name = f"idx_{name}_{'_'.join(cols)}"
        statements.append(f"CREATE INDEX IF NOT EXISTS {index_name} ON {name} ({', '.join(cols)});")
    return tuple(statements)


def all_canonical_ddl() -> tuple[str, ...]:
    """Return the full set of canonical-schema DDL statements (tables + indexes)."""
    statements: list[str] = []
    for schema in SchemaType:
        statements.append(canonical_table_ddl(schema))
        statements.extend(canonical_indexes_ddl(schema))
    return tuple(statements)
