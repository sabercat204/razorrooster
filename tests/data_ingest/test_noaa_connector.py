"""T-055 verification — NOAA CDO connector."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from razor_rooster.data_ingest.connectors.base import (
    CredentialMissingError,
    License,
    ResumeToken,
)
from razor_rooster.data_ingest.connectors.noaa import (
    NoaaConnector,
    NoaaQuery,
    load_noaa_config,
)
from razor_rooster.data_ingest.credentials import ApiKeyBundle
from razor_rooster.data_ingest.normalization.base import RawRecord, TimeSeriesRecord
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import is_registered

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_CONFIG = _REPO_ROOT / "config" / "noaa_datasets.yaml"


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "noaa.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


@pytest.fixture
def credentials() -> ApiKeyBundle:
    return ApiKeyBundle(source_id="noaa", api_key="fake_noaa_token")


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


def _noaa_response(
    results: list[dict[str, Any]],
    *,
    offset: int = 1,
    limit: int = 1000,
    count: int | None = None,
) -> dict[str, Any]:
    return {
        "metadata": {
            "resultset": {
                "offset": offset,
                "count": count if count is not None else len(results),
                "limit": limit,
            }
        },
        "results": results,
    }


def _obs(*, datatype: str, date_str: str, value: float) -> dict[str, Any]:
    return {
        "date": date_str,
        "datatype": datatype,
        "station": "GHCND:USW00023234",
        "attributes": ",,W,",
        "value": value,
    }


def test_noaa_self_registers() -> None:
    assert is_registered("noaa")


def test_class_attributes() -> None:
    assert NoaaConnector.source_id == "noaa"
    assert NoaaConnector.canonical_schema == SchemaType.TIME_SERIES
    assert NoaaConnector.license == License.PUBLIC_DOMAIN
    assert NoaaConnector.backfill_supported is True


def test_load_bundled_config() -> None:
    queries, min_interval = load_noaa_config(_BUNDLED_CONFIG)
    assert len(queries) > 0
    assert all(isinstance(q, NoaaQuery) for q in queries)
    assert min_interval > 0


def test_load_config_rejects_empty_datatypes(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        json.dumps(
            {
                "version": 1,
                "queries": [
                    {
                        "dataset": "GHCND",
                        "station": "X",
                        "datatypes": [],
                        "title": "x",
                        "start_date": "2020-01-01",
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="datatypes"):
        load_noaa_config(bad)


def _single_query_config(tmp_path: Path) -> Path:
    config = tmp_path / "noaa.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "rate_limit": {"min_interval_seconds": 0},
                "queries": [
                    {
                        "dataset": "GHCND",
                        "station": "GHCND:USW00023234",
                        "datatypes": ["TMAX", "TMIN"],
                        "title": "Test",
                        "start_date": "2020-01-01",
                    }
                ],
            }
        )
    )
    return config


def test_fetch_incremental_yields_records(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    config = _single_query_config(tmp_path)
    client, transport = _client(
        [
            (
                200,
                _noaa_response(
                    [
                        _obs(datatype="TMAX", date_str="2026-05-13T00:00:00", value=72.0),
                        _obs(datatype="TMIN", date_str="2026-05-13T00:00:00", value=55.0),
                    ]
                ),
            )
        ]
    )
    connector = NoaaConnector(store, credentials=credentials, config_path=config, client=client)
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 2
    request = transport.requests_received[0]
    # token in header
    assert request.headers.get("token") == "fake_noaa_token"
    # query params contain dataset id
    assert "datasetid=GHCND" in str(request.url)


def test_fetch_without_credentials_raises(store: DuckDBStore, tmp_path: Path) -> None:
    config = _single_query_config(tmp_path)
    connector = NoaaConnector(store, credentials=None, config_path=config, client=httpx.Client())
    with pytest.raises(CredentialMissingError):
        list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))


def test_normalize_parses_iso_timestamp(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    config = _single_query_config(tmp_path)
    connector = NoaaConnector(
        store, credentials=credentials, config_path=config, client=httpx.Client()
    )
    raw = RawRecord(
        source_id="noaa",
        source_record_id="GHCND:USW00023234:TMAX:2026-05-13T00:00:00",
        source_payload_json={
            "dataset": "GHCND",
            "station": "GHCND:USW00023234",
            "datatype": "TMAX",
            "date": "2026-05-13T00:00:00",
            "value": 72.0,
            "attributes": ",,W,",
        },
        source_publication_ts=datetime(2026, 5, 13, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, TimeSeriesRecord)
    assert normalized.value == 72.0
    assert normalized.observation_ts == datetime(2026, 5, 13, tzinfo=UTC)
    assert normalized.series_id == "GHCND:GHCND:USW00023234:TMAX"


def test_fetch_backfill_emits_resume_tokens(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    config = _single_query_config(tmp_path)
    client, _ = _client(
        [
            # First request for TMAX returns one result (no further pages).
            (
                200,
                _noaa_response([_obs(datatype="TMAX", date_str="2026-05-13T00:00:00", value=72.0)]),
            ),
            # Second request for TMIN.
            (
                200,
                _noaa_response([_obs(datatype="TMIN", date_str="2026-05-13T00:00:00", value=55.0)]),
            ),
        ]
    )
    connector = NoaaConnector(store, credentials=credentials, config_path=config, client=client)
    pairs = list(connector.fetch_backfill(until=datetime(2026, 5, 14, tzinfo=UTC)))
    assert len(pairs) == 2
    tokens = [p[1].value for p in pairs]
    assert any("TMAX" in t for t in tokens)
    assert any("TMIN" in t for t in tokens)


def test_fetch_backfill_resumes_from_token(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    """Resume from a TMAX:offset=1 token; subsequent pages run."""
    config = _single_query_config(tmp_path)
    client, transport = _client(
        [
            # TMAX page=2 returns empty results so the loop moves on to TMIN.
            (200, _noaa_response([], offset=2, count=1)),
            # TMIN page=1.
            (
                200,
                _noaa_response([_obs(datatype="TMIN", date_str="2026-05-13T00:00:00", value=55.0)]),
            ),
        ]
    )
    connector = NoaaConnector(store, credentials=credentials, config_path=config, client=client)
    pairs = list(
        connector.fetch_backfill(
            until=datetime(2026, 5, 14, tzinfo=UTC),
            resume_token=ResumeToken(value="GHCND:GHCND:USW00023234:TMAX:1"),
        )
    )
    # We expect at least one TMIN record (TMAX page=2 was empty).
    assert any("TMIN" in p[1].value for p in pairs)
    # Verify the first request was for TMAX (the resume target).
    assert "datatypeid=TMAX" in str(transport.requests_received[0].url)
