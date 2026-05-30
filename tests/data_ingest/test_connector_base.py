"""T-030 verification — Connector ABC + shared fetch infrastructure.

Verifies:
- ABC enforces required class attributes (source_id, title, canonical_schema,
  license).
- Subclasses with proper attributes can be constructed.
- ``run_incremental`` captures exceptions per connector without propagating;
  outcome reflects status='failed' / 'skipped' / 'ok'.
- ``CredentialMissingError`` produces status='skipped'.
- ``RateLimitedError`` produces status='failed' with the typed marker.
- Persister callable receives the normalized stream and returns the count.
- Backfill default raises NotImplementedError when backfill_supported=False.
- ``exponential_backoff_with_jitter`` produces values in the expected range.
- ``ConnectorHealth`` and ``ConnectorOutcome`` dataclasses are well-shaped.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorHealth,
    ConnectorOutcome,
    CredentialMissingError,
    License,
    RateLimitedError,
    ResumeToken,
    exponential_backoff_with_jitter,
    run_incremental,
)
from razor_rooster.data_ingest.normalization.base import (
    NormalizedRecord,
    RawRecord,
    TimeSeriesRecord,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.schemas import SchemaType


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "test.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


class _FakeOkConnector(Connector):
    source_id = "fake_ok"
    title = "Fake OK Connector"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = False
    connector_version = "fake_ok@0.1.0"

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        for i in range(3):
            yield RawRecord(
                source_id=self.source_id,
                source_record_id=f"rec-{i}",
                source_payload_json={"value": i},
                source_publication_ts=datetime(2026, 5, 1, tzinfo=UTC),
            )

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        return TimeSeriesRecord(
            source_id=raw.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=raw.source_payload_json,
            series_id="X",
            observation_ts=raw.source_publication_ts,
            value=float(raw.source_payload_json["value"]),
        )


class _FakeFailingConnector(Connector):
    source_id = "fake_failing"
    title = "Fake Failing Connector"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = False
    connector_version = "fake_failing@0.1.0"

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        raise RuntimeError("simulated upstream failure")
        yield  # pragma: no cover - unreachable

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        raise NotImplementedError


class _FakeRateLimitedConnector(Connector):
    source_id = "fake_rate_limited"
    title = "Fake Rate-Limited Connector"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = False
    connector_version = "fake_rate_limited@0.1.0"

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        raise RateLimitedError("retry budget exhausted after 5 attempts")
        yield  # pragma: no cover - unreachable

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        raise NotImplementedError


class _FakeAuthRequiredConnector(Connector):
    source_id = "fake_auth"
    title = "Fake Auth-Required Connector"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = False
    connector_version = "fake_auth@0.1.0"

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        if self.credentials is None:
            raise CredentialMissingError(f"{self.source_id} requires credentials; set the env vars")
        yield  # pragma: no cover - unreachable

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        raise NotImplementedError


class _BadConnectorMissingSourceId(Connector):
    title = "Bad"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = False
    connector_version = "bad@0.1.0"

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        return iter(())

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        raise NotImplementedError


def test_connector_constructs_with_proper_attributes(store: DuckDBStore) -> None:
    connector = _FakeOkConnector(store)
    assert connector.source_id == "fake_ok"
    assert connector.canonical_schema == SchemaType.TIME_SERIES
    assert connector.license == License.PUBLIC_DOMAIN
    assert connector.credentials is None


def test_connector_rejects_missing_class_attributes(store: DuckDBStore) -> None:
    with pytest.raises(TypeError, match="source_id"):
        _BadConnectorMissingSourceId(store)


def test_run_incremental_captures_records(store: DuckDBStore) -> None:
    connector = _FakeOkConnector(store)
    captured: list[NormalizedRecord] = []

    def persister(records: Iterator[NormalizedRecord]) -> int:
        captured.extend(records)
        return len(captured)

    outcome = run_incremental(
        connector,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        persister=persister,
    )
    assert outcome.status == "ok"
    assert outcome.records_ingested == 3
    assert outcome.duration_seconds >= 0.0
    assert outcome.errors == []
    assert len(captured) == 3
    for r in captured:
        assert isinstance(r, TimeSeriesRecord)


def test_run_incremental_isolates_connector_failure(store: DuckDBStore) -> None:
    connector = _FakeFailingConnector(store)
    outcome = run_incremental(
        connector,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        persister=lambda records: sum(1 for _ in records),
    )
    assert outcome.status == "failed"
    assert outcome.records_ingested == 0
    assert len(outcome.errors) == 1
    assert outcome.errors[0]["type"] == "RuntimeError"
    assert "simulated upstream failure" in outcome.errors[0]["message"]


def test_run_incremental_classifies_rate_limit_separately(store: DuckDBStore) -> None:
    connector = _FakeRateLimitedConnector(store)
    outcome = run_incremental(
        connector,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        persister=lambda records: sum(1 for _ in records),
    )
    assert outcome.status == "failed"
    assert len(outcome.errors) == 1
    assert outcome.errors[0]["type"] == "rate_limit_exhausted"


def test_run_incremental_classifies_missing_credentials_as_skipped(
    store: DuckDBStore,
) -> None:
    connector = _FakeAuthRequiredConnector(store, credentials=None)
    outcome = run_incremental(
        connector,
        since=datetime(2026, 1, 1, tzinfo=UTC),
        persister=lambda records: sum(1 for _ in records),
    )
    assert outcome.status == "skipped"
    assert outcome.records_ingested == 0
    assert outcome.errors[0]["type"] == "credential_missing"


def test_default_fetch_backfill_raises_for_unsupported(store: DuckDBStore) -> None:
    connector = _FakeOkConnector(store)
    assert connector.backfill_supported is False
    with pytest.raises(NotImplementedError, match="does not support backfill"):
        list(connector.fetch_backfill(until=datetime.now(tz=UTC)))


def test_default_health_check_returns_ok(store: DuckDBStore) -> None:
    connector = _FakeOkConnector(store)
    health = connector.health_check()
    assert isinstance(health, ConnectorHealth)
    assert health.source_id == "fake_ok"
    assert health.ok is True
    assert health.latency_ms == 0.0
    assert health.message is not None


def test_resume_token_is_frozen() -> None:
    token = ResumeToken(value="page-42")
    with pytest.raises((AttributeError, TypeError)):
        token.value = "page-43"  # type: ignore[misc]


def test_connector_outcome_default_construction() -> None:
    outcome = ConnectorOutcome(source_id="x", status="ok")
    assert outcome.records_ingested == 0
    assert outcome.errors == []
    assert outcome.duration_seconds == 0.0


def test_connector_health_default_construction() -> None:
    health = ConnectorHealth(source_id="x", ok=True, latency_ms=12.5)
    assert health.message is None


def test_license_enum_values_are_strings() -> None:
    assert License.PUBLIC_DOMAIN == "PUBLIC_DOMAIN"
    assert License.ACLED_TERMS_VERSIONED == "ACLED_TERMS_VERSIONED"
    assert License.POLYMARKET_TERMS_VERSIONED == "POLYMARKET_TERMS_VERSIONED"


def test_exponential_backoff_first_attempt_is_base_with_jitter() -> None:
    delays = [
        exponential_backoff_with_jitter(0, base_seconds=1.0, max_seconds=60.0, jitter=0.25)
        for _ in range(20)
    ]
    # All delays should be in [0.75, 1.25] for attempt 0.
    assert all(0.75 <= d <= 1.25 for d in delays)


def test_exponential_backoff_caps_at_max() -> None:
    delays = [
        exponential_backoff_with_jitter(20, base_seconds=1.0, max_seconds=10.0, jitter=0.0)
        for _ in range(5)
    ]
    # Without jitter, all delays at attempt 20 should equal max_seconds.
    assert all(d == 10.0 for d in delays)


def test_exponential_backoff_zero_jitter_is_deterministic() -> None:
    delay = exponential_backoff_with_jitter(2, base_seconds=1.0, max_seconds=60.0, jitter=0.0)
    assert delay == 4.0  # 1 * 2**2


def test_exponential_backoff_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        exponential_backoff_with_jitter(-1)
    with pytest.raises(ValueError):
        exponential_backoff_with_jitter(0, base_seconds=-1.0)
    with pytest.raises(ValueError):
        exponential_backoff_with_jitter(0, base_seconds=10.0, max_seconds=5.0)
    with pytest.raises(ValueError):
        exponential_backoff_with_jitter(0, jitter=1.5)
    with pytest.raises(ValueError):
        exponential_backoff_with_jitter(0, jitter=-0.1)


def test_credential_missing_error_is_connector_error() -> None:
    assert issubclass(CredentialMissingError, ConnectorError)


def test_rate_limited_error_is_connector_error() -> None:
    assert issubclass(RateLimitedError, ConnectorError)


def test_outcome_dict_serializable_via_dataclasses_asdict() -> None:
    """ConnectorOutcome is the shape that goes into the cycle JSON log."""
    from dataclasses import asdict

    outcome = ConnectorOutcome(
        source_id="fred",
        status="partial",
        records_ingested=100,
        records_skipped_duplicate=5,
        duration_seconds=12.3,
        errors=[{"type": "rate_limit", "retries": 2}],
    )
    serialized = json.dumps(asdict(outcome))
    parsed = json.loads(serialized)
    assert parsed["source_id"] == "fred"
    assert parsed["records_ingested"] == 100
    assert parsed["errors"][0]["retries"] == 2
