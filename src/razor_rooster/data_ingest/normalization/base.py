"""Normalized record dataclasses (T-010 / design §3.2).

A :class:`NormalizedRecord` is a tagged union over the four canonical schemas.
Each variant carries the provenance prefix plus the schema-specific columns and
the verbatim source payload.

Connectors produce instances of these; the persistence layer writes them via
the staging-merge pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from razor_rooster.data_ingest.persistence.schemas import SchemaType


@dataclass(frozen=True, slots=True)
class _ProvenancePrefix:
    """Fields shared by every normalized record (design §4)."""

    source_id: str
    source_record_id: str
    source_publication_ts: datetime
    fetch_ts: datetime
    connector_version: str
    source_payload_json: dict[str, Any]
    superseded_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EventStreamRecord(_ProvenancePrefix):
    """Point-in-time discrete event (design §4.1)."""

    schema_type: Literal[SchemaType.EVENT_STREAM] = field(
        default=SchemaType.EVENT_STREAM, init=False
    )
    event_ts: datetime = field(default_factory=lambda: datetime.min)
    country_iso3: str | None = None
    actor_primary: str | None = None
    actor_secondary: str | None = None
    event_class: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class TimeSeriesRecord(_ProvenancePrefix):
    """Numeric value at a timestamp (design §4.2)."""

    schema_type: Literal[SchemaType.TIME_SERIES] = field(default=SchemaType.TIME_SERIES, init=False)
    series_id: str = ""
    observation_ts: datetime = field(default_factory=lambda: datetime.min)
    value: float | None = None
    unit: str | None = None
    frequency: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentDocketRecord(_ProvenancePrefix):
    """Structured document with metadata (design §4.3)."""

    schema_type: Literal[SchemaType.DOCUMENT_DOCKET] = field(
        default=SchemaType.DOCUMENT_DOCKET, init=False
    )
    title: str = ""
    document_type: str | None = None
    docket_id: str | None = None
    agency: str | None = None
    published_date: date | None = None
    effective_date: date | None = None
    comment_close_date: date | None = None
    full_text_uri: str | None = None
    full_text_local_path: str | None = None


@dataclass(frozen=True, slots=True)
class GeospatialIndicatorRecord(_ProvenancePrefix):
    """Value indexed by geography and time (design §4.4)."""

    schema_type: Literal[SchemaType.GEOSPATIAL_INDICATOR] = field(
        default=SchemaType.GEOSPATIAL_INDICATOR, init=False
    )
    indicator_id: str = ""
    observation_ts: datetime = field(default_factory=lambda: datetime.min)
    country_iso3: str | None = None
    region_code: str | None = None
    lat: float | None = None
    lon: float | None = None
    value: float | None = None
    unit: str | None = None


NormalizedRecord = (
    EventStreamRecord | TimeSeriesRecord | DocumentDocketRecord | GeospatialIndicatorRecord
)
"""Tagged union over the four canonical-schema record variants."""


@dataclass(frozen=True, slots=True)
class RawRecord:
    """A pre-normalization record carrying the source-native payload (design §3.2).

    Connectors fetch :class:`RawRecord` instances and pass them through their
    ``normalize`` method to obtain a :class:`NormalizedRecord`.
    """

    source_id: str
    source_record_id: str
    source_payload_json: dict[str, Any]
    source_publication_ts: datetime
