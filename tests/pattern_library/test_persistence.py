"""T-PL-011 + T-PL-012 — pattern-library schemas, migration, and helpers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    applied_versions,
)
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library.models.analogue import AnalogueFeatureSpace
from razor_rooster.pattern_library.models.base_rate import BaseRateResult
from razor_rooster.pattern_library.models.calibration import (
    CalibrationOutput,
    ReliabilityBin,
)
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    Sector,
)
from razor_rooster.pattern_library.models.outcomes import OutcomeRecord
from razor_rooster.pattern_library.models.signature import SignatureResult
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.pattern_library.persistence.operations import (
    _AnalogueRow,
    mark_event_class_removed,
    query_analogue_population,
    query_latest_base_rate,
    query_outcomes,
    query_signatures,
    record_class_evaluation,
    record_library_version_bump,
    record_refresh,
    upsert_analogue_features,
    upsert_base_rate,
    upsert_calibration,
    upsert_event_class,
    upsert_outcomes,
    upsert_signature,
)
from razor_rooster.pattern_library.persistence.schemas import PL_TABLE_NAMES


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "pl_persistence.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_pattern_library_migrations(conn)
    try:
        yield s
    finally:
        s.close()


# -- migration -------------------------------------------------------------


def test_migration_creates_all_pl_tables(store: DuckDBStore) -> None:
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name LIKE 'pl_%'"
        ).fetchall()
    table_names = {r[0] for r in rows}
    for expected in PL_TABLE_NAMES:
        assert expected in table_names, f"missing pattern_library table: {expected}"


def test_migration_records_version_2001(store: DuckDBStore) -> None:
    with store.connection() as conn:
        versions = applied_versions(conn)
    assert 2001 in versions


def test_migration_idempotent(store: DuckDBStore) -> None:
    with store.connection() as conn:
        before = applied_versions(conn)
        applied = run_pending_pattern_library_migrations(conn)
        after = applied_versions(conn)
    assert applied == ()
    assert before == after


# -- pl_event_classes ------------------------------------------------------


def _stub_query(*_args: object, **_kwargs: object) -> object:
    return None


def _make_class(class_id: str = "test_class") -> EventClass:
    return EventClass(
        class_id=class_id,
        title="Test Class",
        description="A test class.",
        domain_sector=Sector.PUBLIC_HEALTH,
        secondary_sectors=(Sector.GEOPOLITICAL,),
        occurrence_query=_stub_query,
    )


def test_upsert_event_class_inserts(store: DuckDBStore) -> None:
    cls = _make_class()
    with store.connection() as conn:
        upsert_event_class(conn, cls)
        row = conn.execute(
            "SELECT class_id, domain_sector, secondary_sectors, definition_version, "
            "outcome_type, removed_at FROM pl_event_classes WHERE class_id = ?",
            [cls.class_id],
        ).fetchone()
    assert row is not None
    assert row[0] == "test_class"
    assert row[1] == "public_health"
    assert json.loads(row[2]) == ["geopolitical"]
    assert row[3] == 1
    assert row[4] == "binary"
    assert row[5] is None


def test_upsert_event_class_idempotent(store: DuckDBStore) -> None:
    cls = _make_class()
    with store.connection() as conn:
        upsert_event_class(conn, cls)
        upsert_event_class(conn, cls)
        rows = conn.execute(
            "SELECT COUNT(*) FROM pl_event_classes WHERE class_id = ?",
            [cls.class_id],
        ).fetchone()
    assert rows is not None
    assert rows[0] == 1


def test_upsert_event_class_updates_existing(store: DuckDBStore) -> None:
    cls_v1 = _make_class()
    with store.connection() as conn:
        upsert_event_class(conn, cls_v1)
        # Bump the description and definition_version.
        cls_v2 = EventClass(
            class_id="test_class",
            title="Test Class v2",
            description="Updated description.",
            domain_sector=Sector.PUBLIC_HEALTH,
            occurrence_query=_stub_query,
            definition_version=2,
        )
        upsert_event_class(conn, cls_v2)
        row = conn.execute(
            "SELECT title, description, definition_version FROM pl_event_classes "
            "WHERE class_id = ?",
            ["test_class"],
        ).fetchone()
    assert row is not None
    assert row[0] == "Test Class v2"
    assert row[1] == "Updated description."
    assert row[2] == 2


def test_mark_event_class_removed(store: DuckDBStore) -> None:
    cls = _make_class()
    when = datetime(2026, 5, 14, tzinfo=UTC)
    with store.connection() as conn:
        upsert_event_class(conn, cls)
        mark_event_class_removed(conn, cls.class_id, when=when)
        row = conn.execute(
            "SELECT removed_at FROM pl_event_classes WHERE class_id = ?",
            [cls.class_id],
        ).fetchone()
    assert row is not None
    assert row[0] == when


def test_record_class_evaluation_stamps_columns(store: DuckDBStore) -> None:
    cls = _make_class()
    when = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    with store.connection() as conn:
        upsert_event_class(conn, cls)
        record_class_evaluation(conn, class_id=cls.class_id, library_version=3, when=when)
        row = conn.execute(
            "SELECT last_evaluated_at, library_version_at_last_eval "
            "FROM pl_event_classes WHERE class_id = ?",
            [cls.class_id],
        ).fetchone()
    assert row is not None
    assert row[0] == when
    assert row[1] == 3


# -- pl_outcomes -----------------------------------------------------------


def test_upsert_outcomes_writes_and_queries(store: DuckDBStore) -> None:
    rec_a = OutcomeRecord(
        class_id="c",
        occurrence_id="a",
        occurrence_ts=datetime(2025, 1, 1, tzinfo=UTC),
        description="first",
        source_records=({"table": "event_stream", "source_record_id": "abc"},),
    )
    rec_b = OutcomeRecord(
        class_id="c",
        occurrence_id="b",
        occurrence_ts=datetime(2025, 6, 1, tzinfo=UTC),
        description="second",
    )
    with store.connection() as conn:
        n = upsert_outcomes(conn, [rec_a, rec_b], library_version=1, definition_version=1)
        rows = query_outcomes(conn, "c")
    assert n == 2
    assert len(rows) == 2
    by_id = {r.occurrence_id: r for r in rows}
    assert by_id["a"].description == "first"
    assert by_id["a"].source_records == ({"table": "event_stream", "source_record_id": "abc"},)


def test_upsert_outcomes_idempotent_on_re_run(store: DuckDBStore) -> None:
    rec = OutcomeRecord(
        class_id="c",
        occurrence_id="a",
        occurrence_ts=datetime(2025, 1, 1, tzinfo=UTC),
    )
    with store.connection() as conn:
        upsert_outcomes(conn, [rec], library_version=1, definition_version=1)
        upsert_outcomes(conn, [rec], library_version=1, definition_version=1)
        row = conn.execute("SELECT COUNT(*) FROM pl_outcomes").fetchone()
    assert row is not None
    assert row[0] == 1


def test_upsert_outcomes_empty_iterable_is_noop(store: DuckDBStore) -> None:
    with store.connection() as conn:
        n = upsert_outcomes(conn, [], library_version=1, definition_version=1)
    assert n == 0


# -- pl_base_rates ---------------------------------------------------------


def _make_base_rate(class_id: str = "c", library_version: int = 1) -> BaseRateResult:
    return BaseRateResult(
        class_id=class_id,
        window_start=datetime(2010, 1, 1, tzinfo=UTC),
        window_end=datetime(2025, 1, 1, tzinfo=UTC),
        occurrences=10,
        rate_per_year=0.66,
        credible_interval_lower=0.3,
        credible_interval_upper=1.2,
        prior_alpha=0.5,
        prior_beta=0.5,
        library_version=library_version,
        definition_version=1,
        data_as_of=datetime(2026, 5, 14, tzinfo=UTC),
        computed_at=datetime(2026, 5, 14, tzinfo=UTC),
    )


def test_upsert_base_rate_round_trip(store: DuckDBStore) -> None:
    br = _make_base_rate()
    with store.connection() as conn:
        upsert_base_rate(conn, br)
        loaded = query_latest_base_rate(conn, "c")
    assert loaded is not None
    assert loaded.class_id == "c"
    assert loaded.occurrences == 10
    assert loaded.rate_per_year == pytest.approx(0.66)


def test_upsert_base_rate_replaces_same_window_version(store: DuckDBStore) -> None:
    br = _make_base_rate()
    br_v2 = BaseRateResult(
        class_id="c",
        window_start=datetime(2010, 1, 1, tzinfo=UTC),
        window_end=datetime(2025, 1, 1, tzinfo=UTC),
        occurrences=12,
        rate_per_year=0.80,
        credible_interval_lower=0.4,
        credible_interval_upper=1.3,
        prior_alpha=0.5,
        prior_beta=0.5,
        library_version=1,
        definition_version=1,
        data_as_of=datetime(2026, 5, 15, tzinfo=UTC),
        computed_at=datetime(2026, 5, 15, tzinfo=UTC),
    )
    with store.connection() as conn:
        upsert_base_rate(conn, br)
        upsert_base_rate(conn, br_v2)
        loaded = query_latest_base_rate(conn, "c")
        all_rows = conn.execute(
            "SELECT COUNT(*) FROM pl_base_rates WHERE class_id = ?", ["c"]
        ).fetchone()
    assert loaded is not None
    assert loaded.occurrences == 12
    assert all_rows is not None
    assert all_rows[0] == 1


def test_query_latest_base_rate_filters_by_library_version(
    store: DuckDBStore,
) -> None:
    br_v1 = _make_base_rate(library_version=1)
    br_v2 = _make_base_rate(library_version=2)
    with store.connection() as conn:
        upsert_base_rate(conn, br_v1)
        upsert_base_rate(conn, br_v2)
        loaded_v1 = query_latest_base_rate(conn, "c", library_version=1)
    assert loaded_v1 is not None
    assert loaded_v1.library_version == 1


def test_query_latest_base_rate_returns_none_for_unknown_class(
    store: DuckDBStore,
) -> None:
    with store.connection() as conn:
        result = query_latest_base_rate(conn, "no_such_class")
    assert result is None


# -- pl_precursor_signatures -----------------------------------------------


def _make_signature(variable_id: str = "v") -> SignatureResult:
    return SignatureResult(
        class_id="c",
        variable_id=variable_id,
        library_version=1,
        definition_version=1,
        threshold_method="youden_j",
        threshold_value=0.5,
        direction="high_signals_event",
        lead_time_window_days=180,
        pre_event_mean=0.3,
        pre_event_p25=0.2,
        pre_event_p50=0.3,
        pre_event_p75=0.4,
        baseline_mean=0.1,
        baseline_p25=0.05,
        baseline_p50=0.1,
        baseline_p75=0.15,
        hit_rate=0.8,
        false_positive_rate=0.1,
        sample_size_events=10,
        sample_size_baseline=1000,
        confidence_score=0.7,
        computed_at=datetime(2026, 5, 14, tzinfo=UTC),
    )


def test_upsert_signature_round_trip(store: DuckDBStore) -> None:
    sig = _make_signature()
    with store.connection() as conn:
        upsert_signature(conn, sig)
        loaded = query_signatures(conn, "c")
    assert len(loaded) == 1
    assert loaded[0].variable_id == "v"
    assert loaded[0].confidence_score == pytest.approx(0.7)


def test_upsert_signature_replaces_same_variable_version(
    store: DuckDBStore,
) -> None:
    sig_v1 = _make_signature()
    sig_v2 = SignatureResult(
        class_id="c",
        variable_id="v",
        library_version=1,
        definition_version=1,
        threshold_method="youden_j",
        threshold_value=0.6,
        direction="high_signals_event",
        lead_time_window_days=180,
        pre_event_mean=0.4,
        pre_event_p25=None,
        pre_event_p50=None,
        pre_event_p75=None,
        baseline_mean=0.1,
        baseline_p25=None,
        baseline_p50=None,
        baseline_p75=None,
        hit_rate=0.9,
        false_positive_rate=0.05,
        sample_size_events=10,
        sample_size_baseline=1000,
        confidence_score=0.8,
        computed_at=datetime(2026, 5, 15, tzinfo=UTC),
    )
    with store.connection() as conn:
        upsert_signature(conn, sig_v1)
        upsert_signature(conn, sig_v2)
        loaded = query_signatures(conn, "c")
    assert len(loaded) == 1
    assert loaded[0].confidence_score == pytest.approx(0.8)


def test_query_signatures_filters_by_library_version(store: DuckDBStore) -> None:
    sig_v1 = _make_signature()
    with store.connection() as conn:
        upsert_signature(conn, sig_v1)
        none_at_v2 = query_signatures(conn, "c", library_version=2)
    assert none_at_v2 == ()


# -- pl_analogue_features --------------------------------------------------


def test_upsert_analogue_features_writes_and_loads(store: DuckDBStore) -> None:
    space = AnalogueFeatureSpace(
        class_id="c",
        library_version=1,
        definition_version=1,
        feature_ids=("f1", "f2"),
        point_count=2,
        event_count=1,
        normalization_params={"f1": {"mean": 0.0, "std": 1.0}},
    )
    rows = [
        _AnalogueRow(
            point_id="event:1",
            timestamp=datetime(2025, 1, 1, tzinfo=UTC),
            is_event=True,
            feature_vector_raw={"f1": 1.0, "f2": 2.0},
            feature_vector_normalized={"f1": 0.5, "f2": 1.0},
        ),
        _AnalogueRow(
            point_id="baseline:abc",
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            is_event=False,
            feature_vector_raw={"f1": 0.0, "f2": 0.0},
            feature_vector_normalized={"f1": -0.5, "f2": -1.0},
        ),
    ]
    with store.connection() as conn:
        n = upsert_analogue_features(conn, space=space, rows=rows)
        loaded = query_analogue_population(conn, "c", library_version=1)
    assert n == 2
    assert len(loaded) == 2
    events = [r for r in loaded if r.is_event]
    assert len(events) == 1
    assert events[0].feature_vector_raw == {"f1": 1.0, "f2": 2.0}


def test_upsert_analogue_features_replaces_for_same_version(
    store: DuckDBStore,
) -> None:
    space = AnalogueFeatureSpace(
        class_id="c",
        library_version=1,
        definition_version=1,
        feature_ids=("f1",),
        point_count=1,
        event_count=1,
        normalization_params={},
    )
    row1 = _AnalogueRow(
        point_id="event:1",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        is_event=True,
        feature_vector_raw={"f1": 1.0},
        feature_vector_normalized={"f1": 0.5},
    )
    row2 = _AnalogueRow(
        point_id="event:2",
        timestamp=datetime(2025, 6, 1, tzinfo=UTC),
        is_event=True,
        feature_vector_raw={"f1": 2.0},
        feature_vector_normalized={"f1": 1.0},
    )
    with store.connection() as conn:
        upsert_analogue_features(conn, space=space, rows=[row1])
        upsert_analogue_features(conn, space=space, rows=[row2])
        loaded = query_analogue_population(conn, "c", library_version=1)
    assert len(loaded) == 1
    assert loaded[0].point_id == "event:2"


def test_upsert_analogue_features_empty_is_noop(store: DuckDBStore) -> None:
    space = AnalogueFeatureSpace(
        class_id="c",
        library_version=1,
        definition_version=1,
        feature_ids=("f1",),
        point_count=1,
        event_count=1,
        normalization_params={},
    )
    with store.connection() as conn:
        n = upsert_analogue_features(conn, space=space, rows=[])
    assert n == 0


# -- pl_calibration --------------------------------------------------------


def test_upsert_calibration_round_trip(store: DuckDBStore) -> None:
    out = CalibrationOutput(
        class_id="c",
        library_version=1,
        definition_version=1,
        method="leave_one_out_signature",
        brier_score=0.18,
        reliability_bins=(
            ReliabilityBin(
                bin_low=0.0,
                bin_high=0.1,
                predicted_mean=0.05,
                observed_freq=0.04,
                count=20,
            ),
        ),
        prediction_trace_path="data/library/calibration/c.json",
        computed_at=datetime(2026, 5, 14, tzinfo=UTC),
    )
    with store.connection() as conn:
        upsert_calibration(conn, out)
        row = conn.execute(
            "SELECT brier_score, prediction_trace_path, reliability_bins "
            "FROM pl_calibration WHERE class_id = ?",
            ["c"],
        ).fetchone()
    assert row is not None
    assert row[0] == pytest.approx(0.18)
    assert row[1] == "data/library/calibration/c.json"
    bins = json.loads(row[2])
    assert len(bins) == 1
    assert bins[0]["count"] == 20


def test_upsert_calibration_insufficient_data(store: DuckDBStore) -> None:
    out = CalibrationOutput(
        class_id="rare",
        library_version=1,
        definition_version=1,
        method="insufficient_data",
        brier_score=None,
        reliability_bins=(),
        prediction_trace_path="data/library/calibration/rare.json",
        computed_at=datetime(2026, 5, 14, tzinfo=UTC),
    )
    with store.connection() as conn:
        upsert_calibration(conn, out)
        row = conn.execute(
            "SELECT brier_score, method FROM pl_calibration WHERE class_id = ?",
            ["rare"],
        ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] == "insufficient_data"


# -- pl_library_versions + pl_refresh_log ----------------------------------


def test_record_library_version_bump_inserts(store: DuckDBStore) -> None:
    when = datetime(2026, 5, 14, tzinfo=UTC)
    with store.connection() as conn:
        record_library_version_bump(
            conn,
            library_version=2,
            bump_reason="class_added",
            affected_class_ids=("c1", "c2"),
            when=when,
        )
        row = conn.execute(
            "SELECT library_version, bump_reason, affected_class_ids, bumped_at "
            "FROM pl_library_versions WHERE library_version = ?",
            [2],
        ).fetchone()
    assert row is not None
    assert row[0] == 2
    assert row[1] == "class_added"
    assert json.loads(row[2]) == ["c1", "c2"]
    assert row[3] == when


def test_record_library_version_bump_idempotent(store: DuckDBStore) -> None:
    with store.connection() as conn:
        record_library_version_bump(conn, library_version=2, bump_reason="code_change")
        record_library_version_bump(conn, library_version=2, bump_reason="code_change")
        rows = conn.execute(
            "SELECT COUNT(*) FROM pl_library_versions WHERE library_version = ?",
            [2],
        ).fetchone()
    assert rows is not None
    assert rows[0] == 1


def test_record_refresh_appends_row(store: DuckDBStore) -> None:
    started = datetime(2026, 5, 14, tzinfo=UTC)
    ended = datetime(2026, 5, 14, 0, 30, tzinfo=UTC)
    with store.connection() as conn:
        rid = record_refresh(
            conn,
            started_at=started,
            ended_at=ended,
            library_version=1,
            classes_processed=[
                {"class_id": "c1", "status": "ok", "duration_seconds": 1.0},
            ],
        )
        row = conn.execute(
            "SELECT refresh_id, library_version, started_at, ended_at "
            "FROM pl_refresh_log WHERE refresh_id = ?",
            [rid],
        ).fetchone()
    assert row is not None
    assert row[0] == rid
    assert row[1] == 1
    assert row[2] == started
    assert row[3] == ended


def test_record_refresh_with_explicit_id(store: DuckDBStore) -> None:
    started = datetime(2026, 5, 14, tzinfo=UTC)
    explicit = "test-refresh-001"
    with store.connection() as conn:
        rid = record_refresh(
            conn,
            refresh_id=explicit,
            started_at=started,
            ended_at=None,
            library_version=1,
            classes_processed=[],
        )
    assert rid == explicit
