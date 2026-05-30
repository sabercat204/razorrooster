"""T-056 verification — USGS Mineral Commodity Summaries connector."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from razor_rooster.data_ingest.connectors.base import License, ResumeToken
from razor_rooster.data_ingest.connectors.usgs_minerals import (
    UsgsEdition,
    UsgsMineralsConnector,
    load_usgs_config,
)
from razor_rooster.data_ingest.normalization.base import RawRecord, TimeSeriesRecord
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import is_registered

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_CONFIG = _REPO_ROOT / "config" / "usgs_minerals.yaml"


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "usgs.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


_SAMPLE_CSV = (
    "commodity,production,imports,reserves\n"
    "Aluminum,860,5400,Large\n"
    "Copper,1300,950,48000\n"
    "Lithium,17,3.2,8800\n"
)


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


def test_usgs_self_registers() -> None:
    assert is_registered("usgs_minerals")


def test_class_attributes() -> None:
    assert UsgsMineralsConnector.source_id == "usgs_minerals"
    assert UsgsMineralsConnector.canonical_schema == SchemaType.TIME_SERIES
    assert UsgsMineralsConnector.license == License.PUBLIC_DOMAIN
    assert UsgsMineralsConnector.cadence_default == "annual"


def test_load_bundled_config() -> None:
    editions = load_usgs_config(_BUNDLED_CONFIG)
    assert len(editions) > 0
    assert all(isinstance(e, UsgsEdition) for e in editions)


def test_load_config_rejects_empty_metric_columns(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
version: 1
editions:
  - year: 2024
    url: "https://example.com/x.csv"
    title: "x"
    commodity_column: commodity
    metric_columns: []
"""
    )
    with pytest.raises(ValueError, match="metric_columns"):
        load_usgs_config(bad)


def _make_config(tmp_path: Path, *, year: int = 2024) -> Path:
    config = tmp_path / "usgs.yaml"
    config.write_text(
        f"""
version: 1
editions:
  - year: {year}
    url: "https://example.com/{year}.csv"
    title: "USGS {year}"
    commodity_column: commodity
    metric_columns:
      - production
      - imports
      - reserves
"""
    )
    return config


def test_fetch_incremental_yields_records(store: DuckDBStore, tmp_path: Path) -> None:
    config = _make_config(tmp_path, year=2024)
    connector = UsgsMineralsConnector(
        store, config_path=config, client=_client([(200, _SAMPLE_CSV)])
    )
    records = list(connector.fetch_incremental(since=datetime(2024, 1, 1, tzinfo=UTC)))
    # 3 commodities by 3 metrics = 9 records.
    assert len(records) == 9


def test_fetch_incremental_skips_old_editions(store: DuckDBStore, tmp_path: Path) -> None:
    config = _make_config(tmp_path, year=2024)
    connector = UsgsMineralsConnector(store, config_path=config, client=_client([]))
    # since=2025-01-01 → skip the 2024 edition (older than since).
    records = list(connector.fetch_incremental(since=datetime(2025, 1, 1, tzinfo=UTC)))
    assert records == []


def test_fetch_backfill_emits_resume_tokens(store: DuckDBStore, tmp_path: Path) -> None:
    config = _make_config(tmp_path, year=2024)
    connector = UsgsMineralsConnector(
        store, config_path=config, client=_client([(200, _SAMPLE_CSV)])
    )
    pairs = list(connector.fetch_backfill(until=datetime(2026, 5, 14, tzinfo=UTC)))
    tokens = {p[1].value for p in pairs}
    assert tokens == {"2024"}


def test_fetch_backfill_resumes_from_token(store: DuckDBStore, tmp_path: Path) -> None:
    """A token of '2024' skips the 2024 edition; only later editions run."""
    config = tmp_path / "usgs.yaml"
    config.write_text(
        """
version: 1
editions:
  - year: 2024
    url: "https://example.com/2024.csv"
    title: "USGS 2024"
    commodity_column: commodity
    metric_columns: [production]
  - year: 2025
    url: "https://example.com/2025.csv"
    title: "USGS 2025"
    commodity_column: commodity
    metric_columns: [production]
"""
    )
    connector = UsgsMineralsConnector(
        store,
        config_path=config,
        client=_client([(200, "commodity,production\nAluminum,1000\n")]),
    )
    pairs = list(
        connector.fetch_backfill(
            until=datetime(2026, 5, 14, tzinfo=UTC),
            resume_token=ResumeToken(value="2024"),
        )
    )
    assert len(pairs) == 1
    assert pairs[0][1].value == "2025"


def test_normalize_parses_year_to_january_first(store: DuckDBStore, tmp_path: Path) -> None:
    config = _make_config(tmp_path, year=2024)
    connector = UsgsMineralsConnector(store, config_path=config, client=httpx.Client())
    raw = RawRecord(
        source_id="usgs_minerals",
        source_record_id="2024:Aluminum:production",
        source_payload_json={
            "year": 2024,
            "commodity": "Aluminum",
            "metric": "production",
            "value": "860",
            "unit": None,
            "edition_title": "USGS 2024",
            "edition_url": "https://example.com/2024.csv",
        },
        source_publication_ts=datetime(2024, 1, 1, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, TimeSeriesRecord)
    assert normalized.value == 860.0
    assert normalized.observation_ts == datetime(2024, 1, 1, tzinfo=UTC)
    assert normalized.series_id == "Aluminum:production"
    assert normalized.frequency == "A"


def test_normalize_handles_non_numeric_value(store: DuckDBStore, tmp_path: Path) -> None:
    config = _make_config(tmp_path, year=2024)
    connector = UsgsMineralsConnector(store, config_path=config, client=httpx.Client())
    raw = RawRecord(
        source_id="usgs_minerals",
        source_record_id="2024:Aluminum:reserves",
        source_payload_json={
            "year": 2024,
            "commodity": "Aluminum",
            "metric": "reserves",
            "value": "Large",
            "unit": None,
            "edition_title": "USGS 2024",
            "edition_url": "https://example.com/2024.csv",
        },
        source_publication_ts=datetime(2024, 1, 1, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, TimeSeriesRecord)
    assert normalized.value is None  # non-numeric → None


def test_csv_with_missing_commodity_column_returns_no_records(
    store: DuckDBStore, tmp_path: Path
) -> None:
    config = _make_config(tmp_path, year=2024)
    connector = UsgsMineralsConnector(
        store,
        config_path=config,
        client=_client([(200, "wrong_column,production\nx,1\n")]),
    )
    records = list(connector.fetch_incremental(since=datetime(2024, 1, 1, tzinfo=UTC)))
    assert records == []
