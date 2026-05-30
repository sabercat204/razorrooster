"""T-052 verification — GDELT 2.0 events connector."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from razor_rooster.data_ingest.connectors.base import License, ResumeToken
from razor_rooster.data_ingest.connectors.gdelt_events import (
    GdeltEventsConnector,
    gdelt_filename,
    gdelt_url,
    iter_15_minute_windows,
    round_to_15_minute_window,
)
from razor_rooster.data_ingest.normalization.base import EventStreamRecord, RawRecord
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import is_registered


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "gdelt.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


def _build_gdelt_zip(rows: list[list[str]]) -> bytes:
    """Build an in-memory GDELT-shaped zip with one TSV file."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        tsv_lines = []
        for row in rows:
            # Pad to 61 columns.
            padded = list(row) + [""] * (61 - len(row))
            tsv_lines.append("\t".join(padded))
        zf.writestr("20260514093000.export.CSV", "\n".join(tsv_lines))
    return buf.getvalue()


def _gdelt_row(
    *,
    event_id: str,
    sql_date: str,
    actor1: str = "USA",
    actor2: str = "RUS",
    event_code: str = "01",
    country: str = "USA",
    source_url: str = "https://example.com/news",
) -> list[str]:
    """Build a GDELT row matching the column order in _GDELT_COLUMNS."""
    row = [""] * 61
    row[0] = event_id
    row[1] = sql_date
    row[6] = actor1
    row[16] = actor2
    row[26] = event_code  # EventCode column index
    row[53] = country  # ActionGeo_CountryCode column index
    row[60] = source_url  # SOURCEURL column index
    return row


class _CannedTransport(httpx.MockTransport):
    def __init__(self, responses: list[tuple[int, bytes | str]]) -> None:
        self._responses = list(responses)
        self.requests_received: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests_received.append(request)
            if not self._responses:
                return httpx.Response(404)
            status, body = self._responses.pop(0)
            if isinstance(body, bytes):
                return httpx.Response(status, content=body)
            return httpx.Response(status, text=body)

        super().__init__(handler)


def _client(responses: list[tuple[int, bytes | str]]) -> tuple[httpx.Client, _CannedTransport]:
    transport = _CannedTransport(responses)
    return httpx.Client(transport=transport, timeout=5.0), transport


# --- helpers --------------------------------------------------------------


def test_round_to_15_minute_window_rounds_down() -> None:
    ts = datetime(2026, 5, 14, 9, 37, 42, tzinfo=UTC)
    rounded = round_to_15_minute_window(ts)
    assert rounded == datetime(2026, 5, 14, 9, 30, tzinfo=UTC)


def test_round_to_15_minute_window_naive_datetime_assumes_utc() -> None:
    ts = datetime(2026, 5, 14, 9, 37)
    rounded = round_to_15_minute_window(ts)
    assert rounded.tzinfo == UTC


def test_iter_15_minute_windows_yields_quarters() -> None:
    start = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    end = datetime(2026, 5, 14, 10, 0, tzinfo=UTC)
    windows = list(iter_15_minute_windows(start, end))
    assert windows == [
        datetime(2026, 5, 14, 9, 0, tzinfo=UTC),
        datetime(2026, 5, 14, 9, 15, tzinfo=UTC),
        datetime(2026, 5, 14, 9, 30, tzinfo=UTC),
        datetime(2026, 5, 14, 9, 45, tzinfo=UTC),
    ]


def test_gdelt_filename_format() -> None:
    ts = datetime(2026, 5, 14, 9, 30, 0, tzinfo=UTC)
    assert gdelt_filename(ts) == "20260514093000.export.CSV.zip"


def test_gdelt_url_format() -> None:
    ts = datetime(2026, 5, 14, 9, 30, 0, tzinfo=UTC)
    url = gdelt_url(ts)
    assert url.startswith("http://data.gdeltproject.org/gdeltv2/")
    assert "20260514093000" in url


# --- registration ---------------------------------------------------------


def test_gdelt_self_registers() -> None:
    assert is_registered("gdelt_events")


def test_class_attributes() -> None:
    assert GdeltEventsConnector.source_id == "gdelt_events"
    assert GdeltEventsConnector.canonical_schema == SchemaType.EVENT_STREAM
    assert GdeltEventsConnector.license == License.PUBLIC_DOMAIN
    assert GdeltEventsConnector.backfill_supported is True


# --- fetch + parse --------------------------------------------------------


def test_fetch_incremental_parses_events(store: DuckDBStore) -> None:
    rows = [
        _gdelt_row(event_id="1234567890", sql_date="20260514"),
        _gdelt_row(event_id="1234567891", sql_date="20260514", actor1="GBR", actor2="FRA"),
    ]
    zip_bytes = _build_gdelt_zip(rows)
    client, _ = _client([(200, zip_bytes)])
    connector = GdeltEventsConnector(store, client=client)
    # Fetch a narrow window so only one URL is requested.
    since = datetime(2026, 5, 14, 9, 30, tzinfo=UTC)
    until = datetime(2026, 5, 14, 9, 45, tzinfo=UTC)
    records = list(_iter_with_until(connector, since, until))
    assert len(records) == 2
    assert records[0].source_record_id == "1234567890"


def _iter_with_until(
    connector: GdeltEventsConnector, since: datetime, until: datetime
) -> list[RawRecord]:
    """Helper: fetch_incremental but capped at 'until' for test determinism."""
    records: list[RawRecord] = []
    for window_ts in iter_15_minute_windows(since, until):
        for raw in connector._fetch_window(window_ts):
            records.append(raw)
    return records


def test_normalize_extracts_event_fields(store: DuckDBStore) -> None:
    connector = GdeltEventsConnector(store, client=httpx.Client())
    payload = {
        "GLOBALEVENTID": "1234567890",
        "SQLDATE": "20260514",
        "Actor1Name": "USA",
        "Actor2Name": "RUS",
        "EventCode": "01",
        "ActionGeo_CountryCode": "USA",
        "SOURCEURL": "https://example.com/news",
    }
    raw = RawRecord(
        source_id="gdelt_events",
        source_record_id="1234567890",
        source_payload_json=payload,
        source_publication_ts=datetime(2026, 5, 14, 9, 30, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, EventStreamRecord)
    assert normalized.event_ts == datetime(2026, 5, 14, tzinfo=UTC)
    assert normalized.country_iso3 == "USA"
    assert normalized.actor_primary == "USA"
    assert normalized.actor_secondary == "RUS"
    assert normalized.event_class == "01"
    assert normalized.description == "https://example.com/news"


def test_normalize_handles_empty_country(store: DuckDBStore) -> None:
    connector = GdeltEventsConnector(store, client=httpx.Client())
    raw = RawRecord(
        source_id="gdelt_events",
        source_record_id="1",
        source_payload_json={
            "GLOBALEVENTID": "1",
            "SQLDATE": "20260514",
            "Actor1Name": "",
            "Actor2Name": "",
            "EventCode": "",
            "ActionGeo_CountryCode": "",
            "SOURCEURL": "",
        },
        source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, EventStreamRecord)
    assert normalized.country_iso3 is None
    assert normalized.actor_primary is None


def test_normalize_handles_invalid_sqldate(store: DuckDBStore) -> None:
    connector = GdeltEventsConnector(store, client=httpx.Client())
    raw = RawRecord(
        source_id="gdelt_events",
        source_record_id="1",
        source_payload_json={
            "GLOBALEVENTID": "1",
            "SQLDATE": "not-a-date",
        },
        source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, EventStreamRecord)
    # Falls back to "now" — just confirm it parsed without raising.
    assert normalized.event_ts.tzinfo == UTC


def test_404_returns_no_records(store: DuckDBStore) -> None:
    """Some GDELT windows simply don't exist; 404 → empty, not error."""
    client, _ = _client([(404, "not found")])
    connector = GdeltEventsConnector(store, client=client)
    records = list(connector._fetch_window(datetime(2026, 5, 14, 9, 30, tzinfo=UTC)))
    assert records == []


def test_bad_zip_returns_no_records(store: DuckDBStore) -> None:
    client, _ = _client([(200, b"not a zip file")])
    connector = GdeltEventsConnector(store, client=client)
    records = list(connector._fetch_window(datetime(2026, 5, 14, 9, 30, tzinfo=UTC)))
    assert records == []


def test_resume_token_parses_and_advances(store: DuckDBStore) -> None:
    """A resume token like '20260514093000' should advance to 09:45."""
    rows = [_gdelt_row(event_id="X", sql_date="20260514")]
    zip_bytes = _build_gdelt_zip(rows)
    client, transport = _client([(200, zip_bytes), (404, "")])  # 09:45 then later windows 404
    connector = GdeltEventsConnector(store, client=client)
    pairs = list(
        connector.fetch_backfill(
            until=datetime(2026, 5, 14, 10, 0, tzinfo=UTC),
            resume_token=ResumeToken(value="20260514093000"),
        )
    )
    # Should have requested the 09:45 window first.
    assert any("20260514094500" in str(req.url) for req in transport.requests_received)
    assert all(p[1].value == "20260514094500" for p in pairs)


def test_resume_token_invalid_raises(store: DuckDBStore) -> None:
    connector = GdeltEventsConnector(store, client=httpx.Client())
    with pytest.raises(ValueError, match="invalid GDELT window token"):
        list(
            connector.fetch_backfill(
                until=datetime(2026, 5, 14, tzinfo=UTC),
                resume_token=ResumeToken(value="not-a-timestamp"),
            )
        )
