"""T-SCAN-030 / T-SCAN-031 — class evaluator + scan orchestrator tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library import registry
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    PrecursorVariable,
    Sector,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.signal_scanner.engines.candidates import CandidateConfig
from razor_rooster.signal_scanner.engines.scanner import (
    StrictDriftAbort,
    evaluate_class,
    run_scan,
)
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
    registry._set_discovered_for_tests(True)
    yield
    registry._clear_for_tests()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "scan.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
    try:
        yield s
    finally:
        s.close()


def _occurrences_empty(_conn: object) -> pd.DataFrame:
    return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})


def _precursor_constant(value: float):
    """Factory: a precursor query that always returns a single-value series."""

    def _query(_conn: object, _start: datetime, _end: datetime) -> pd.Series:
        idx = pd.date_range(end=_end, periods=10, freq="D", tz="UTC")
        return pd.Series([value] * 10, index=idx, dtype=float)

    return _query


def _precursor_empty(_conn: object, _start: datetime, _end: datetime) -> pd.Series:
    return pd.Series(dtype=float)


def _make_class(
    class_id: str,
    *,
    sector: Sector = Sector.PUBLIC_HEALTH,
    precursor_value: float | None = 8.0,
) -> EventClass:
    query_fn = _precursor_empty if precursor_value is None else _precursor_constant(precursor_value)
    return EventClass(
        class_id=class_id,
        title=f"Test class {class_id}",
        description=f"Synthetic test class for scanner tests ({class_id})",
        domain_sector=sector,
        occurrence_query=_occurrences_empty,
        precursors=(
            PrecursorVariable(
                variable_id="v1",
                title="First precursor",
                query=query_fn,
                direction="high_signals_event",
                lead_time_window=timedelta(days=180),
            ),
        ),
    )


def _seed_pattern_library_outputs(
    store: DuckDBStore,
    class_id: str,
    *,
    rate_per_year: float = 0.05,
    threshold: float = 5.0,
    hit_rate: float = 0.7,
    fpr: float = 0.2,
    confidence: float = 0.8,
    library_version: int = 1,
    definition_version: int = 1,
) -> None:
    """Insert minimal pl_base_rates + pl_precursor_signatures rows so the
    scanner facade has something to read.

    The seeded rows match what the pattern_library engines would have
    produced; the test doesn't run a real refresh because that's
    pattern_library's domain, not the scanner's.
    """
    now = datetime(2026, 5, 15, tzinfo=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pl_event_classes ("
            "class_id, title, description, domain_sector, secondary_sectors, "
            "definition_version, outcome_type, registered_at, "
            "last_evaluated_at, library_version_at_last_eval, removed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            [
                class_id,
                f"Test class {class_id}",
                f"desc {class_id}",
                "public_health",
                json.dumps([]),
                definition_version,
                "binary",
                now,
                now,
                library_version,
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO pl_base_rates ("
            "class_id, window_start, window_end, occurrences, rate_per_year, "
            "credible_interval_lower, credible_interval_upper, prior_alpha, prior_beta, "
            "library_version, definition_version, data_as_of, computed_at, "
            "low_sample_warning, source_stale_warning, stale"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                class_id,
                now - timedelta(days=365 * 10),
                now,
                5,
                rate_per_year,
                rate_per_year * 0.5,
                rate_per_year * 1.5,
                0.5,
                0.5,
                library_version,
                definition_version,
                now,
                now,
                False,
                False,
                False,
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO pl_precursor_signatures ("
            "class_id, variable_id, library_version, definition_version, "
            "threshold_method, threshold_value, direction, lead_time_window_days, "
            "pre_event_mean, pre_event_p25, pre_event_p50, pre_event_p75, "
            "baseline_mean, baseline_p25, baseline_p50, baseline_p75, "
            "hit_rate, false_positive_rate, sample_size_events, sample_size_baseline, "
            "confidence_score, low_confidence_warning, computed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                class_id,
                "v1",
                library_version,
                definition_version,
                "youden_j",
                threshold,
                "high_signals_event",
                180,
                8.0,
                6.0,
                8.0,
                10.0,
                3.0,
                1.5,
                3.0,
                4.5,
                hit_rate,
                fpr,
                20,
                200,
                confidence,
                confidence < 0.3,
                now,
            ],
        )


def test_evaluate_class_strong_signal_marks_candidate(store: DuckDBStore) -> None:
    cls = _make_class("test_class_a")
    registry.register(cls)
    _seed_pattern_library_outputs(store, "test_class_a", hit_rate=0.8, fpr=0.1)
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    record, trace = evaluate_class(
        store=store,
        cls=cls,
        scan_id="test-001",
        scan_started_at=started,
        library_version=1,
        candidate_config=CandidateConfig(),
        n_samples=500,
    )
    assert record.error is None
    assert record.is_candidate is True
    assert record.candidate_direction == "elevated"
    assert record.posterior > record.base_rate
    assert trace.payload["is_candidate"] is True


def test_evaluate_class_weak_signal_not_candidate(store: DuckDBStore) -> None:
    cls = _make_class("test_class_b")
    registry.register(cls)
    # Confidence below floor.
    _seed_pattern_library_outputs(store, "test_class_b", hit_rate=0.6, fpr=0.5, confidence=0.1)
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    record, _trace = evaluate_class(
        store=store,
        cls=cls,
        scan_id="test-002",
        scan_started_at=started,
        library_version=1,
        candidate_config=CandidateConfig(),
        n_samples=500,
    )
    assert record.is_candidate is False
    assert record.low_signature_confidence is True


def test_evaluate_class_missing_data_no_update(store: DuckDBStore) -> None:
    cls = _make_class("test_class_c", precursor_value=None)
    registry.register(cls)
    _seed_pattern_library_outputs(store, "test_class_c")
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    record, trace = evaluate_class(
        store=store,
        cls=cls,
        scan_id="test-003",
        scan_started_at=started,
        library_version=1,
        candidate_config=CandidateConfig(),
        n_samples=500,
    )
    assert record.no_update_applied is True
    assert record.is_candidate is False
    assert record.log_odds_shift == 0.0
    assert trace.payload["no_update_applied"] is True


def test_evaluate_class_definition_drift_strict_aborts(store: DuckDBStore) -> None:
    cls = _make_class("test_class_d")
    registry.register(cls)
    # Seed pattern_library outputs at a stale definition_version.
    _seed_pattern_library_outputs(store, "test_class_d", definition_version=99)
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    with pytest.raises(StrictDriftAbort):
        evaluate_class(
            store=store,
            cls=cls,
            scan_id="test-004",
            scan_started_at=started,
            library_version=1,
            candidate_config=CandidateConfig(),
            n_samples=500,
            strict=True,
        )


def test_evaluate_class_definition_drift_non_strict_flags(store: DuckDBStore) -> None:
    cls = _make_class("test_class_e")
    registry.register(cls)
    _seed_pattern_library_outputs(store, "test_class_e", definition_version=99)
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    record, _trace = evaluate_class(
        store=store,
        cls=cls,
        scan_id="test-005",
        scan_started_at=started,
        library_version=1,
        candidate_config=CandidateConfig(),
        n_samples=500,
    )
    assert record.definition_drift_warning is True
    # Scan still completes (no error).
    assert record.error is None


def test_evaluate_class_no_persisted_base_rate(store: DuckDBStore) -> None:
    """Class registered but pattern_library never refreshed it."""
    cls = _make_class("test_class_f")
    registry.register(cls)
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    record, _trace = evaluate_class(
        store=store,
        cls=cls,
        scan_id="test-006",
        scan_started_at=started,
        library_version=1,
        candidate_config=CandidateConfig(),
        n_samples=500,
    )
    assert record.no_update_applied is True
    assert record.is_candidate is False


def test_evaluate_class_failure_isolation(store: DuckDBStore) -> None:
    """Bad precursor query is captured on record.error, not raised."""

    def _broken_query(_conn: object, _start: datetime, _end: datetime) -> pd.Series:
        raise RuntimeError("synthetic precursor failure")

    # Class with a precursor query that throws.
    cls = EventClass(
        class_id="test_class_broken",
        title="Broken class",
        description="Synthetic class with a broken precursor",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_occurrences_empty,
        precursors=(
            PrecursorVariable(
                variable_id="v1",
                title="Broken",
                query=_broken_query,
                direction="high_signals_event",
                lead_time_window=timedelta(days=180),
            ),
        ),
    )
    registry.register(cls)
    _seed_pattern_library_outputs(store, "test_class_broken")
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    record, trace = evaluate_class(
        store=store,
        cls=cls,
        scan_id="test-007",
        scan_started_at=started,
        library_version=1,
        candidate_config=CandidateConfig(),
        n_samples=500,
    )
    # Precursor failure is internal to the query helper and produces a
    # missing value, so the class evaluation completes with no_update,
    # not a hard error.
    assert record.no_update_applied is True
    assert record.is_candidate is False
    assert "no_update" in str(trace.payload)


def test_run_scan_persists_records_for_all_registered_classes(store: DuckDBStore) -> None:
    cls_a = _make_class("test_scan_a", precursor_value=8.0)
    cls_b = _make_class("test_scan_b", precursor_value=1.0)
    registry.register(cls_a)
    registry.register(cls_b)
    _seed_pattern_library_outputs(store, "test_scan_a")
    _seed_pattern_library_outputs(store, "test_scan_b")

    report = run_scan(store, max_workers=1, n_samples=500)
    assert report.completed_at is not None
    assert report.succeeded == 2
    assert report.failed == 0
    records = report.classes
    assert {r.class_id for r in records} == {"test_scan_a", "test_scan_b"}

    # Verify persistence
    with store.connection() as conn:
        persisted = query_scan_records(conn, scan_id=report.scan_id)
        summary = query_scan_summary(conn, scan_id=report.scan_id)
        trace_a = query_trace(conn, scan_id=report.scan_id, class_id="test_scan_a")
    assert len(persisted) == 2
    assert summary is not None
    assert summary.classes_total == 2
    assert summary.classes_succeeded == 2
    assert trace_a is not None
    assert trace_a.payload["class_id"] == "test_scan_a"


def test_run_scan_only_class_id_filters(store: DuckDBStore) -> None:
    cls_a = _make_class("test_scan_only_a")
    cls_b = _make_class("test_scan_only_b")
    registry.register(cls_a)
    registry.register(cls_b)
    _seed_pattern_library_outputs(store, "test_scan_only_a")
    _seed_pattern_library_outputs(store, "test_scan_only_b")
    report = run_scan(store, only_class_id="test_scan_only_a", max_workers=1, n_samples=500)
    assert report.succeeded == 1
    assert {r.class_id for r in report.classes} == {"test_scan_only_a"}


def test_run_scan_re_runs_produce_distinct_scan_ids(store: DuckDBStore) -> None:
    """REQ-SCAN-PERSIST-003: each scan is a fresh immutable observation."""
    cls = _make_class("test_scan_idempotent")
    registry.register(cls)
    _seed_pattern_library_outputs(store, "test_scan_idempotent")
    report_a = run_scan(store, max_workers=1, n_samples=500)
    report_b = run_scan(store, max_workers=1, n_samples=500)
    assert report_a.scan_id != report_b.scan_id

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT COUNT(DISTINCT scan_id) FROM scan_records WHERE class_id = ?",
            ["test_scan_idempotent"],
        ).fetchone()
    assert rows is not None and rows[0] == 2


def test_run_scan_marks_library_stale_when_no_refresh(store: DuckDBStore) -> None:
    cls = _make_class("test_scan_stale")
    registry.register(cls)
    _seed_pattern_library_outputs(store, "test_scan_stale")
    report = run_scan(store, max_workers=1, n_samples=500)
    # No pl_refresh_log rows -> library_stale_warning should be true.
    with store.connection() as conn:
        summary = query_scan_summary(conn, scan_id=report.scan_id)
    assert summary is not None
    assert summary.library_stale_warning is True


def test_run_scan_with_empty_registry_produces_empty_report(store: DuckDBStore) -> None:
    report = run_scan(store, max_workers=1, n_samples=500)
    assert report.classes == []
    assert report.succeeded == 0
    assert report.failed == 0
    assert report.completed_at is not None
