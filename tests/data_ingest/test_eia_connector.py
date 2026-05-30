"""T-061 verification — EIA connector."""

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
from razor_rooster.data_ingest.connectors.eia import (
    EiaConnector,
    EiaSeries,
    load_eia_config,
)
from razor_rooster.data_ingest.credentials import ApiKeyBundle
from razor_rooster.data_ingest.normalization.base import RawRecord, TimeSeriesRecord
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import is_registered

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_CONFIG = _REPO_ROOT / "config" / "eia_series.yaml"


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "eia.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


@pytest.fixture
def credentials() -> ApiKeyBundle:
    return ApiKeyBundle(source_id="eia", api_key="fake_eia_key")


def _eia_response(rows: list[dict[str, Any]], *, total: int | None = None) -> dict[str, Any]:
    return {
        "response": {
            "total": total if total is not None else len(rows),
            "frequency": "daily",
            "data": rows,
            "description": "Test response",
        },
        "request": {},
        "apiVersion": "2.1.0",
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


# --- registration & config -------------------------------------------------


def test_eia_self_registers() -> None:
    assert is_registered("eia")


def test_class_attributes() -> None:
    assert EiaConnector.source_id == "eia"
    assert EiaConnector.canonical_schema == SchemaType.TIME_SERIES
    assert EiaConnector.license == License.PUBLIC_DOMAIN
    assert EiaConnector.backfill_supported is True


def test_load_bundled_config() -> None:
    series = load_eia_config(_BUNDLED_CONFIG)
    assert len(series) > 0
    assert all(isinstance(s, EiaSeries) for s in series)


def test_load_config_rejects_missing_facets_key(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
version: 1
series:
  - id: TEST
    route: "x/y"
    facets: 42
    title: "Test"
    frequency: D
    units: x
"""
    )
    with pytest.raises(ValueError, match="facets"):
        load_eia_config(bad)


def _single_config(tmp_path: Path) -> Path:
    config = tmp_path / "eia.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "series": [
                    {
                        "id": "PET.RWTC.D",
                        "route": "petroleum/pri/spt",
                        "facets": {"product": ["EPCWTI"]},
                        "title": "WTI",
                        "frequency": "D",
                        "units": "usd_per_barrel",
                    }
                ],
            }
        )
    )
    return config


# --- fetch + normalize -----------------------------------------------------


def test_fetch_incremental_yields_records(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    config = _single_config(tmp_path)
    client, transport = _client(
        [
            (
                200,
                _eia_response(
                    [
                        {"period": "2026-05-13", "value": "82.40"},
                        {"period": "2026-05-14", "value": "82.55"},
                    ]
                ),
            )
        ]
    )
    connector = EiaConnector(store, credentials=credentials, config_path=config, client=client)
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 2
    assert records[0].source_record_id == "PET.RWTC.D:2026-05-13"
    request = transport.requests_received[0]
    assert "api_key=fake_eia_key" in str(request.url)
    assert "frequency=daily" in str(request.url)


def test_fetch_without_credentials_raises(store: DuckDBStore, tmp_path: Path) -> None:
    config = _single_config(tmp_path)
    connector = EiaConnector(store, credentials=None, config_path=config, client=httpx.Client())
    with pytest.raises(CredentialMissingError, match="EIA_API_KEY"):
        list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))


def test_normalize_handles_period_formats(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    config = _single_config(tmp_path)
    connector = EiaConnector(
        store, credentials=credentials, config_path=config, client=httpx.Client()
    )

    # Daily.
    raw_d = RawRecord(
        source_id="eia",
        source_record_id="X:2026-05-14",
        source_payload_json={
            "series_id": "X",
            "period": "2026-05-14",
            "value": "1.0",
            "frequency": "D",
        },
        source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
    )
    n_d = connector.normalize(raw_d)
    assert isinstance(n_d, TimeSeriesRecord)
    assert n_d.observation_ts == datetime(2026, 5, 14, tzinfo=UTC)

    # Monthly.
    raw_m = RawRecord(
        source_id="eia",
        source_record_id="X:2026-05",
        source_payload_json={
            "series_id": "X",
            "period": "2026-05",
            "value": "100",
            "frequency": "M",
        },
        source_publication_ts=datetime(2026, 5, 1, tzinfo=UTC),
    )
    n_m = connector.normalize(raw_m)
    assert isinstance(n_m, TimeSeriesRecord)
    assert n_m.observation_ts == datetime(2026, 5, 1, tzinfo=UTC)

    # Quarterly.
    raw_q = RawRecord(
        source_id="eia",
        source_record_id="X:2026-Q2",
        source_payload_json={
            "series_id": "X",
            "period": "2026-Q2",
            "value": "200",
            "frequency": "Q",
        },
        source_publication_ts=datetime(2026, 4, 1, tzinfo=UTC),
    )
    n_q = connector.normalize(raw_q)
    assert isinstance(n_q, TimeSeriesRecord)
    assert n_q.observation_ts == datetime(2026, 4, 1, tzinfo=UTC)

    # Annual.
    raw_a = RawRecord(
        source_id="eia",
        source_record_id="X:2026",
        source_payload_json={
            "series_id": "X",
            "period": "2026",
            "value": "300",
            "frequency": "A",
        },
        source_publication_ts=datetime(2026, 1, 1, tzinfo=UTC),
    )
    n_a = connector.normalize(raw_a)
    assert isinstance(n_a, TimeSeriesRecord)
    assert n_a.observation_ts == datetime(2026, 1, 1, tzinfo=UTC)


def test_normalize_handles_null_value(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    config = _single_config(tmp_path)
    connector = EiaConnector(
        store, credentials=credentials, config_path=config, client=httpx.Client()
    )
    raw = RawRecord(
        source_id="eia",
        source_record_id="X:2026-05-14",
        source_payload_json={
            "series_id": "X",
            "period": "2026-05-14",
            "value": None,
            "frequency": "D",
        },
        source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, TimeSeriesRecord)
    assert normalized.value is None


def test_fetch_backfill_emits_resume_tokens(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    config = _single_config(tmp_path)
    client, _ = _client(
        [
            (
                200,
                _eia_response(
                    [{"period": "2026-05-14", "value": "82.55"}],
                    total=1,
                ),
            )
        ]
    )
    connector = EiaConnector(store, credentials=credentials, config_path=config, client=client)
    pairs = list(connector.fetch_backfill(until=datetime(2026, 5, 14, tzinfo=UTC)))
    assert len(pairs) == 1
    assert pairs[0][1].value == "PET.RWTC.D:0"


def test_fetch_backfill_resumes_from_token(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    config = _single_config(tmp_path)
    client, transport = _client(
        [(200, _eia_response([{"period": "2026-05-14", "value": "82.55"}], total=1))]
    )
    connector = EiaConnector(store, credentials=credentials, config_path=config, client=client)
    list(
        connector.fetch_backfill(
            until=datetime(2026, 5, 14, tzinfo=UTC),
            resume_token=ResumeToken(value="PET.RWTC.D:0"),
        )
    )
    request = transport.requests_received[0]
    assert "offset=5000" in str(request.url)
