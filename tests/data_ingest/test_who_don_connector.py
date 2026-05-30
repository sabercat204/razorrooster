"""T-054 verification — WHO DON connector."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from razor_rooster.data_ingest.connectors.base import License, RateLimitedError
from razor_rooster.data_ingest.connectors.who_don import WhoDonConnector
from razor_rooster.data_ingest.normalization.base import EventStreamRecord, RawRecord
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import is_registered


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "who.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


_SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>WHO Disease Outbreak News</title>
    <link>https://www.who.int/emergencies/disease-outbreak-news</link>
    <description>WHO DON</description>
    <item>
      <title>Cholera – Sudan</title>
      <link>https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON123</link>
      <description>&lt;p&gt;Brief description with some &lt;em&gt;HTML&lt;/em&gt; markup.&lt;/p&gt;</description>
      <pubDate>Wed, 14 May 2026 09:30:00 GMT</pubDate>
      <guid>https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON123</guid>
    </item>
    <item>
      <title>Mpox – Democratic Republic of the Congo</title>
      <link>https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON124</link>
      <description>Mpox outbreak update.</description>
      <pubDate>Tue, 13 May 2026 12:00:00 GMT</pubDate>
      <guid>https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON124</guid>
    </item>
    <item>
      <title>Avian Influenza A(H5N1) - Cambodia</title>
      <link>https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON125</link>
      <description>One human case.</description>
      <pubDate>Mon, 12 May 2026 16:45:00 GMT</pubDate>
      <guid>https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON125</guid>
    </item>
  </channel>
</rss>
"""  # noqa: RUF001 — WHO uses en-dashes in real titles; this fixture must reflect that.


class _CannedTransport(httpx.MockTransport):
    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self._responses = list(responses)
        self.requests_received: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests_received.append(request)
            if not self._responses:
                return httpx.Response(500, text="no canned response")
            status, body = self._responses.pop(0)
            return httpx.Response(status, text=body)

        super().__init__(handler)


def _client(responses: list[tuple[int, str]]) -> httpx.Client:
    return httpx.Client(transport=_CannedTransport(responses), timeout=5.0)


def test_who_don_self_registers() -> None:
    assert is_registered("who_don")


def test_class_attributes() -> None:
    assert WhoDonConnector.source_id == "who_don"
    assert WhoDonConnector.canonical_schema == SchemaType.EVENT_STREAM
    assert WhoDonConnector.license == License.PUBLIC_DOMAIN
    assert WhoDonConnector.backfill_supported is False


def test_fetch_incremental_yields_recent_entries(store: DuckDBStore) -> None:
    connector = WhoDonConnector(store, client=_client([(200, _SAMPLE_FEED)]))
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 3
    record_ids = [r.source_record_id for r in records]
    assert "https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON123" in record_ids


def test_fetch_incremental_filters_by_since(store: DuckDBStore) -> None:
    connector = WhoDonConnector(store, client=_client([(200, _SAMPLE_FEED)]))
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 13, 13, 0, tzinfo=UTC)))
    assert len(records) == 1
    assert "DON123" in records[0].source_record_id


def test_normalize_extracts_disease_and_country(store: DuckDBStore) -> None:
    connector = WhoDonConnector(store, client=httpx.Client())
    raw = RawRecord(
        source_id="who_don",
        source_record_id="https://example/2026-DON123",
        source_payload_json={
            "title": "Cholera \u2013 Sudan",
            "description": "<p>Brief description.</p>",
            "link": "https://example/2026-DON123",
            "pubDate": "Wed, 14 May 2026 09:30:00 GMT",
            "guid": "https://example/2026-DON123",
        },
        source_publication_ts=datetime(2026, 5, 14, 9, 30, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, EventStreamRecord)
    assert normalized.event_class == "Cholera"
    assert normalized.country_iso3 == "SDN"
    assert normalized.description == "Brief description."


def test_normalize_handles_unsplittable_title(store: DuckDBStore) -> None:
    connector = WhoDonConnector(store, client=httpx.Client())
    raw = RawRecord(
        source_id="who_don",
        source_record_id="X",
        source_payload_json={
            "title": "Unstructured title with no separator",
            "description": "",
            "link": "",
            "pubDate": "",
            "guid": "X",
        },
        source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, EventStreamRecord)
    assert normalized.event_class == "Unstructured title with no separator"
    assert normalized.country_iso3 is None


def test_normalize_handles_country_with_articles(store: DuckDBStore) -> None:
    """'Democratic Republic of the Congo' is in the geo whitelist? It's
    actually ambiguous — this test confirms either an iso3 mapping or
    None, not a crash."""
    connector = WhoDonConnector(store, client=httpx.Client())
    raw = RawRecord(
        source_id="who_don",
        source_record_id="X",
        source_payload_json={
            "title": "Mpox \u2013 Democratic Republic of the Congo",
            "description": "",
            "link": "",
            "pubDate": "",
            "guid": "X",
        },
        source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, EventStreamRecord)
    assert normalized.event_class == "Mpox"
    # DRC isn't in the small whitelist; expect None (not a guess) and a warning.
    assert normalized.country_iso3 is None


def test_429_retries_then_raises(store: DuckDBStore) -> None:
    connector = WhoDonConnector(
        store,
        client=_client([(429, "rate limit")] * 6),
    )
    import razor_rooster.data_ingest.connectors.who_don as who_module

    original = who_module.exponential_backoff_with_jitter
    who_module.exponential_backoff_with_jitter = lambda *a, **k: 0.0  # type: ignore[assignment]
    try:
        with pytest.raises(RateLimitedError):
            list(connector.fetch_incremental(since=datetime(2026, 1, 1, tzinfo=UTC)))
    finally:
        who_module.exponential_backoff_with_jitter = original


def test_invalid_xml_returns_no_records(store: DuckDBStore) -> None:
    connector = WhoDonConnector(
        store,
        client=_client([(200, "<not xml<<<")]),
    )
    records = list(connector.fetch_incremental(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert records == []


def test_backfill_unsupported(store: DuckDBStore) -> None:
    connector = WhoDonConnector(store, client=httpx.Client())
    with pytest.raises(NotImplementedError):
        list(connector.fetch_backfill(until=datetime(2026, 5, 14, tzinfo=UTC)))
