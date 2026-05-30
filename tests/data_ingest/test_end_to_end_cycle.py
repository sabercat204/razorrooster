"""T-070 — end-to-end cycle integration test.

Exercises the whole data_ingest pipeline against an in-memory cohort of
synthetic connectors that stand in for the 12 v1 sources. Mocks at the
connector seam (each fakes its own ``fetch_incremental`` / ``normalize``)
so the scheduler, persister, staging-merge, provenance helpers, and
cycle-report writer are all real code under test.

Scenarios verified (one per acceptance criterion in DATA_INGEST.md §7):

- happy path: every connector succeeds, records persist, summary is
  well-formed, ``cycle_log`` row inserted.
- failure isolation (REQ-SRC-004): one connector raises; the cycle
  completes; other connectors still ingest; the failing one's outcome
  is ``failed`` with structured error info.
- idempotency (REQ-PERSIST-003): re-running the same cycle inserts
  zero new rows.
- source revision (REQ-PERSIST-004): when a connector's payload
  changes for an existing record between cycles, the prior row is
  marked ``superseded_at`` and a fresh row is inserted.

Mock connectors cover all four canonical schemas:
- time_series:        fred, worldbank, eia, noaa, usgs, bdi
- event_stream:       gdelt, who_don, acled
- document_docket:    federal_register, nrc_adams, regulations_gov
- geospatial:         (covered by noaa's optional second-schema output
                       in real life; for v1 the 12 v1 sources keep one
                       canonical schema each, so the test does the same)

The mocks are intentionally thin — the per-connector unit tests
(test_*_connector.py) cover normalization and edge cases. T-070 is
about composition.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.config.loader import (
    IngestScheduleConfig,
    SourceSchedule,
    _ScheduleDefaults,
)
from razor_rooster.data_ingest.connectors.base import (
    Connector,
    License,
)
from razor_rooster.data_ingest.cycle_report import run_and_report
from razor_rooster.data_ingest.normalization.base import (
    DocumentDocketRecord,
    EventStreamRecord,
    NormalizedRecord,
    RawRecord,
    TimeSeriesRecord,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.provenance import register_source
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import _clear_for_tests, register


# ---------------------------------------------------------------------------
# Fixture: registry isolation. Phase-6 connector imports register at import
# time; the integration test wants only its own synthetic registrations
# active during the cycle, then must restore the real ones for the rest of
# the suite.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolate_registry() -> Iterator[None]:
    from razor_rooster.data_ingest.registry import _REGISTRY

    snapshot = dict(_REGISTRY)
    _clear_for_tests()
    try:
        yield
    finally:
        _clear_for_tests()
        _REGISTRY.update(snapshot)


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db = tmp_path / "e2e.duckdb"
    s = DuckDBStore(db)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


# ---------------------------------------------------------------------------
# Synthetic connector base. Each subclass overrides class attributes
# (source_id, canonical_schema, license, fixed_record_ids) and the
# _build_payload helper to exercise its target schema.
# ---------------------------------------------------------------------------
class _SyntheticConnector(Connector):
    """Tiny mock connector used by the integration test only."""

    cadence_default = "daily"
    backfill_supported = False
    connector_version = "synthetic@0.1.0"

    # The integration test mutates these via a class-attribute monkeypatch
    # to drive the source-revision scenario. None means "use the default
    # value derived from source_record_id".
    payload_override: dict[str, object] | None = None

    # Whether to raise mid-fetch. The failure-isolation scenario flips this
    # on for one connector.
    raise_on_fetch: bool = False

    fixed_record_ids: tuple[str, ...] = ("rec-1", "rec-2")

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        if self.raise_on_fetch:
            raise RuntimeError("synthetic-connector-failure")

        publication = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
        for rid in self.fixed_record_ids:
            payload = self._build_payload(rid)
            if self.payload_override is not None:
                payload = {**payload, **self.payload_override}
            yield RawRecord(
                source_id=self.source_id,
                source_record_id=rid,
                source_payload_json=payload,
                source_publication_ts=publication,
            )

    # Subclasses override: build the payload dict for a given record id.
    def _build_payload(self, rid: str) -> dict[str, object]:
        return {"value": 1.0, "rid": rid}

    # Subclasses override: produce the schema-specific NormalizedRecord.
    def normalize(self, raw: RawRecord) -> NormalizedRecord:  # pragma: no cover
        raise NotImplementedError


class _TimeSeriesMock(_SyntheticConnector):
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        value = float(raw.source_payload_json.get("value", 0.0))  # type: ignore[arg-type]
        return TimeSeriesRecord(
            source_id=raw.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=raw.source_payload_json,
            series_id=str(raw.source_payload_json.get("series_id", "X")),
            observation_ts=raw.source_publication_ts,
            value=value,
            unit="USD",
            frequency="daily",
        )


class _EventStreamMock(_SyntheticConnector):
    canonical_schema = SchemaType.EVENT_STREAM
    license = License.PUBLIC_DOMAIN

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        return EventStreamRecord(
            source_id=raw.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=raw.source_payload_json,
            event_ts=raw.source_publication_ts,
            country_iso3=str(raw.source_payload_json.get("country_iso3", "USA")),
            event_class=str(raw.source_payload_json.get("event_class", "test_event")),
            description=str(raw.source_payload_json.get("description", "synthetic event")),
        )


class _DocumentDocketMock(_SyntheticConnector):
    canonical_schema = SchemaType.DOCUMENT_DOCKET
    license = License.PUBLIC_DOMAIN

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        return DocumentDocketRecord(
            source_id=raw.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=raw.source_payload_json,
            title=str(raw.source_payload_json.get("title", "synthetic doc")),
            document_type="rule",
            agency=str(raw.source_payload_json.get("agency", "EPA")),
            published_date=date(2026, 5, 14),
            full_text_uri="https://example.test/" + raw.source_record_id,
        )


# Per-source mocks. One concrete class per v1 source so the registry holds
# distinct entries and the report's connectors[] list looks like a real
# cycle.
class _FredMock(_TimeSeriesMock):
    source_id = "fred"
    title = "FRED (mock)"


class _WorldbankMock(_TimeSeriesMock):
    source_id = "worldbank"
    title = "World Bank (mock)"


class _EiaMock(_TimeSeriesMock):
    source_id = "eia"
    title = "EIA (mock)"


class _NoaaMock(_TimeSeriesMock):
    source_id = "noaa"
    title = "NOAA CDO (mock)"


class _UsgsMock(_TimeSeriesMock):
    source_id = "usgs"
    title = "USGS Minerals (mock)"


class _BdiMock(_TimeSeriesMock):
    source_id = "bdi_proxy"
    title = "BDI proxy via FRED (mock)"


class _GdeltMock(_EventStreamMock):
    source_id = "gdelt"
    title = "GDELT events (mock)"


class _WhoDonMock(_EventStreamMock):
    source_id = "who_don"
    title = "WHO DON (mock)"


class _AcledMock(_EventStreamMock):
    source_id = "acled"
    title = "ACLED (mock)"


class _FederalRegisterMock(_DocumentDocketMock):
    source_id = "federal_register"
    title = "Federal Register (mock)"


class _NrcAdamsMock(_DocumentDocketMock):
    source_id = "nrc_adams"
    title = "NRC ADAMS (mock)"


class _RegulationsGovMock(_DocumentDocketMock):
    source_id = "regulations_gov"
    title = "regulations.gov (mock)"


_ALL_MOCKS: tuple[type[_SyntheticConnector], ...] = (
    _FredMock,
    _WorldbankMock,
    _EiaMock,
    _NoaaMock,
    _UsgsMock,
    _BdiMock,
    _GdeltMock,
    _WhoDonMock,
    _AcledMock,
    _FederalRegisterMock,
    _NrcAdamsMock,
    _RegulationsGovMock,
)


def _make_full_schedule() -> IngestScheduleConfig:
    """One source schedule entry per v1 mock."""
    return IngestScheduleConfig(
        version=1,
        defaults=_ScheduleDefaults(max_workers=2, batch_size=10_000),
        sources={
            cls.source_id: SourceSchedule(
                cadence="daily",
                freshness_threshold_seconds=172_800,
            )
            for cls in _ALL_MOCKS
        },
    )


def _register_all_sources(store: DuckDBStore) -> None:
    """Register all 12 mocks in the source registry and the sources table."""
    for cls in _ALL_MOCKS:
        register(cls)
    with store.connection() as conn:
        for cls in _ALL_MOCKS:
            schema_str = (
                "time_series"
                if cls.canonical_schema == SchemaType.TIME_SERIES
                else "event_stream"
                if cls.canonical_schema == SchemaType.EVENT_STREAM
                else "document_docket"
            )
            register_source(
                conn,
                source_id=cls.source_id,
                source_type=schema_str,
                cadence="daily",
                freshness_threshold_seconds=172_800,
                license="PUBLIC_DOMAIN",
            )


# Per canonical schema, list the table to query for row counts.
_SCHEMA_TABLE = {
    SchemaType.TIME_SERIES: "time_series",
    SchemaType.EVENT_STREAM: "event_stream",
    SchemaType.DOCUMENT_DOCKET: "document_docket",
}


def _row_count(store: DuckDBStore, table: str, source_id: str | None = None) -> int:
    with store.connection() as conn:
        if source_id is None:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        else:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE source_id = ?", [source_id]
            ).fetchone()
    return int(row[0]) if row is not None else 0


def _active_row_count(store: DuckDBStore, table: str, source_id: str) -> int:
    with store.connection() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE source_id = ? AND superseded_at IS NULL",
            [source_id],
        ).fetchone()
    return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_e2e_happy_path_all_12_connectors(store: DuckDBStore, tmp_path: Path) -> None:
    """Full cycle with every v1 connector succeeding."""
    _register_all_sources(store)
    schedule = _make_full_schedule()

    report, log_file = run_and_report(
        store,
        schedule=schedule,
        cycle_id="e2e-happy",
        log_dir=tmp_path / "cycles",
        print_summary=False,
    )

    # Every mock ran and produced 2 records.
    assert len(report.outcomes) == len(_ALL_MOCKS), report.outcomes
    assert {o.source_id for o in report.outcomes} == {cls.source_id for cls in _ALL_MOCKS}
    for outcome in report.outcomes:
        assert outcome.status == "ok", outcome
        assert outcome.records_ingested == 2, outcome

    # Persisted row counts per canonical schema add up.
    time_series_sources = [m for m in _ALL_MOCKS if m.canonical_schema == SchemaType.TIME_SERIES]
    event_stream_sources = [m for m in _ALL_MOCKS if m.canonical_schema == SchemaType.EVENT_STREAM]
    document_docket_sources = [
        m for m in _ALL_MOCKS if m.canonical_schema == SchemaType.DOCUMENT_DOCKET
    ]

    assert _row_count(store, "time_series") == len(time_series_sources) * 2
    assert _row_count(store, "event_stream") == len(event_stream_sources) * 2
    assert _row_count(store, "document_docket") == len(document_docket_sources) * 2

    # Log file is well-formed JSONL.
    assert log_file.exists()
    lines = [ln for ln in log_file.read_text().splitlines() if ln.strip()]
    assert lines, "log file should not be empty"


def test_e2e_failure_isolation_one_connector_raises(store: DuckDBStore, tmp_path: Path) -> None:
    """REQ-SRC-004 — one connector failing does not stop the others."""
    _register_all_sources(store)

    # Flip one mock to raise. Use a class-attribute mutation so the scheduler's
    # instance-creation path picks it up automatically.
    _GdeltMock.raise_on_fetch = True
    try:
        schedule = _make_full_schedule()
        report, _ = run_and_report(
            store,
            schedule=schedule,
            cycle_id="e2e-failure",
            log_dir=tmp_path / "cycles",
            print_summary=False,
        )
    finally:
        _GdeltMock.raise_on_fetch = False

    # All connectors got an outcome; gdelt failed; the rest succeeded.
    by_id = {o.source_id: o for o in report.outcomes}
    assert by_id["gdelt"].status == "failed"
    assert by_id["gdelt"].records_ingested == 0
    assert any("synthetic-connector-failure" in str(e) for e in by_id["gdelt"].errors)

    for sid, outcome in by_id.items():
        if sid == "gdelt":
            continue
        assert outcome.status == "ok", (sid, outcome)
        assert outcome.records_ingested == 2, (sid, outcome)

    # gdelt persisted nothing; the others persisted the expected amount.
    assert _row_count(store, "event_stream", "gdelt") == 0
    assert _row_count(store, "event_stream", "who_don") == 2
    assert _row_count(store, "event_stream", "acled") == 2


def test_e2e_idempotency_two_cycles_no_duplicates(store: DuckDBStore, tmp_path: Path) -> None:
    """REQ-PERSIST-003 — running the same cycle twice doesn't duplicate rows."""
    _register_all_sources(store)
    schedule = _make_full_schedule()

    cycle_1_at = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)
    cycle_2_at = datetime(2026, 5, 15, 8, 0, tzinfo=UTC)  # 24h later so cadence is satisfied

    run_and_report(
        store,
        schedule=schedule,
        cycle_id="e2e-idem-1",
        log_dir=tmp_path / "cycles",
        print_summary=False,
        now=cycle_1_at,
    )
    after_first_ts = _row_count(store, "time_series")
    after_first_es = _row_count(store, "event_stream")
    after_first_dd = _row_count(store, "document_docket")

    run_and_report(
        store,
        schedule=schedule,
        cycle_id="e2e-idem-2",
        log_dir=tmp_path / "cycles",
        print_summary=False,
        now=cycle_2_at,
    )
    after_second_ts = _row_count(store, "time_series")
    after_second_es = _row_count(store, "event_stream")
    after_second_dd = _row_count(store, "document_docket")

    assert after_first_ts == after_second_ts, "time_series duplicated on second cycle"
    assert after_first_es == after_second_es, "event_stream duplicated on second cycle"
    assert after_first_dd == after_second_dd, "document_docket duplicated on second cycle"


def test_e2e_source_revision_supersedes_prior_row(store: DuckDBStore, tmp_path: Path) -> None:
    """REQ-PERSIST-004 — when a source's payload changes between cycles,
    the prior row is superseded and a new active row is inserted.
    """
    _register_all_sources(store)
    schedule = _make_full_schedule()

    cycle_1_at = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)
    cycle_2_at = datetime(2026, 5, 15, 8, 0, tzinfo=UTC)  # 24h later

    # Cycle 1: default payload.
    run_and_report(
        store,
        schedule=schedule,
        cycle_id="e2e-rev-1",
        log_dir=tmp_path / "cycles",
        print_summary=False,
        now=cycle_1_at,
    )

    # Confirm fred has 2 active rows after cycle 1.
    assert _active_row_count(store, "time_series", "fred") == 2

    # Cycle 2: change fred's payload for the same record ids.
    _FredMock.payload_override = {"value": 999.0, "revised": True}
    try:
        run_and_report(
            store,
            schedule=schedule,
            cycle_id="e2e-rev-2",
            log_dir=tmp_path / "cycles",
            print_summary=False,
            now=cycle_2_at,
        )
    finally:
        _FredMock.payload_override = None

    # Two new active rows for fred; two prior rows superseded; total physical rows = 4.
    assert _active_row_count(store, "time_series", "fred") == 2
    assert _row_count(store, "time_series", "fred") == 4

    # Confirm the revised value is live.
    with store.connection() as conn:
        row = conn.execute(
            "SELECT value FROM time_series WHERE source_id = 'fred' AND superseded_at IS NULL "
            "ORDER BY source_record_id LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] == 999.0

    # Other sources unchanged: still 2 active rows each, no supersession.
    for cls in _ALL_MOCKS:
        if cls.source_id == "fred":
            continue
        if cls.canonical_schema not in _SCHEMA_TABLE:
            continue
        table = _SCHEMA_TABLE[cls.canonical_schema]
        assert _active_row_count(store, table, cls.source_id) == 2, cls.source_id
        assert _row_count(store, table, cls.source_id) == 2, cls.source_id


def test_e2e_cycle_log_records_each_run(store: DuckDBStore, tmp_path: Path) -> None:
    """A cycle_log row is inserted per cycle and points at the JSONL file."""
    _register_all_sources(store)
    schedule = _make_full_schedule()

    cycle_1_at = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)
    cycle_2_at = datetime(2026, 5, 15, 8, 0, tzinfo=UTC)

    run_and_report(
        store,
        schedule=schedule,
        cycle_id="e2e-log-1",
        log_dir=tmp_path / "cycles",
        print_summary=False,
        now=cycle_1_at,
    )
    run_and_report(
        store,
        schedule=schedule,
        cycle_id="e2e-log-2",
        log_dir=tmp_path / "cycles",
        print_summary=False,
        now=cycle_2_at,
    )

    with store.connection() as conn:
        rows = conn.execute("SELECT cycle_id, log_path FROM cycle_log ORDER BY cycle_id").fetchall()
    cycle_ids = [r[0] for r in rows]
    assert "e2e-log-1" in cycle_ids
    assert "e2e-log-2" in cycle_ids
    for _cid, log_path in rows:
        assert Path(log_path).exists()


# ---------------------------------------------------------------------------
# Defensive: confirm the mock cohort actually covers all 12 v1 sources, so
# refactors that rename a source surface here rather than silently passing.
# ---------------------------------------------------------------------------
_EXPECTED_V1_SOURCE_IDS = {
    "fred",
    "worldbank",
    "eia",
    "noaa",
    "usgs",
    "bdi_proxy",
    "gdelt",
    "who_don",
    "acled",
    "federal_register",
    "nrc_adams",
    "regulations_gov",
}


def test_mock_cohort_covers_all_v1_sources() -> None:
    cohort_ids = {cls.source_id for cls in _ALL_MOCKS}
    assert cohort_ids == _EXPECTED_V1_SOURCE_IDS


# Belt-and-braces: the schedule replace helper exists in case a future
# scenario needs to alter one entry. Not used by the current scenarios but
# included so the import stays consistent for follow-up tests.
def _replace_source_schedule(
    schedule: IngestScheduleConfig, source_id: str, **kwargs: object
) -> IngestScheduleConfig:
    new_sources = dict(schedule.sources)
    new_sources[source_id] = replace(schedule.sources[source_id], **kwargs)  # type: ignore[arg-type]
    return replace(schedule, sources=new_sources)
