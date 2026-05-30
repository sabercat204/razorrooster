"""T-063 verification — regulations.gov connector."""

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
from razor_rooster.data_ingest.connectors.regulations_gov import RegulationsGovConnector
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
    db_path = tmp_path / "reggov.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


@pytest.fixture
def credentials() -> ApiKeyBundle:
    return ApiKeyBundle(source_id="regulations_gov", api_key="fake_reggov_key")


def _reggov_response(dockets: list[dict[str, Any]], *, total_pages: int = 1) -> dict[str, Any]:
    return {
        "data": dockets,
        "meta": {"totalElements": len(dockets), "totalPages": total_pages, "pageSize": 250},
    }


def _docket(
    *,
    docket_id: str = "EPA-HQ-OAR-2026-0001",
    title: str = "Test EPA Docket",
    agency: str = "EPA",
    docket_type: str = "Rulemaking",
    posted_date: str = "2026-05-14",
    modified_date: str = "2026-05-14",
) -> dict[str, Any]:
    return {
        "id": docket_id,
        "type": "dockets",
        "attributes": {
            "title": title,
            "agencyId": agency,
            "docketType": docket_type,
            "postedDate": posted_date,
            "modifyDate": modified_date,
            "commentEndDate": None,
        },
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


def test_reggov_self_registers() -> None:
    assert is_registered("regulations_gov")


def test_class_attributes() -> None:
    assert RegulationsGovConnector.source_id == "regulations_gov"
    assert RegulationsGovConnector.canonical_schema == SchemaType.DOCUMENT_DOCKET
    assert RegulationsGovConnector.license == License.PUBLIC_DOMAIN
    assert RegulationsGovConnector.backfill_supported is True


def test_fetch_incremental_yields_records(store: DuckDBStore, credentials: ApiKeyBundle) -> None:
    client, transport = _client(
        [(200, _reggov_response([_docket(docket_id="EPA-HQ-OAR-2026-0001")]))]
    )
    connector = RegulationsGovConnector(store, credentials=credentials, client=client)
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 1
    assert records[0].source_record_id == "EPA-HQ-OAR-2026-0001"
    request = transport.requests_received[0]
    assert request.headers.get("X-Api-Key") == "fake_reggov_key"
    assert request.url.params.get("filter[agencyId]") == "EPA"


def test_fetch_without_credentials_raises(store: DuckDBStore) -> None:
    connector = RegulationsGovConnector(store, credentials=None, client=httpx.Client())
    with pytest.raises(CredentialMissingError, match="REGULATIONS_GOV_API_KEY"):
        list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))


def test_normalize_extracts_attributes(store: DuckDBStore, credentials: ApiKeyBundle) -> None:
    connector = RegulationsGovConnector(store, credentials=credentials, client=httpx.Client())
    raw = RawRecord(
        source_id="regulations_gov",
        source_record_id="EPA-HQ-OAR-2026-0001",
        source_payload_json=_docket(
            docket_id="EPA-HQ-OAR-2026-0001",
            title="Test Rulemaking",
            agency="EPA",
            docket_type="Rulemaking",
            posted_date="2026-05-14",
            modified_date="2026-05-14",
        ),
        source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, DocumentDocketRecord)
    assert normalized.title == "Test Rulemaking"
    assert normalized.docket_id == "EPA-HQ-OAR-2026-0001"
    assert normalized.agency == "EPA"
    assert normalized.document_type == "Rulemaking"
    assert normalized.published_date == date(2026, 5, 14)
    assert normalized.full_text_uri == "https://www.regulations.gov/docket/EPA-HQ-OAR-2026-0001"


def test_fetch_backfill_emits_resume_tokens(store: DuckDBStore, credentials: ApiKeyBundle) -> None:
    client, _ = _client([(200, _reggov_response([_docket(docket_id="EPA-2003-0001")]))])
    connector = RegulationsGovConnector(store, credentials=credentials, client=client)
    pairs = list(connector.fetch_backfill(until=datetime(2026, 5, 14, tzinfo=UTC)))
    assert len(pairs) == 1
    assert pairs[0][1].value == "2026-05-14:1"


def test_fetch_backfill_resumes_from_token(store: DuckDBStore, credentials: ApiKeyBundle) -> None:
    client, transport = _client([(200, _reggov_response([_docket(docket_id="EPA-2024-0001")]))])
    connector = RegulationsGovConnector(store, credentials=credentials, client=client)
    list(
        connector.fetch_backfill(
            until=datetime(2026, 5, 14, tzinfo=UTC),
            resume_token=ResumeToken(value="2026-05-14:1"),
        )
    )
    request = transport.requests_received[0]
    assert request.url.params.get("page[number]") == "2"
