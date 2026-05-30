"""T-062 verification — NRC ADAMS connector."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from razor_rooster.data_ingest.connectors.base import (
    CredentialMissingError,
    License,
    ResumeToken,
)
from razor_rooster.data_ingest.connectors.nrc_adams import NrcAdamsConnector
from razor_rooster.data_ingest.credentials import ApiKeyBundle
from razor_rooster.data_ingest.normalization.base import (
    DocumentDocketRecord,
    RawRecord,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import is_registered


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "nrc.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


@pytest.fixture
def credentials() -> ApiKeyBundle:
    return ApiKeyBundle(source_id="nrc_adams", api_key="fake_nrc_key")


def _nrc_response(docs: list[dict[str, Any]], *, total: int | None = None) -> dict[str, Any]:
    return {"results": docs, "totalCount": total if total is not None else len(docs)}


def _doc(
    *,
    accession: str,
    title: str = "Test ADAMS Document",
    docket: str = "50-247",
    document_type: str = "Letter",
    document_date: str = "2026-05-14",
) -> dict[str, Any]:
    return {
        "accessionNumber": accession,
        "documentTitle": title,
        "documentType": document_type,
        "documentDate": document_date,
        "docketNumber": docket,
        "documentUri": f"https://adams.nrc.gov/wba/document/{accession}",
    }


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


def test_nrc_self_registers() -> None:
    assert is_registered("nrc_adams")


def test_class_attributes() -> None:
    assert NrcAdamsConnector.source_id == "nrc_adams"
    assert NrcAdamsConnector.canonical_schema == SchemaType.DOCUMENT_DOCKET
    assert NrcAdamsConnector.license == License.PUBLIC_DOMAIN
    assert NrcAdamsConnector.cadence_default == "weekly"
    assert NrcAdamsConnector.backfill_supported is True


def test_fetch_incremental_yields_records(store: DuckDBStore, credentials: ApiKeyBundle) -> None:
    client, transport = _client([(200, _nrc_response([_doc(accession="ML2026000001")]))])
    connector = NrcAdamsConnector(store, credentials=credentials, client=client)
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 1
    assert records[0].source_record_id == "ML2026000001"
    request = transport.requests_received[0]
    assert request.headers.get("Ocp-Apim-Subscription-Key") == "fake_nrc_key"
    assert "documentDate.gte=2026-05-01" in str(request.url)


def test_fetch_without_credentials_raises(store: DuckDBStore) -> None:
    connector = NrcAdamsConnector(store, credentials=None, client=httpx.Client())
    with pytest.raises(CredentialMissingError, match="NRC_ADAMS_API_KEY"):
        list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))


def test_normalize_extracts_metadata(store: DuckDBStore, credentials: ApiKeyBundle) -> None:
    connector = NrcAdamsConnector(store, credentials=credentials, client=httpx.Client())
    raw = RawRecord(
        source_id="nrc_adams",
        source_record_id="ML2026000001",
        source_payload_json=_doc(
            accession="ML2026000001",
            title="License Renewal Application",
            docket="50-247",
            document_type="Application",
            document_date="2026-05-14",
        ),
        source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, DocumentDocketRecord)
    assert normalized.title == "License Renewal Application"
    assert normalized.docket_id == "50-247"
    assert normalized.agency == "U.S. Nuclear Regulatory Commission"
    assert normalized.published_date == date(2026, 5, 14)
    assert normalized.full_text_uri is not None


def test_fetch_backfill_emits_resume_tokens(store: DuckDBStore, credentials: ApiKeyBundle) -> None:
    client, _ = _client([(200, _nrc_response([_doc(accession="ML1999000001")]))])
    connector = NrcAdamsConnector(store, credentials=credentials, client=client)
    pairs = list(connector.fetch_backfill(until=datetime(2026, 5, 14, tzinfo=UTC)))
    assert len(pairs) == 1
    assert pairs[0][1].value == "2026-05-14:0"


def test_fetch_backfill_resumes_from_token(store: DuckDBStore, credentials: ApiKeyBundle) -> None:
    client, transport = _client([(200, _nrc_response([_doc(accession="ML1999000002")]))])
    connector = NrcAdamsConnector(store, credentials=credentials, client=client)
    list(
        connector.fetch_backfill(
            until=datetime(2026, 5, 14, tzinfo=UTC),
            resume_token=ResumeToken(value="2026-05-14:0"),
        )
    )
    request = transport.requests_received[0]
    assert "offset=100" in str(request.url)
