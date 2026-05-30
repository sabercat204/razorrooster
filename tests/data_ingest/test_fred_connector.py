"""T-050 verification — FRED connector.

Verifies:
- Loading the bundled FRED series config.
- Incremental fetch parses a representative response into RawRecord stream.
- Backfill fetch parses + emits resume tokens correctly.
- Backfill resume picks up from the next series after a token's series.
- Backfill resume picks up from the day after a token's date within the
  same series.
- Normalization handles "." (missing) values as None.
- Normalization respects FRED's date format (YYYY-MM-DD → UTC datetime).
- 429/5xx responses retry with backoff up to the cap, then raise RateLimitedError.
- Missing credentials raise CredentialMissingError.
- The connector self-registers in the source registry on import.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from razor_rooster.data_ingest.connectors.base import (
    CredentialMissingError,
    License,
    RateLimitedError,
    ResumeToken,
)
from razor_rooster.data_ingest.connectors.fred import (
    FredConnector,
    FredSeries,
    load_fred_series_config,
)
from razor_rooster.data_ingest.credentials import ApiKeyBundle
from razor_rooster.data_ingest.normalization.base import TimeSeriesRecord
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import is_registered

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_FRED_CONFIG = _REPO_ROOT / "config" / "fred_series.yaml"


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "fred.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


@pytest.fixture
def credentials() -> ApiKeyBundle:
    return ApiKeyBundle(source_id="fred", api_key="fake_test_key_for_unit_tests")


# Mock-transport helper: intercept httpx requests and return canned responses.
class _CannedTransport(httpx.MockTransport):
    """A test transport that returns pre-canned responses per URL pattern."""

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


def _client_with_responses(
    responses: list[tuple[int, dict[str, Any]]],
) -> tuple[httpx.Client, _CannedTransport]:
    transport = _CannedTransport(responses)
    return httpx.Client(transport=transport, timeout=5.0), transport


def _ok_observations(series_id: str, dates_values: list[tuple[str, str]]) -> dict[str, Any]:
    """Build a representative FRED API success body."""
    return {
        "realtime_start": "2026-05-14",
        "realtime_end": "2026-05-14",
        "observation_start": "1900-01-01",
        "observation_end": "9999-12-31",
        "units": "lin",
        "output_type": 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "count": len(dates_values),
        "offset": 0,
        "limit": 100000,
        "observations": [
            {
                "realtime_start": "2026-05-14",
                "realtime_end": "2026-05-14",
                "date": d,
                "value": v,
            }
            for d, v in dates_values
        ],
    }


# --- config loading --------------------------------------------------------


def test_load_bundled_fred_series_config() -> None:
    series = load_fred_series_config(_BUNDLED_FRED_CONFIG)
    assert len(series) > 0
    assert all(isinstance(s, FredSeries) for s in series)
    series_ids = {s.id for s in series}
    assert {"DGS10", "UNRATE", "GDPC1"} <= series_ids


def test_load_fred_series_config_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_fred_series_config(tmp_path / "nonexistent.yaml")


def test_load_fred_series_config_rejects_missing_series_key(tmp_path: Path) -> None:
    bad_path = tmp_path / "bad.yaml"
    bad_path.write_text("version: 1\n")
    with pytest.raises(ValueError, match="series"):
        load_fred_series_config(bad_path)


# --- registration ----------------------------------------------------------


def test_fred_connector_self_registers() -> None:
    """Importing the module registers the connector via the @register decorator."""
    # Just importing fred.py at the top of this test file is enough to register.
    assert is_registered("fred")


def test_fred_connector_class_attributes_match_spec() -> None:
    assert FredConnector.source_id == "fred"
    assert FredConnector.canonical_schema == SchemaType.TIME_SERIES
    assert FredConnector.license == License.PUBLIC_DOMAIN
    assert FredConnector.backfill_supported is True
    assert FredConnector.cadence_default == "daily"


# --- fetch happy paths -----------------------------------------------------


def test_fetch_incremental_yields_records(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    series_config = tmp_path / "series.yaml"
    series_config.write_text(
        json.dumps(
            {
                "version": 1,
                "series": [{"id": "DGS10", "title": "10Y", "frequency": "D", "units": "percent"}],
            }
        )
    )
    client, transport = _client_with_responses(
        [(200, _ok_observations("DGS10", [("2026-05-13", "4.27"), ("2026-05-14", "4.30")]))]
    )
    connector = FredConnector(
        store,
        credentials=credentials,
        series_config_path=series_config,
        client=client,
    )
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 2
    assert records[0].source_record_id == "DGS10:2026-05-13"
    assert records[0].source_payload_json["value"] == "4.27"
    assert len(transport.requests_received) == 1
    request = transport.requests_received[0]
    assert "series_id=DGS10" in str(request.url)
    assert "observation_start=2026-05-01" in str(request.url)


def test_fetch_backfill_emits_resume_tokens(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    series_config = tmp_path / "series.yaml"
    series_config.write_text(
        json.dumps(
            {
                "version": 1,
                "series": [
                    {"id": "DGS10", "title": "10Y", "frequency": "D", "units": "percent"},
                    {"id": "UNRATE", "title": "Unemployment", "frequency": "M", "units": "percent"},
                ],
            }
        )
    )
    client, _ = _client_with_responses(
        [
            (200, _ok_observations("DGS10", [("2026-05-13", "4.27"), ("2026-05-14", "4.30")])),
            (200, _ok_observations("UNRATE", [("2026-04-01", "3.6")])),
        ]
    )
    connector = FredConnector(
        store,
        credentials=credentials,
        series_config_path=series_config,
        client=client,
    )
    pairs = list(connector.fetch_backfill(until=datetime(2026, 5, 14, tzinfo=UTC)))
    assert len(pairs) == 3
    tokens = [p[1].value for p in pairs if p[1] is not None]
    assert tokens == ["DGS10:2026-05-13", "DGS10:2026-05-14", "UNRATE:2026-04-01"]


def test_fetch_backfill_resumes_from_token(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    """Resume from 'DGS10:2026-05-13' → only DGS10 records after that date + the UNRATE series."""
    series_config = tmp_path / "series.yaml"
    series_config.write_text(
        json.dumps(
            {
                "version": 1,
                "series": [
                    {"id": "DGS10", "title": "10Y", "frequency": "D", "units": "percent"},
                    {"id": "UNRATE", "title": "Unemployment", "frequency": "M", "units": "percent"},
                ],
            }
        )
    )
    client, transport = _client_with_responses(
        [
            (200, _ok_observations("DGS10", [("2026-05-14", "4.30")])),
            (200, _ok_observations("UNRATE", [("2026-04-01", "3.6")])),
        ]
    )
    connector = FredConnector(
        store,
        credentials=credentials,
        series_config_path=series_config,
        client=client,
    )
    pairs = list(
        connector.fetch_backfill(
            until=datetime(2026, 5, 14, tzinfo=UTC),
            resume_token=ResumeToken(value="DGS10:2026-05-13"),
        )
    )
    record_ids = [p[0].source_record_id for p in pairs]
    assert record_ids == ["DGS10:2026-05-14", "UNRATE:2026-04-01"]
    # The first request should have observation_start=2026-05-14 (day after).
    first_request = transport.requests_received[0]
    assert "observation_start=2026-05-14" in str(first_request.url)


def test_fetch_backfill_resumes_after_unknown_series_in_token(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    """If the token names a series not in the current config, skip until found."""
    series_config = tmp_path / "series.yaml"
    series_config.write_text(
        json.dumps(
            {
                "version": 1,
                "series": [{"id": "DGS10", "title": "10Y", "frequency": "D", "units": "percent"}],
            }
        )
    )
    client, transport = _client_with_responses([])
    connector = FredConnector(
        store,
        credentials=credentials,
        series_config_path=series_config,
        client=client,
    )
    # Token's series 'NOTREAL' isn't in the config → all series get skipped.
    pairs = list(
        connector.fetch_backfill(
            until=datetime(2026, 5, 14, tzinfo=UTC),
            resume_token=ResumeToken(value="NOTREAL:2026-01-01"),
        )
    )
    assert pairs == []
    assert transport.requests_received == []


# --- normalization ---------------------------------------------------------


def test_normalize_handles_missing_value(store: DuckDBStore, credentials: ApiKeyBundle) -> None:
    connector = FredConnector(
        store,
        credentials=credentials,
        series_config_path=_BUNDLED_FRED_CONFIG,
        client=httpx.Client(),
    )
    raw = _make_raw(
        {
            "series_id": "DGS10",
            "observation_date": "2026-05-13",
            "value": ".",
            "units": "percent",
            "frequency": "D",
        }
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, TimeSeriesRecord)
    assert normalized.value is None
    assert normalized.series_id == "DGS10"


def test_normalize_parses_numeric_value(store: DuckDBStore, credentials: ApiKeyBundle) -> None:
    connector = FredConnector(
        store,
        credentials=credentials,
        series_config_path=_BUNDLED_FRED_CONFIG,
        client=httpx.Client(),
    )
    raw = _make_raw(
        {
            "series_id": "DGS10",
            "observation_date": "2026-05-13",
            "value": "4.27",
            "units": "percent",
            "frequency": "D",
        }
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, TimeSeriesRecord)
    assert normalized.value == 4.27
    assert normalized.observation_ts == datetime(2026, 5, 13, tzinfo=UTC)


def test_normalize_handles_empty_string_value(
    store: DuckDBStore, credentials: ApiKeyBundle
) -> None:
    connector = FredConnector(
        store,
        credentials=credentials,
        series_config_path=_BUNDLED_FRED_CONFIG,
        client=httpx.Client(),
    )
    raw = _make_raw(
        {
            "series_id": "DGS10",
            "observation_date": "2026-05-13",
            "value": "",
            "units": "percent",
            "frequency": "D",
        }
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, TimeSeriesRecord)
    assert normalized.value is None


# --- error handling --------------------------------------------------------


def test_fetch_without_credentials_raises(store: DuckDBStore, tmp_path: Path) -> None:
    series_config = tmp_path / "s.yaml"
    series_config.write_text(
        json.dumps(
            {
                "version": 1,
                "series": [{"id": "DGS10", "title": "x", "frequency": "D", "units": "percent"}],
            }
        )
    )
    connector = FredConnector(
        store,
        credentials=None,
        series_config_path=series_config,
        client=httpx.Client(),
    )
    with pytest.raises(CredentialMissingError, match="FRED_API_KEY"):
        list(connector.fetch_incremental(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_429_responses_retry_then_raise_rate_limited(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    series_config = tmp_path / "s.yaml"
    series_config.write_text(
        json.dumps(
            {
                "version": 1,
                "series": [{"id": "DGS10", "title": "x", "frequency": "D", "units": "percent"}],
            }
        )
    )
    # Six 429s — the connector tries the first plus 5 retries, all fail.
    client, transport = _client_with_responses([(429, {"error": "rate limited"})] * 6)
    connector = FredConnector(
        store,
        credentials=credentials,
        series_config_path=series_config,
        client=client,
    )
    # Dial the rate limiter down to zero so the test runs fast.
    connector._last_request_at = 0.0  # type: ignore[reportPrivateUsage]
    # Patch the sleep so we don't actually wait 60 seconds in tests.
    import razor_rooster.data_ingest.connectors.fred as fred_module

    original_backoff = fred_module.exponential_backoff_with_jitter
    fred_module.exponential_backoff_with_jitter = lambda *a, **k: 0.0  # type: ignore[assignment]
    try:
        with pytest.raises(RateLimitedError, match="rate-limited"):
            list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    finally:
        fred_module.exponential_backoff_with_jitter = original_backoff
    assert len(transport.requests_received) == 6


def test_5xx_responses_retry_then_raise(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    series_config = tmp_path / "s.yaml"
    series_config.write_text(
        json.dumps(
            {
                "version": 1,
                "series": [{"id": "DGS10", "title": "x", "frequency": "D", "units": "percent"}],
            }
        )
    )
    client, _ = _client_with_responses([(503, {"error": "service unavailable"})] * 6)
    connector = FredConnector(
        store,
        credentials=credentials,
        series_config_path=series_config,
        client=client,
    )
    import razor_rooster.data_ingest.connectors.fred as fred_module

    original_backoff = fred_module.exponential_backoff_with_jitter
    fred_module.exponential_backoff_with_jitter = lambda *a, **k: 0.0  # type: ignore[assignment]
    try:
        with pytest.raises(RateLimitedError):
            list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    finally:
        fred_module.exponential_backoff_with_jitter = original_backoff


def test_4xx_non_429_raises_immediately(
    store: DuckDBStore, credentials: ApiKeyBundle, tmp_path: Path
) -> None:
    series_config = tmp_path / "s.yaml"
    series_config.write_text(
        json.dumps(
            {
                "version": 1,
                "series": [{"id": "DGS10", "title": "x", "frequency": "D", "units": "percent"}],
            }
        )
    )
    client, _ = _client_with_responses([(400, {"error": "bad request"})])
    connector = FredConnector(
        store,
        credentials=credentials,
        series_config_path=series_config,
        client=client,
    )
    with pytest.raises(httpx.HTTPStatusError):
        list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))


# --- helpers ---------------------------------------------------------------


def _make_raw(payload: dict[str, Any]) -> Any:
    """Build a RawRecord-shaped object for testing normalize()."""
    from razor_rooster.data_ingest.normalization.base import RawRecord

    return RawRecord(
        source_id="fred",
        source_record_id=f"{payload['series_id']}:{payload['observation_date']}",
        source_payload_json=payload,
        source_publication_ts=datetime.now(tz=UTC),
    )


def _capture_iter(it: Iterator[Any]) -> list[Any]:
    return list(it)
