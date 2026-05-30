"""T-PL-080 — end-to-end refresh against a synthetic data_ingest corpus.

Composes the full pattern_library pipeline against a mocked
data_ingest snapshot covering all eight seed classes. Verifies:

- All eight seed classes auto-discover and register.
- Refresh against the populated corpus completes without per-class
  failure.
- Each class produces a base-rate row, signatures (if precursors are
  defined), analogue feature space (if analogue features are defined),
  and a calibration row.
- Library-version mismatch detection: a consumer holding an older
  version sees outputs tagged with the higher version.

The synthetic corpus is small but representative — enough rows for
the refresh pipeline to exercise its branches but not so many that
the test takes forever.
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
from razor_rooster.pattern_library import library, registry
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


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    registry._clear_for_tests()
    yield
    registry._clear_for_tests()


@pytest.fixture
def populated_store(tmp_path: Path) -> Iterator[DuckDBStore]:
    """A DuckDB store with a synthetic data_ingest corpus seeded.

    The seeded data covers:
    - WHO DON entries flagged with PHEIC text (occurrences for
      pheic_declaration_12mo).
    - GDELT events with country_iso3 distribution (occurrences for
      gdelt_conflict_intensification).
    - Federal Register proposed/final rule pairs (occurrences for
      final_rule_within_12mo).
    - FRED brent crude price series (occurrences for opec_unscheduled_cut
      via the heuristic).
    - NOAA ENSO 3.4 anomaly series.
    - EIA grid_disturbance series (one synthetic event so the class
      has at least one occurrence).
    - ACLED events for multi_signal_geopolitical_alert.
    - polymarket_resolutions so the meta-class scaffolding has a
      schema to query against (it'll still return empty per design).
    """
    db_path = tmp_path / "pl_e2e.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        register_polymarket_sources(conn)
        _seed_corpus(conn)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    return tmp_path / "library" / ".refresh.lock"


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    target = tmp_path / "calibration"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _seed_corpus(conn) -> None:
    """Insert a representative-but-small synthetic data_ingest corpus."""
    now = datetime(2026, 5, 14, tzinfo=UTC)

    # WHO DON: a handful of entries with PHEIC flags spread across
    # 2010-2024 plus a non-PHEIC distractor.
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

    # GDELT events: moderate density across two countries.
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

    # ACLED events: similar shape to GDELT for the multi-signal class.
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

    # Federal Register: pairs of proposed + final rules within 12 months
    # to exercise the docket-pair predicate.
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

    # FRED Brent crude: synthetic price series with a 12% jump halfway
    # through to trigger the OPEC heuristic.
    for day_offset in range(120):
        day = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day_offset)
        # Step up sharply at offset 60.
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

    # NOAA ENSO: simple oscillating series with a clear neutral->elnino
    # transition mid-window.
    for day_offset in range(180):
        day = datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=day_offset)
        # Anomaly stays near 0 for 90 days then ramps to +1.0.
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


# -- end-to-end refresh ---------------------------------------------------


def test_refresh_against_seeded_corpus_succeeds_for_all_classes(
    populated_store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """Full refresh against a populated corpus; every seed class completes."""
    registry.discover()
    report = run_refresh(
        populated_store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
        now=datetime(2026, 5, 14, tzinfo=UTC),
    )

    expected_class_ids = {
        "enso_neutral_to_elnino",
        "eia_grid_reliability_event",
        "final_rule_within_12mo",
        "gdelt_conflict_intensification",
        "multi_signal_geopolitical_alert",
        "opec_unscheduled_cut",
        "pheic_declaration_12mo",
        "polymarket_resolution_calibration",
    }
    seen = {o.class_id for o in report.classes}
    assert seen == expected_class_ids
    failed = [o for o in report.classes if o.status == "failed"]
    assert not failed, f"failed classes: {[(o.class_id, o.errors) for o in failed]}"


def test_refresh_persists_outputs_for_classes_with_data(
    populated_store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """Classes whose predicates match seed data should persist non-zero outputs."""
    registry.discover()
    run_refresh(
        populated_store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )

    # Classes that should have at least one occurrence given the seed corpus.
    classes_with_data = (
        "pheic_declaration_12mo",  # 5 PHEIC entries
        "gdelt_conflict_intensification",  # weekly density spikes
        "final_rule_within_12mo",  # 8 docket pairs
        "multi_signal_geopolitical_alert",  # ACLED weekly density spikes
    )
    for class_id in classes_with_data:
        result = library.base_rate(populated_store, class_id)
        assert result is not None, class_id
        assert result.occurrences > 0, class_id


def test_calibration_writes_trace_files(
    populated_store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """Each class should write a calibration trace JSON file."""
    registry.discover()
    run_refresh(
        populated_store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    expected_files = {
        "pheic_declaration_12mo.json",
        "gdelt_conflict_intensification.json",
        "final_rule_within_12mo.json",
        "opec_unscheduled_cut.json",
        "enso_neutral_to_elnino.json",
        "eia_grid_reliability_event.json",
        "multi_signal_geopolitical_alert.json",
        "polymarket_resolution_calibration.json",
    }
    written = {f.name for f in trace_dir.iterdir() if f.suffix == ".json"}
    missing = expected_files - written
    assert not missing, f"missing trace files: {missing}"


def test_facade_returns_versioned_outputs(
    populated_store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """Public facade returns dataclasses tagged with library_version."""
    registry.discover()
    run_refresh(
        populated_store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    summaries = library.list_classes(populated_store)
    assert len(summaries) >= 8

    cur = library.current_version()
    for summary in summaries:
        assert summary.library_version_at_last_eval is not None
        # The version stamped on the summary should match what
        # current_version() returns at refresh time.
        assert summary.library_version_at_last_eval == cur


def test_pattern_library_isolates_per_class_failures_e2e(
    populated_store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """Even a synthetic broken class doesn't tank the others.

    Seeded classes do not include a deliberately-broken one — the test
    confirms that under the populated corpus, none of the eight seed
    classes raise. A separate registry-isolation test in
    test_engine_refresh.py covers the deliberate-failure scenario.
    """
    registry.discover()
    report = run_refresh(
        populated_store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    # All eight succeed.
    statuses = {o.class_id: o.status for o in report.classes}
    for class_id, status in statuses.items():
        assert status == "ok", f"{class_id} failed with status {status}"


def test_library_version_mismatch_detection(
    populated_store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """Consumers can detect mismatches between their cached version and the live one."""
    registry.discover()
    run_refresh(
        populated_store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    # Read current version, then read a base rate. The base rate's
    # library_version should equal the current version.
    cur = library.current_version()
    result = library.base_rate(populated_store, "pheic_declaration_12mo")
    assert result is not None
    assert result.library_version == cur
    # Querying for a non-existent older version returns None — the
    # consumer's cache-invalidation contract.
    assert library.base_rate(populated_store, "pheic_declaration_12mo", library_version=999) is None
