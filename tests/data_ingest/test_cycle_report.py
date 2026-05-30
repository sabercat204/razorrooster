"""T-040 verification — cycle report writer.

Verifies:
- ``write_cycle_report`` writes a JSONL file under the cycle log directory.
- The JSONL file contains the canonical CycleSummary shape from T-021.
- A ``cycle_log`` row is inserted pointing at the file.
- The stdout summary includes connector status and stale-source warnings.
- ``run_and_report`` runs an end-to-end cycle and returns the file path.
- A failing connector's outcome is reflected in the summary.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.config.loader import (
    IngestScheduleConfig,
    SourceSchedule,
    _ScheduleDefaults,
)
from razor_rooster.data_ingest.connectors.base import (
    Connector,
    ConnectorOutcome,
    License,
)
from razor_rooster.data_ingest.cycle_report import (
    run_and_report,
    write_cycle_report,
)
from razor_rooster.data_ingest.normalization.base import (
    NormalizedRecord,
    RawRecord,
    TimeSeriesRecord,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.provenance import register_source
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import _clear_for_tests, register
from razor_rooster.data_ingest.scheduler import CycleReport


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
    db_path = tmp_path / "report.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


def _make_schedule(source_ids: list[str]) -> IngestScheduleConfig:
    return IngestScheduleConfig(
        version=1,
        defaults=_ScheduleDefaults(max_workers=1, batch_size=10_000),
        sources={
            sid: SourceSchedule(cadence="daily", freshness_threshold_seconds=172800)
            for sid in source_ids
        },
    )


def _register_synthetic_source(store: DuckDBStore, source_id: str) -> None:
    with store.connection() as conn:
        register_source(
            conn,
            source_id=source_id,
            source_type="time_series",
            cadence="daily",
            freshness_threshold_seconds=172800,
            license="PUBLIC_DOMAIN",
        )


class _OkConnector(Connector):
    title = "Synthetic OK"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = False
    connector_version = "synth_ok@0.1.0"

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        for i in range(2):
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


def test_write_cycle_report_creates_jsonl_file(store: DuckDBStore, tmp_path: Path) -> None:
    log_dir = tmp_path / "cycles"
    started = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)
    report = CycleReport(cycle_id="test-cycle-001", started_at=started)
    report.completed_at = datetime(2026, 5, 14, 8, 1, tzinfo=UTC)
    report.duration_seconds = 60.0
    report.outcomes.append(
        ConnectorOutcome(
            source_id="fred",
            status="ok",
            records_ingested=42,
            duration_seconds=2.5,
        )
    )

    captured: list[str] = []
    log_file = write_cycle_report(store, report, log_dir=log_dir, print_fn=captured.append)

    assert log_file.exists()
    assert log_file.suffix == ".jsonl"
    parsed = json.loads(log_file.read_text())
    assert parsed["cycle_id"] == "test-cycle-001"
    assert parsed["connectors"][0]["source_id"] == "fred"
    assert parsed["connectors"][0]["records_ingested"] == 42


def test_write_cycle_report_inserts_cycle_log_row(store: DuckDBStore, tmp_path: Path) -> None:
    started = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)
    report = CycleReport(cycle_id="test-cycle-002", started_at=started)
    report.completed_at = datetime(2026, 5, 14, 8, 1, tzinfo=UTC)
    report.duration_seconds = 60.0

    write_cycle_report(store, report, log_dir=tmp_path / "cycles", print_summary=False)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT cycle_id, log_path FROM cycle_log WHERE cycle_id = ?",
            ["test-cycle-002"],
        ).fetchone()
    assert row is not None
    assert row[0] == "test-cycle-002"
    assert "cycle-" in row[1]


def test_write_cycle_report_includes_skipped_as_anomalies(
    store: DuckDBStore, tmp_path: Path
) -> None:
    started = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)
    report = CycleReport(cycle_id="test-skipped", started_at=started)
    report.completed_at = started
    report.duration_seconds = 0.0
    report.skipped.append(("noaa", "no connector registered"))
    report.skipped.append(("usgs", "not due yet"))

    log_file = write_cycle_report(store, report, log_dir=tmp_path / "cycles", print_summary=False)
    parsed = json.loads(log_file.read_text())
    skip_anomalies = [a for a in parsed["anomalies_detected"] if a["type"] == "skipped"]
    assert len(skip_anomalies) == 2
    skipped_sources = {a["source_id"] for a in skip_anomalies}
    assert skipped_sources == {"noaa", "usgs"}


def test_print_summary_includes_per_connector_lines(store: DuckDBStore, tmp_path: Path) -> None:
    started = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)
    report = CycleReport(cycle_id="test-print", started_at=started)
    report.completed_at = datetime(2026, 5, 14, 8, 1, tzinfo=UTC)
    report.duration_seconds = 60.0
    report.outcomes.append(
        ConnectorOutcome(source_id="fred", status="ok", records_ingested=10, duration_seconds=1.0)
    )
    report.outcomes.append(
        ConnectorOutcome(
            source_id="noaa", status="failed", records_ingested=0, duration_seconds=2.5
        )
    )

    captured: list[str] = []
    write_cycle_report(
        store,
        report,
        log_dir=tmp_path / "cycles",
        print_summary=True,
        print_fn=captured.append,
    )

    output = "\n".join(captured)
    assert "test-print" in output
    assert "fred" in output
    assert "noaa" in output
    assert "ok" in output
    assert "failed" in output


def test_print_summary_includes_no_connectors_message(store: DuckDBStore, tmp_path: Path) -> None:
    started = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)
    report = CycleReport(cycle_id="empty-cycle", started_at=started)
    report.completed_at = started
    report.duration_seconds = 0.0

    captured: list[str] = []
    write_cycle_report(store, report, log_dir=tmp_path / "cycles", print_fn=captured.append)
    assert "no connectors ran" in "\n".join(captured)


def test_run_and_report_runs_full_cycle(store: DuckDBStore, tmp_path: Path) -> None:
    class _Synth(_OkConnector):
        source_id = "synth_e2e"

    register(_Synth)
    _register_synthetic_source(store, "synth_e2e")

    schedule = _make_schedule(["synth_e2e"])
    report, log_file = run_and_report(
        store,
        schedule=schedule,
        cycle_id="e2e-001",
        log_dir=tmp_path / "cycles",
        print_summary=True,
    )

    # Capture-via-print would be weird here since print_summary uses real print;
    # we verified the format separately. Here we just confirm the artifacts.
    assert log_file.exists()
    assert report.cycle_id == "e2e-001"
    assert len(report.outcomes) == 1
    assert report.outcomes[0].source_id == "synth_e2e"
    assert report.outcomes[0].records_ingested == 2

    parsed = json.loads(log_file.read_text())
    assert parsed["cycle_id"] == "e2e-001"
    assert len(parsed["connectors"]) == 1


def test_run_and_report_rejects_wrong_schedule_type(store: DuckDBStore, tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="IngestScheduleConfig"):
        run_and_report(
            store,
            schedule={"version": 1},  # not an IngestScheduleConfig
            cycle_id="bad",
            log_dir=tmp_path / "cycles",
        )


def test_summary_includes_stale_source_list(store: DuckDBStore, tmp_path: Path) -> None:
    """Sources that have never been fetched show up in stale_sources."""
    with store.connection() as conn:
        register_source(
            conn,
            source_id="never_fetched",
            source_type="time_series",
            cadence="daily",
            freshness_threshold_seconds=172800,
            license="PUBLIC_DOMAIN",
        )

    started = datetime(2026, 5, 14, tzinfo=UTC)
    report = CycleReport(cycle_id="stale-test", started_at=started)
    report.completed_at = started
    report.duration_seconds = 0.0

    log_file = write_cycle_report(store, report, log_dir=tmp_path / "cycles", print_summary=False)
    parsed = json.loads(log_file.read_text())
    assert "never_fetched" in parsed["stale_sources"]
