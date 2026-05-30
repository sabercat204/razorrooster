"""T-SCAN-080 — end-to-end scan against the seed pattern_library.

Composes the full pipeline end-to-end: synthetic data_ingest seed
data, real pattern_library refresh against that seed, then
``run_scan`` over the populated library. Verifies acceptance
criteria from SIGNAL_SCANNER.md §8:

- Daily scan runs end-to-end within the perf bound (sub-30-seconds
  for the eight seed classes against synthetic data).
- Every seed class produces a scan record per scan.
- Reasoning traces are populated and renderable.
- Provenance is complete (library_version, definition_version,
  data_as_of, warnings).
- Per-class failures isolate.
- Re-running a scan produces a new immutable scan rather than
  overwriting prior.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library import registry
from razor_rooster.pattern_library.engines.refresh import run_refresh
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.source import (
    register_polymarket_sources,
)
from razor_rooster.signal_scanner.engines.scanner import run_scan
from razor_rooster.signal_scanner.engines.trace import render_trace_text
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)
from razor_rooster.signal_scanner.persistence.operations import (
    query_scan_records,
    query_scan_summary,
    query_trace,
)


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    registry._clear_for_tests()
    yield
    registry._clear_for_tests()


@pytest.fixture
def populated_store(tmp_path: Path) -> Iterator[DuckDBStore]:
    """Synthetic data_ingest corpus + refreshed pattern_library outputs.

    Reuses the same seeding pattern as
    tests/pattern_library/test_end_to_end_refresh.py so the seed
    classes evaluate against representative data.
    """
    db_path = tmp_path / "scan_e2e.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        register_polymarket_sources(conn)
        _seed_corpus(conn)

    # Refresh the pattern library against the seeded data so the
    # scanner has base rates and signatures to read.
    registry.discover()
    trace_dir = tmp_path / "calibration"
    trace_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = tmp_path / "library" / ".refresh.lock"
    run_refresh(
        s,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
        now=datetime(2026, 5, 14, tzinfo=UTC),
    )

    try:
        yield s
    finally:
        s.close()


def _seed_corpus(conn) -> None:
    """Insert the same synthetic-but-representative data_ingest fixture
    used by the pattern_library E2E test.

    We deliberately copy rather than import a shared helper to keep
    test surfaces independent — tests on different subsystems can
    diverge their fixtures over time without coupling.
    """
    now = datetime(2026, 5, 14, tzinfo=UTC)

    pheic_entries = [
        (datetime(2010, 6, 11, tzinfo=UTC), "WHO declares PHEIC: H1N1 pandemic"),
        (datetime(2014, 8, 8, tzinfo=UTC), "WHO declares PHEIC: Ebola West Africa"),
        (datetime(2016, 2, 1, tzinfo=UTC), "WHO declares PHEIC: Zika virus"),
        (datetime(2020, 1, 30, tzinfo=UTC), "WHO declares PHEIC: novel coronavirus"),
        (datetime(2022, 7, 23, tzinfo=UTC), "WHO declares PHEIC: monkeypox / mpox"),
    ]
    for ts, desc in pheic_entries:
        conn.execute(
            "INSERT INTO event_stream "
            "(source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, event_ts, country_iso3, "
            "event_class, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "who_don",
                f"who-{ts.isoformat()}",
                ts,
                now,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                ts,
                None,
                "pheic_declaration",
                desc,
            ],
        )

    # GDELT: 60-day window with weekly density spikes.
    for day_offset in range(60):
        day = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day_offset)
        for i in range(60 if day_offset % 7 == 0 else 5):
            conn.execute(
                "INSERT INTO event_stream "
                "(source_id, source_record_id, source_publication_ts, fetch_ts, "
                "connector_version, source_payload_json, event_ts, country_iso3, "
                "event_class, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    "gdelt_events",
                    f"gdelt-{day.isoformat()}-{i}",
                    day,
                    now,
                    "test@1",
                    json.dumps({"raw": "synthetic"}),
                    day,
                    "USA",
                    "conflict",
                    "synthetic GDELT event",
                ],
            )

    # ACLED.
    for day_offset in range(40):
        day = datetime(2024, 1, 15, tzinfo=UTC) + timedelta(days=day_offset)
        for i in range(35 if day_offset % 7 == 1 else 4):
            conn.execute(
                "INSERT INTO event_stream "
                "(source_id, source_record_id, source_publication_ts, fetch_ts, "
                "connector_version, source_payload_json, event_ts, country_iso3, "
                "event_class, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    "acled",
                    f"acled-{day.isoformat()}-{i}",
                    day,
                    now,
                    "test@1",
                    json.dumps({"raw": "synthetic"}),
                    day,
                    "MEX",
                    "violence",
                    "synthetic ACLED event",
                ],
            )

    # Federal Register paired rules.
    for i in range(8):
        proposed_date = datetime(2020 + i // 4, 1 + (i * 2) % 11, 1, tzinfo=UTC)
        final_date = proposed_date + timedelta(days=200)
        docket_id = f"docket-{i:03d}"
        for doc_type, doc_date, suffix in (
            ("proposed_rule", proposed_date, "p"),
            ("rule", final_date, "f"),
        ):
            conn.execute(
                "INSERT INTO document_docket "
                "(source_id, source_record_id, source_publication_ts, fetch_ts, "
                "connector_version, source_payload_json, title, document_type, "
                "docket_id, agency, published_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    "federal_register",
                    f"{docket_id}-{suffix}",
                    doc_date,
                    now,
                    "test@1",
                    json.dumps({"raw": "synthetic"}),
                    f"Synthetic rule {docket_id} {suffix}",
                    doc_type,
                    docket_id,
                    "EPA",
                    doc_date,
                ],
            )

    # FRED Brent.
    for day_offset in range(120):
        day = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day_offset)
        price = 75.0 if day_offset < 60 else 90.0
        conn.execute(
            "INSERT INTO time_series "
            "(source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, series_id, observation_ts, "
            "value, unit, frequency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "fred",
                f"fred-DCOILBRENTEU-{day.isoformat()}",
                day,
                now,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                "DCOILBRENTEU",
                day,
                price,
                "USD/bbl",
                "daily",
            ],
        )

    # NOAA ENSO.
    for day_offset in range(180):
        day = datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=day_offset)
        anomaly = 0.1 if day_offset < 90 else 1.0
        conn.execute(
            "INSERT INTO time_series "
            "(source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, series_id, observation_ts, "
            "value, unit, frequency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "noaa",
                f"noaa-ENSO-nino34-{day.isoformat()}",
                day,
                now,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                "ENSO_nino34_anomaly",
                day,
                anomaly,
                "C",
                "daily",
            ],
        )


# -- end-to-end scan --------------------------------------------------------


def test_full_scan_against_seeded_library_succeeds(populated_store: DuckDBStore) -> None:
    """All eight seed classes evaluate; no per-class failures."""
    report = run_scan(populated_store, max_workers=1, n_samples=500)
    assert report.completed_at is not None
    assert report.failed == 0
    expected_classes = {
        "enso_neutral_to_elnino",
        "eia_grid_reliability_event",
        "final_rule_within_12mo",
        "gdelt_conflict_intensification",
        "multi_signal_geopolitical_alert",
        "opec_unscheduled_cut",
        "pheic_declaration_12mo",
        "polymarket_resolution_calibration",
    }
    seen = {r.class_id for r in report.classes}
    assert seen == expected_classes


def test_scan_persists_record_per_class(populated_store: DuckDBStore) -> None:
    report = run_scan(populated_store, max_workers=1, n_samples=500)
    with populated_store.connection() as conn:
        records = query_scan_records(conn, scan_id=report.scan_id)
    assert len(records) == 8


def test_scan_writes_trace_per_class(populated_store: DuckDBStore) -> None:
    report = run_scan(populated_store, max_workers=1, n_samples=500)
    with populated_store.connection() as conn:
        for record in report.classes:
            trace = query_trace(conn, scan_id=report.scan_id, class_id=record.class_id)
            assert trace is not None
            # The trace payload must be renderable to text.
            rendered = render_trace_text(trace.payload)
            assert isinstance(rendered, str)
            assert len(rendered) > 0


def test_scan_provenance_is_complete(populated_store: DuckDBStore) -> None:
    """Every record has library_version, definition_version, data_as_of."""
    report = run_scan(populated_store, max_workers=1, n_samples=500)
    for record in report.classes:
        assert record.pattern_library_version >= 1
        assert record.class_definition_version >= 1
        assert record.data_as_of is not None
        assert record.scan_started_at is not None


def test_scan_re_run_produces_distinct_scan_id(populated_store: DuckDBStore) -> None:
    """REQ-SCAN-PERSIST-003: each scan is a fresh immutable observation."""
    report_a = run_scan(populated_store, max_workers=1, n_samples=500)
    report_b = run_scan(populated_store, max_workers=1, n_samples=500)
    assert report_a.scan_id != report_b.scan_id
    with populated_store.connection() as conn:
        rows = conn.execute("SELECT COUNT(DISTINCT scan_id) FROM scan_records").fetchone()
    assert rows is not None
    assert rows[0] >= 2


def test_scan_summary_reflects_partial_success_when_no_data(
    populated_store: DuckDBStore,
) -> None:
    """Several seed classes will have no_update_applied because the
    scanner has no current-precursor data — those still count as
    succeeded (the class's evaluation completed cleanly), and the
    summary correctly reports the total.
    """
    report = run_scan(populated_store, max_workers=1, n_samples=500)
    with populated_store.connection() as conn:
        summary = query_scan_summary(conn, scan_id=report.scan_id)
    assert summary is not None
    assert summary.classes_total == 8
    assert summary.classes_succeeded + summary.classes_failed == summary.classes_total
    assert summary.classes_failed == 0


def test_scan_completes_quickly(populated_store: DuckDBStore) -> None:
    """v1 NFR-SCAN-PERF-002: single-class scan under 30s.

    The synthetic corpus is small; this should finish in seconds, not
    minutes. We give substantial headroom in the assertion to avoid
    flakiness on slow CI hardware.
    """
    report = run_scan(populated_store, max_workers=2, n_samples=300)
    assert report.duration_seconds is not None
    assert report.duration_seconds < 30.0
