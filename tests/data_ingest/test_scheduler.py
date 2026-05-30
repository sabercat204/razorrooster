"""T-033 verification — cycle scheduler.

Verifies:
- ``evaluate_due`` flags first-run sources, fresh-enough sources skipped,
  cadence boundary respected.
- ``run_cycle`` runs registered connectors, skips schedule entries with no
  registered connector.
- Failure isolation: one connector throwing does not stop others.
- Connector outcomes are persisted, ``last_successful_fetch`` updates.
- The persister normalizes records and writes them via the staging-merge
  pattern; round-trips show inserted rows.
- The ``only`` filter restricts to a subset of sources.
- Single-threaded fast path runs when ``max_workers == 1``.
- Multi-threaded path runs with ``max_workers > 1``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
from razor_rooster.data_ingest.normalization.base import (
    NormalizedRecord,
    RawRecord,
    TimeSeriesRecord,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.provenance import (
    register_source,
)
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import (
    _clear_for_tests,
    register,
)
from razor_rooster.data_ingest.scheduler import (
    DueDecision,
    evaluate_due,
    fetch_last_successful_lookup,
    run_cycle,
)


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
    db_path = tmp_path / "scheduler.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


def _make_schedule(
    sources: dict[str, SourceSchedule],
    *,
    max_workers: int = 4,
    batch_size: int = 10_000,
) -> IngestScheduleConfig:
    return IngestScheduleConfig(
        version=1,
        defaults=_ScheduleDefaults(max_workers=max_workers, batch_size=batch_size),
        sources=sources,
    )


def _basic_schedule(source_ids: list[str]) -> IngestScheduleConfig:
    return _make_schedule(
        {
            sid: SourceSchedule(cadence="daily", freshness_threshold_seconds=172800)
            for sid in source_ids
        }
    )


def _register_synthetic_source(
    store: DuckDBStore, *, source_id: str, license_value: str = "PUBLIC_DOMAIN"
) -> None:
    with store.connection() as conn:
        register_source(
            conn,
            source_id=source_id,
            source_type="time_series",
            cadence="daily",
            freshness_threshold_seconds=172800,
            license=license_value,
        )


# --- Connector classes used as fixtures ------------------------------------


class _AlwaysOk(Connector):
    """Synthetic connector that yields three time-series records."""

    title = "Always OK"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = False
    connector_version = "always_ok@0.1.0"

    def __init__(self, store: DuckDBStore, *, credentials: object | None = None) -> None:
        super().__init__(store, credentials=credentials)  # type: ignore[arg-type]
        self.calls = 0

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        self.calls += 1
        for i in range(3):
            yield RawRecord(
                source_id=self.source_id,
                source_record_id=f"rec-{i}",
                source_payload_json={"value": float(i)},
                source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
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


class _AlwaysFails(Connector):
    title = "Always Fails"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = False
    connector_version = "always_fails@0.1.0"

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        raise RuntimeError("simulated failure")
        yield  # pragma: no cover

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        raise NotImplementedError


# --- evaluate_due ----------------------------------------------------------


def test_evaluate_due_first_run_is_due() -> None:
    schedule = _basic_schedule(["fred"])
    decisions = evaluate_due(schedule, last_fetch_lookup={"fred": None})
    assert len(decisions) == 1
    assert decisions[0].is_due is True
    assert "first run" in decisions[0].reason


def test_evaluate_due_recent_fetch_not_due() -> None:
    schedule = _basic_schedule(["fred"])
    last = datetime.now(tz=UTC) - timedelta(hours=1)  # 1 hour ago
    decisions = evaluate_due(schedule, last_fetch_lookup={"fred": last})
    assert decisions[0].is_due is False
    assert "not due" in decisions[0].reason


def test_evaluate_due_old_fetch_is_due() -> None:
    schedule = _basic_schedule(["fred"])
    last = datetime.now(tz=UTC) - timedelta(days=2)
    decisions = evaluate_due(schedule, last_fetch_lookup={"fred": last})
    assert decisions[0].is_due is True


def test_evaluate_due_only_filter() -> None:
    schedule = _basic_schedule(["fred", "acled", "noaa"])
    decisions = evaluate_due(
        schedule,
        last_fetch_lookup={"fred": None, "acled": None, "noaa": None},
        only=["fred"],
    )
    source_ids = [d.source_id for d in decisions]
    assert source_ids == ["fred"]


def test_evaluate_due_returns_decision_per_source() -> None:
    schedule = _basic_schedule(["a", "b", "c"])
    decisions = evaluate_due(
        schedule,
        last_fetch_lookup={"a": None, "b": None, "c": None},
    )
    assert len(decisions) == 3
    assert all(d.is_due for d in decisions)


def test_evaluate_due_handles_weekly_cadence() -> None:
    schedule = _make_schedule(
        {
            "weekly_src": SourceSchedule(
                cadence="weekly",
                day_of_week="monday",
                freshness_threshold_seconds=1209600,
            )
        }
    )
    last = datetime.now(tz=UTC) - timedelta(days=8)
    decisions = evaluate_due(schedule, last_fetch_lookup={"weekly_src": last})
    assert decisions[0].is_due is True

    last_recent = datetime.now(tz=UTC) - timedelta(days=3)
    decisions_recent = evaluate_due(schedule, last_fetch_lookup={"weekly_src": last_recent})
    assert decisions_recent[0].is_due is False


def test_evaluate_due_decision_is_immutable() -> None:
    decision = DueDecision(
        source_id="x",
        is_due=True,
        reason="first run",
        last_successful_fetch=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        decision.is_due = False  # type: ignore[misc]


# --- run_cycle: positive cases ---------------------------------------------


def test_run_cycle_runs_registered_connector(store: DuckDBStore) -> None:
    class _SyntheticOk(_AlwaysOk):
        source_id = "synth_ok"

    register(_SyntheticOk)
    _register_synthetic_source(store, source_id="synth_ok")

    schedule = _basic_schedule(["synth_ok"])
    report = run_cycle(store, schedule, cycle_id="test_run_simple")

    assert len(report.outcomes) == 1
    assert report.outcomes[0].source_id == "synth_ok"
    assert report.outcomes[0].status == "ok"
    assert report.outcomes[0].records_ingested == 3
    assert report.duration_seconds is not None and report.duration_seconds >= 0


def test_run_cycle_persists_records(store: DuckDBStore) -> None:
    class _SyntheticOk(_AlwaysOk):
        source_id = "synth_ok"

    register(_SyntheticOk)
    _register_synthetic_source(store, source_id="synth_ok")

    schedule = _basic_schedule(["synth_ok"])
    run_cycle(store, schedule, cycle_id="test_persist")

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT source_id, COUNT(*) FROM time_series WHERE source_id = ? GROUP BY source_id",
            ["synth_ok"],
        ).fetchall()
    assert rows == [("synth_ok", 3)]


def test_run_cycle_updates_last_successful_fetch(store: DuckDBStore) -> None:
    class _SyntheticOk(_AlwaysOk):
        source_id = "synth_ok"

    register(_SyntheticOk)
    _register_synthetic_source(store, source_id="synth_ok")

    schedule = _basic_schedule(["synth_ok"])
    run_cycle(store, schedule, cycle_id="test_update_fetch")

    with store.connection() as conn:
        row = conn.execute(
            "SELECT last_successful_fetch FROM sources WHERE source_id = 'synth_ok'"
        ).fetchone()
    assert row is not None
    assert row[0] is not None


# --- run_cycle: failure isolation -------------------------------------------


def test_run_cycle_isolates_per_connector_failure(store: DuckDBStore) -> None:
    class _Ok(_AlwaysOk):
        source_id = "ok"

    class _Fails(_AlwaysFails):
        source_id = "fails"

    register(_Ok)
    register(_Fails)
    _register_synthetic_source(store, source_id="ok")
    _register_synthetic_source(store, source_id="fails")

    schedule = _basic_schedule(["ok", "fails"])
    report = run_cycle(store, schedule, cycle_id="test_failure")

    by_source = {o.source_id: o for o in report.outcomes}
    assert by_source["ok"].status == "ok"
    assert by_source["fails"].status == "failed"
    assert len(by_source["fails"].errors) == 1


def test_run_cycle_records_failure_in_sources(store: DuckDBStore) -> None:
    class _Fails(_AlwaysFails):
        source_id = "fails"

    register(_Fails)
    _register_synthetic_source(store, source_id="fails")

    schedule = _basic_schedule(["fails"])
    run_cycle(store, schedule, cycle_id="test_failure_record")

    with store.connection() as conn:
        row = conn.execute(
            "SELECT last_failed_fetch, last_failure_summary FROM sources WHERE source_id = 'fails'"
        ).fetchone()
    assert row is not None
    assert row[0] is not None
    assert row[1] is not None
    assert "simulated failure" in str(row[1])


# --- run_cycle: skipping ---------------------------------------------------


def test_run_cycle_skips_unregistered_source(store: DuckDBStore) -> None:
    schedule = _basic_schedule(["unknown"])
    report = run_cycle(store, schedule, cycle_id="test_unregistered")
    assert report.outcomes == []
    assert ("unknown", "no connector registered for this source_id") in report.skipped


def test_run_cycle_skips_not_due_sources(store: DuckDBStore) -> None:
    class _Ok(_AlwaysOk):
        source_id = "fresh"

    register(_Ok)
    _register_synthetic_source(store, source_id="fresh")

    # Manually set last_successful_fetch to recent so the source is not due.
    with store.connection() as conn:
        conn.execute(
            "UPDATE sources SET last_successful_fetch = ? WHERE source_id = 'fresh'",
            [datetime.now(tz=UTC) - timedelta(minutes=10)],
        )

    schedule = _basic_schedule(["fresh"])
    report = run_cycle(store, schedule, cycle_id="test_not_due")
    assert report.outcomes == []
    assert any(sid == "fresh" for sid, _ in report.skipped)


# --- run_cycle: filtering --------------------------------------------------


def test_run_cycle_only_filter(store: DuckDBStore) -> None:
    class _Ok1(_AlwaysOk):
        source_id = "ok1"

    class _Ok2(_AlwaysOk):
        source_id = "ok2"

    register(_Ok1)
    register(_Ok2)
    _register_synthetic_source(store, source_id="ok1")
    _register_synthetic_source(store, source_id="ok2")

    schedule = _basic_schedule(["ok1", "ok2"])
    report = run_cycle(store, schedule, cycle_id="test_only", only=["ok1"])

    source_ids = [o.source_id for o in report.outcomes]
    assert source_ids == ["ok1"]


# --- run_cycle: concurrency paths ------------------------------------------


def test_run_cycle_single_threaded_fast_path(store: DuckDBStore) -> None:
    """When max_workers <= 1, the scheduler skips the ThreadPoolExecutor."""

    class _Ok(_AlwaysOk):
        source_id = "single_thread_ok"

    register(_Ok)
    _register_synthetic_source(store, source_id="single_thread_ok")

    schedule = _make_schedule(
        {"single_thread_ok": SourceSchedule(cadence="daily", freshness_threshold_seconds=172800)},
        max_workers=1,
    )
    report = run_cycle(store, schedule, cycle_id="test_single")
    assert len(report.outcomes) == 1
    assert report.outcomes[0].status == "ok"


def test_run_cycle_multi_threaded_completes_all(store: DuckDBStore) -> None:
    """A four-source cycle on max_workers=4 completes all four."""
    sources = []
    for i in range(4):

        class _Ok(_AlwaysOk):
            source_id = f"mt_{i}"

        _Ok.source_id = f"mt_{i}"
        register(_Ok)
        sources.append(f"mt_{i}")
        _register_synthetic_source(store, source_id=f"mt_{i}")

    schedule = _basic_schedule(sources)
    report = run_cycle(store, schedule, cycle_id="test_mt")
    assert len(report.outcomes) == 4
    assert all(o.status == "ok" for o in report.outcomes)


# --- helpers ---------------------------------------------------------------


def test_fetch_last_successful_lookup_reads_sources(store: DuckDBStore) -> None:
    _register_synthetic_source(store, source_id="src_a")
    _register_synthetic_source(store, source_id="src_b")

    with store.connection() as conn:
        conn.execute(
            "UPDATE sources SET last_successful_fetch = ? WHERE source_id = 'src_a'",
            [datetime(2026, 5, 14, tzinfo=UTC)],
        )
    lookup = fetch_last_successful_lookup(store)
    assert lookup["src_a"] == datetime(2026, 5, 14, tzinfo=UTC)
    assert lookup["src_b"] is None
