"""T-053 verification — Federal Register connector."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from razor_rooster.data_ingest.connectors.base import (
    License,
    RateLimitedError,
    ResumeToken,
)
from razor_rooster.data_ingest.connectors.federal_register import (
    FederalRegisterConnector,
)
from razor_rooster.data_ingest.normalization.base import DocumentDocketRecord, RawRecord
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import is_registered


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "fr.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


class _CannedTransport(httpx.MockTransport):
    def __init__(self, responses: list[tuple[int, dict[str, Any]]]) -> None:
        self._responses = list(responses)
        self.requests_received: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests_received.append(request)
            if not self._responses:
                return httpx.Response(500, json={"error": "no canned response"})
            status, body = self._responses.pop(0)
            return httpx.Response(status, json=body)

        super().__init__(handler)


def _client(responses: list[tuple[int, dict[str, Any]]]) -> tuple[httpx.Client, _CannedTransport]:
    transport = _CannedTransport(responses)
    return httpx.Client(transport=transport, timeout=5.0), transport


def _fr_response(docs: list[dict[str, Any]], *, total_pages: int = 1) -> dict[str, Any]:
    return {
        "count": len(docs),
        "total_pages": total_pages,
        "next_page_url": None,
        "results": docs,
    }


def _doc(
    *,
    document_number: str,
    title: str,
    doc_type: str = "rule",
    publication_date: str = "2026-05-14",
    agency: str = "Environmental Protection Agency",
    docket_id: str | None = "EPA-HQ-OAR-2026-0001",
) -> dict[str, Any]:
    return {
        "document_number": document_number,
        "title": title,
        "type": doc_type,
        "publication_date": publication_date,
        "effective_on": "2026-06-01",
        "comments_close_on": None,
        "html_url": f"https://www.federalregister.gov/documents/2026/05/14/{document_number}",
        "abstract": "Test abstract.",
        "agencies": [{"name": agency, "raw_name": agency.upper()}],
        "docket_id": docket_id,
        "docket_ids": [docket_id] if docket_id else [],
    }


def test_federal_register_self_registers() -> None:
    assert is_registered("federal_register")


def test_class_attributes() -> None:
    assert FederalRegisterConnector.source_id == "federal_register"
    assert FederalRegisterConnector.canonical_schema == SchemaType.DOCUMENT_DOCKET
    assert FederalRegisterConnector.license == License.PUBLIC_DOMAIN
    assert FederalRegisterConnector.backfill_supported is True


def test_fetch_incremental_yields_records(store: DuckDBStore) -> None:
    client, transport = _client(
        [
            (
                200,
                _fr_response(
                    [_doc(document_number="2026-001", title="Test Rule One")],
                ),
            )
        ]
    )
    connector = FederalRegisterConnector(store, client=client)
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 1
    assert records[0].source_record_id == "2026-001"
    request = transport.requests_received[0]
    # httpx URL-encodes brackets; check via the params API rather than string.
    assert request.url.params.get("conditions[publication_date][gte]") == "2026-05-01"


def test_fetch_incremental_paginates(store: DuckDBStore) -> None:
    client, transport = _client(
        [
            (
                200,
                _fr_response([_doc(document_number="2026-A", title="A")], total_pages=2),
            ),
            (
                200,
                _fr_response([_doc(document_number="2026-B", title="B")], total_pages=2),
            ),
        ]
    )
    connector = FederalRegisterConnector(store, client=client)
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 2
    assert len(transport.requests_received) == 2


def test_fetch_backfill_emits_resume_tokens(store: DuckDBStore) -> None:
    client, _ = _client(
        [
            (
                200,
                _fr_response([_doc(document_number="2025-001", title="X")]),
            )
        ]
    )
    connector = FederalRegisterConnector(store, client=client)
    pairs = list(connector.fetch_backfill(until=datetime(2026, 5, 14, tzinfo=UTC)))
    assert len(pairs) == 1
    assert pairs[0][1].value == "2026-05-14:1"


def test_fetch_backfill_resumes_from_token(store: DuckDBStore) -> None:
    client, transport = _client(
        [
            (
                200,
                _fr_response([_doc(document_number="2026-002", title="Resumed")]),
            )
        ]
    )
    connector = FederalRegisterConnector(store, client=client)
    list(
        connector.fetch_backfill(
            until=datetime(2026, 5, 14, tzinfo=UTC),
            resume_token=ResumeToken(value="2026-05-14:1"),
        )
    )
    request = transport.requests_received[0]
    assert "page=2" in str(request.url)


def test_normalize_extracts_document_metadata(store: DuckDBStore) -> None:
    connector = FederalRegisterConnector(store, client=httpx.Client())
    raw = RawRecord(
        source_id="federal_register",
        source_record_id="2026-001",
        source_payload_json=_doc(
            document_number="2026-001",
            title="Test Rule",
            doc_type="proposed_rule",
            publication_date="2026-05-14",
        ),
        source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, DocumentDocketRecord)
    assert normalized.title == "Test Rule"
    assert normalized.document_type == "proposed_rule"
    assert normalized.docket_id == "EPA-HQ-OAR-2026-0001"
    assert normalized.agency == "Environmental Protection Agency"
    assert normalized.published_date == date(2026, 5, 14)
    assert normalized.full_text_uri is not None


def test_normalize_handles_missing_optional_fields(store: DuckDBStore) -> None:
    connector = FederalRegisterConnector(store, client=httpx.Client())
    raw = RawRecord(
        source_id="federal_register",
        source_record_id="2026-002",
        source_payload_json={
            "document_number": "2026-002",
            "title": "Notice",
            "type": "notice",
            "publication_date": "2026-05-14",
        },
        source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, DocumentDocketRecord)
    assert normalized.docket_id is None
    assert normalized.agency is None
    assert normalized.effective_date is None


def test_429_retries_then_raises(store: DuckDBStore) -> None:
    client, _ = _client([(429, {"error": "rate limit"})] * 6)
    connector = FederalRegisterConnector(store, client=client)
    import razor_rooster.data_ingest.connectors.federal_register as fr_module

    original = fr_module.exponential_backoff_with_jitter
    fr_module.exponential_backoff_with_jitter = lambda *a, **k: 0.0  # type: ignore[assignment]
    try:
        with pytest.raises(RateLimitedError):
            list(connector.fetch_incremental(since=datetime(2026, 1, 1, tzinfo=UTC)))
    finally:
        fr_module.exponential_backoff_with_jitter = original
