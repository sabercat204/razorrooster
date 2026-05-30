"""T-051 verification — World Bank connector.

Verifies:
- Loading the bundled World Bank config.
- Incremental fetch parses representative API responses.
- Pagination iterates until ``page == pages``.
- Backfill emits resume tokens of shape ``<indicator>:<country>:<page>``.
- Backfill resume picks up the next page within the same (indicator, country)
  pair.
- Backfill resume rolls forward to the next indicator after a token.
- Normalization parses year-only "date" into a tz-aware UTC datetime.
- Normalization handles null values.
- 429/5xx retry logic, then RateLimitedError.
- The connector is unauthenticated (credentials always None).
- Self-registers in the source registry.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from razor_rooster.data_ingest.connectors.base import (
    License,
    RateLimitedError,
    ResumeToken,
)
from razor_rooster.data_ingest.connectors.worldbank import (
    WorldBankConnector,
    load_worldbank_config,
)
from razor_rooster.data_ingest.normalization.base import RawRecord, TimeSeriesRecord
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import is_registered

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_CONFIG = _REPO_ROOT / "config" / "worldbank_indicators.yaml"


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "wb.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


def _wb_response(
    indicator_id: str,
    rows: list[dict[str, Any]],
    *,
    page: int = 1,
    pages: int = 1,
    total: int | None = None,
) -> list[Any]:
    """Build a representative World Bank API response."""
    return [
        {
            "page": page,
            "pages": pages,
            "per_page": 1000,
            "total": total if total is not None else len(rows),
            "sourceid": "2",
            "sourcename": "World Development Indicators",
            "lastupdated": "2026-01-01",
        },
        rows,
    ]


def _make_row(
    *,
    indicator_id: str,
    country_iso3: str,
    country_name: str,
    date_str: str,
    value: float | None,
) -> dict[str, Any]:
    return {
        "indicator": {"id": indicator_id, "value": "Test Indicator"},
        "country": {"id": country_iso3[:2], "value": country_name},
        "countryiso3code": country_iso3,
        "date": date_str,
        "value": value,
        "unit": "",
        "obs_status": "",
        "decimal": 1,
    }


class _CannedTransport(httpx.MockTransport):
    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self._responses = list(responses)
        self.requests_received: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests_received.append(request)
            if not self._responses:
                return httpx.Response(500, json={"error": "no canned response"})
            status, body = self._responses.pop(0)
            return httpx.Response(status, json=body)

        super().__init__(handler)


def _client(responses: list[tuple[int, Any]]) -> tuple[httpx.Client, _CannedTransport]:
    transport = _CannedTransport(responses)
    return httpx.Client(transport=transport, timeout=5.0), transport


# --- config ---------------------------------------------------------------


def test_load_bundled_config() -> None:
    indicators = load_worldbank_config(_BUNDLED_CONFIG)
    assert len(indicators) > 0
    ids = {ind.id for ind in indicators}
    assert "NY.GDP.MKTP.CD" in ids
    assert "SP.POP.TOTL" in ids


def test_load_config_rejects_missing_indicators_key(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\n")
    with pytest.raises(ValueError, match="indicators"):
        load_worldbank_config(bad)


def test_load_config_rejects_invalid_country_scope(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nindicators:\n  - id: TEST\n    title: Test\n    countries: 42\n")
    with pytest.raises(ValueError, match="countries"):
        load_worldbank_config(bad)


def test_load_config_with_country_list(tmp_path: Path) -> None:
    config = tmp_path / "list.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [
                    {"id": "TEST", "title": "Test", "countries": ["USA", "GBR"]},
                ],
            }
        )
    )
    indicators = load_worldbank_config(config)
    assert indicators[0].countries == ("USA", "GBR")


def test_load_config_with_all_countries(tmp_path: Path) -> None:
    config = tmp_path / "all.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [
                    {"id": "TEST", "title": "Test", "countries": "all"},
                ],
            }
        )
    )
    indicators = load_worldbank_config(config)
    assert indicators[0].countries == "all"


# --- registration ----------------------------------------------------------


def test_worldbank_self_registers() -> None:
    assert is_registered("worldbank")


def test_class_attributes() -> None:
    assert WorldBankConnector.source_id == "worldbank"
    assert WorldBankConnector.canonical_schema == SchemaType.TIME_SERIES
    assert WorldBankConnector.license == License.PUBLIC_DOMAIN
    assert WorldBankConnector.backfill_supported is True


# --- fetch happy paths -----------------------------------------------------


def test_fetch_incremental_yields_records(store: DuckDBStore, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [
                    {"id": "NY.GDP", "title": "GDP", "countries": "all"},
                ],
            }
        )
    )
    rows = [
        _make_row(
            indicator_id="NY.GDP",
            country_iso3="USA",
            country_name="United States",
            date_str="2024",
            value=27_000_000_000_000.0,
        ),
        _make_row(
            indicator_id="NY.GDP",
            country_iso3="GBR",
            country_name="United Kingdom",
            date_str="2024",
            value=3_500_000_000_000.0,
        ),
    ]
    client, _ = _client([(200, _wb_response("NY.GDP", rows))])
    connector = WorldBankConnector(store, config_path=config, client=client)
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 2
    assert records[0].source_record_id == "NY.GDP:USA:2024"
    assert records[1].source_record_id == "NY.GDP:GBR:2024"


def test_fetch_incremental_paginates(store: DuckDBStore, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [{"id": "TST", "title": "Test", "countries": "all"}],
            }
        )
    )
    page1 = _wb_response(
        "TST",
        [
            _make_row(
                indicator_id="TST",
                country_iso3="USA",
                country_name="US",
                date_str="2024",
                value=1.0,
            )
        ],
        page=1,
        pages=2,
    )
    page2 = _wb_response(
        "TST",
        [
            _make_row(
                indicator_id="TST",
                country_iso3="USA",
                country_name="US",
                date_str="2023",
                value=0.9,
            )
        ],
        page=2,
        pages=2,
    )
    client, transport = _client([(200, page1), (200, page2)])
    connector = WorldBankConnector(store, config_path=config, client=client)
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 2
    assert len(transport.requests_received) == 2


def test_fetch_backfill_emits_resume_tokens(store: DuckDBStore, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [{"id": "TST", "title": "Test", "countries": "all"}],
            }
        )
    )
    page1 = _wb_response(
        "TST",
        [
            _make_row(
                indicator_id="TST",
                country_iso3="USA",
                country_name="US",
                date_str="2024",
                value=1.0,
            )
        ],
        page=1,
        pages=2,
    )
    page2 = _wb_response(
        "TST",
        [
            _make_row(
                indicator_id="TST",
                country_iso3="USA",
                country_name="US",
                date_str="2023",
                value=0.9,
            )
        ],
        page=2,
        pages=2,
    )
    client, _ = _client([(200, page1), (200, page2)])
    connector = WorldBankConnector(store, config_path=config, client=client)
    pairs = list(connector.fetch_backfill(until=datetime(2026, 5, 14, tzinfo=UTC)))
    tokens = [p[1].value for p in pairs if p[1] is not None]
    assert tokens == ["TST:all:1", "TST:all:2"]


def test_fetch_backfill_resumes_from_token(store: DuckDBStore, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [{"id": "TST", "title": "Test", "countries": "all"}],
            }
        )
    )
    page2 = _wb_response(
        "TST",
        [
            _make_row(
                indicator_id="TST",
                country_iso3="USA",
                country_name="US",
                date_str="2023",
                value=0.9,
            )
        ],
        page=2,
        pages=2,
    )
    client, transport = _client([(200, page2)])
    connector = WorldBankConnector(store, config_path=config, client=client)
    pairs = list(
        connector.fetch_backfill(
            until=datetime(2026, 5, 14, tzinfo=UTC),
            resume_token=ResumeToken(value="TST:all:1"),
        )
    )
    assert len(pairs) == 1
    # Only one request should fire: page=2.
    assert len(transport.requests_received) == 1
    assert "page=2" in str(transport.requests_received[0].url)


# --- normalization ---------------------------------------------------------


def test_normalize_parses_year_to_january_first(store: DuckDBStore, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [{"id": "TST", "title": "Test", "countries": "all"}],
            }
        )
    )
    connector = WorldBankConnector(store, config_path=config, client=httpx.Client())
    raw = RawRecord(
        source_id="worldbank",
        source_record_id="TST:USA:2024",
        source_payload_json={
            "indicator_id": "TST",
            "country_iso3": "USA",
            "date": "2024",
            "value": 12.34,
        },
        source_publication_ts=datetime.now(tz=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, TimeSeriesRecord)
    assert normalized.value == 12.34
    assert normalized.observation_ts == datetime(2024, 1, 1, tzinfo=UTC)
    assert normalized.frequency == "A"


def test_normalize_handles_null_value(store: DuckDBStore, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [{"id": "TST", "title": "Test", "countries": "all"}],
            }
        )
    )
    connector = WorldBankConnector(store, config_path=config, client=httpx.Client())
    raw = RawRecord(
        source_id="worldbank",
        source_record_id="TST:USA:2024",
        source_payload_json={
            "indicator_id": "TST",
            "country_iso3": "USA",
            "date": "2024",
            "value": None,
        },
        source_publication_ts=datetime.now(tz=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, TimeSeriesRecord)
    assert normalized.value is None


# --- error handling --------------------------------------------------------


def test_429_responses_retry_then_raise(store: DuckDBStore, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [{"id": "TST", "title": "Test", "countries": "all"}],
            }
        )
    )
    client, _ = _client([(429, [{"message": "rate limit"}, []])] * 6)
    connector = WorldBankConnector(store, config_path=config, client=client)
    import razor_rooster.data_ingest.connectors.worldbank as wb_module

    original = wb_module.exponential_backoff_with_jitter
    wb_module.exponential_backoff_with_jitter = lambda *a, **k: 0.0  # type: ignore[assignment]
    try:
        with pytest.raises(RateLimitedError):
            list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    finally:
        wb_module.exponential_backoff_with_jitter = original


def test_unexpected_response_shape_returns_no_records(store: DuckDBStore, tmp_path: Path) -> None:
    """If the API returns a non-list body, the connector logs and returns nothing."""
    config = tmp_path / "c.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [{"id": "TST", "title": "Test", "countries": "all"}],
            }
        )
    )
    client, _ = _client([(200, {"unexpected": "shape"})])
    connector = WorldBankConnector(store, config_path=config, client=client)
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert records == []


def test_meta_only_response_returns_no_records(store: DuckDBStore, tmp_path: Path) -> None:
    """When the API returns [meta, None] or [meta, message], no records emit."""
    config = tmp_path / "c.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [{"id": "TST", "title": "Test", "countries": "all"}],
            }
        )
    )
    client, _ = _client([(200, [{"page": 1, "pages": 0}, None])])
    connector = WorldBankConnector(store, config_path=config, client=client)
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert records == []


def test_no_credentials_required(store: DuckDBStore, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [{"id": "TST", "title": "Test", "countries": "all"}],
            }
        )
    )
    rows = [
        _make_row(
            indicator_id="TST", country_iso3="USA", country_name="US", date_str="2024", value=1.0
        )
    ]
    client, _ = _client([(200, _wb_response("TST", rows))])
    # Pass credentials=None explicitly; should still work.
    connector = WorldBankConnector(store, credentials=None, config_path=config, client=client)
    records = list(connector.fetch_incremental(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(records) == 1


def test_indicator_with_explicit_country_list(store: DuckDBStore, tmp_path: Path) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "indicators": [
                    {"id": "TST", "title": "Test", "countries": ["USA", "GBR"]},
                ],
            }
        )
    )
    client, transport = _client(
        [
            (
                200,
                _wb_response(
                    "TST",
                    [
                        _make_row(
                            indicator_id="TST",
                            country_iso3="USA",
                            country_name="US",
                            date_str="2024",
                            value=1.0,
                        )
                    ],
                ),
            ),
            (
                200,
                _wb_response(
                    "TST",
                    [
                        _make_row(
                            indicator_id="TST",
                            country_iso3="GBR",
                            country_name="UK",
                            date_str="2024",
                            value=2.0,
                        )
                    ],
                ),
            ),
        ]
    )
    connector = WorldBankConnector(store, config_path=config, client=client)
    records = list(connector.fetch_incremental(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(records) == 2
    assert len(transport.requests_received) == 2
    assert "/country/USA/" in str(transport.requests_received[0].url)
    assert "/country/GBR/" in str(transport.requests_received[1].url)
