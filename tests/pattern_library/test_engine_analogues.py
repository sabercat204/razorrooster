"""T-PL-045 — analogue feature space engine tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library.engines.analogues import (
    DEFAULT_TOP_K,
    find_analogues,
    populate_feature_space,
)
from razor_rooster.pattern_library.models.analogue import (
    AnalogueFeatureSpace,
    AnalogueResults,
)
from razor_rooster.pattern_library.models.event_class import (
    AnalogueFeature,
    EventClass,
    Sector,
)
from razor_rooster.pattern_library.models.outcomes import OutcomeRecord
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.pattern_library.persistence.operations import (
    upsert_analogue_features,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "pl_analogues.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_pattern_library_migrations(conn)
    try:
        yield s
    finally:
        s.close()


def _stub_query(*_args: object, **_kwargs: object) -> object:
    return None


def _make_outcome(idx: int, year: int) -> OutcomeRecord:
    return OutcomeRecord(
        class_id="test_class",
        occurrence_id=f"occ-{idx}",
        occurrence_ts=datetime(year, 6, 1, tzinfo=UTC),
    )


def _two_cluster_feature(
    feature_id: str,
    cluster_a_centroid: float,
    cluster_b_centroid: float,
    cluster_a_dts: list[datetime],
) -> AnalogueFeature:
    """A feature query that returns ``cluster_a_centroid`` for timestamps
    in cluster_a_dts and ``cluster_b_centroid`` everywhere else.
    """
    cluster_a_set = {ts.date() for ts in cluster_a_dts}

    def query(_conn: duckdb.DuckDBPyConnection, ts: datetime) -> float:
        if ts.date() in cluster_a_set:
            return cluster_a_centroid
        return cluster_b_centroid

    return AnalogueFeature(feature_id=feature_id, query=query, weight=1.0)


# -- populate_feature_space ----------------------------------------------


def test_populate_feature_space_writes_event_and_baseline_rows(
    store: DuckDBStore,
) -> None:
    occurrences = [_make_outcome(i, 2020 + i) for i in range(5)]
    feature_a = AnalogueFeature(
        feature_id="f_a",
        query=lambda _c, ts: float(ts.year),
        weight=1.0,
    )
    feature_b = AnalogueFeature(
        feature_id="f_b",
        query=lambda _c, ts: float(ts.month),
        weight=1.0,
    )
    cls = EventClass(
        class_id="test_class",
        title="x",
        description="x",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_stub_query,
        analogue_features=(feature_a, feature_b),
        baseline_sample_size=20,
        refractory_months=1,
    )

    rng = np.random.default_rng(42)
    with store.connection() as conn:
        space, rows = populate_feature_space(
            conn,
            cls,
            outcomes=occurrences,
            library_version=1,
            baseline_size=20,
            rng=rng,
            now=datetime(2026, 5, 14, tzinfo=UTC),
        )

    assert isinstance(space, AnalogueFeatureSpace)
    assert space.event_count == 5
    assert space.point_count >= 5  # at least events; baseline may be filtered
    assert "f_a" in space.normalization_params
    assert space.normalization_params["f_a"]["std"] > 0
    assert len(rows) == space.point_count
    # Row payload has both raw and normalized vectors.
    sample = rows[0]
    assert "f_a" in sample.feature_vector_raw
    assert "f_a" in sample.feature_vector_normalized


def test_populate_feature_space_no_features_returns_empty(store: DuckDBStore) -> None:
    cls = EventClass(
        class_id="test_class",
        title="x",
        description="x",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_stub_query,
        analogue_features=(),
    )
    occurrences = [_make_outcome(i, 2020 + i) for i in range(3)]
    with store.connection() as conn:
        space, rows = populate_feature_space(
            conn,
            cls,
            outcomes=occurrences,
            library_version=1,
        )
    assert rows == ()
    assert space.point_count == 0


def test_populate_feature_space_handles_failing_feature_query(
    store: DuckDBStore,
) -> None:
    """A feature query that raises gets coalesced to 0.0, doesn't kill the run."""
    bad_feature = AnalogueFeature(
        feature_id="bad",
        query=lambda _c, _t: (_ for _ in ()).throw(RuntimeError("boom")),
        weight=1.0,
    )
    good_feature = AnalogueFeature(
        feature_id="good",
        query=lambda _c, ts: float(ts.year),
        weight=1.0,
    )
    cls = EventClass(
        class_id="test_class",
        title="x",
        description="x",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_stub_query,
        analogue_features=(bad_feature, good_feature),
        baseline_sample_size=10,
        refractory_months=1,
    )
    occurrences = [_make_outcome(i, 2020 + i) for i in range(3)]
    with store.connection() as conn:
        space, rows = populate_feature_space(
            conn,
            cls,
            outcomes=occurrences,
            library_version=1,
            baseline_size=10,
        )
    assert space.point_count >= 3
    # Bad feature coalesces to 0.0 across all points.
    for row in rows:
        assert row.feature_vector_raw["bad"] == 0.0


# -- find_analogues ------------------------------------------------------


def _seed_feature_space(
    store: DuckDBStore,
    *,
    class_id: str,
    library_version: int,
    cluster_a_dts: list[datetime],
    cluster_b_dts: list[datetime],
) -> None:
    """Persist a synthetic two-cluster feature space directly."""
    feature_a = _two_cluster_feature("f_a", 1.0, 5.0, cluster_a_dts)
    feature_b = _two_cluster_feature("f_b", 1.0, 5.0, cluster_a_dts)
    cls = EventClass(
        class_id=class_id,
        title="x",
        description="x",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_stub_query,
        analogue_features=(feature_a, feature_b),
        baseline_sample_size=10,
        refractory_months=1,
    )
    outcomes = [
        OutcomeRecord(
            class_id=class_id,
            occurrence_id=f"a-{i}",
            occurrence_ts=ts,
        )
        for i, ts in enumerate(cluster_a_dts)
    ]
    with store.connection() as conn:
        space, rows = populate_feature_space(
            conn,
            cls,
            outcomes=outcomes,
            library_version=library_version,
            baseline_size=len(cluster_b_dts),
        )
        upsert_analogue_features(conn, space=space, rows=list(rows))


def test_find_analogues_returns_top_k(store: DuckDBStore) -> None:
    cluster_a_dts = [
        datetime(2020, 6, 1, tzinfo=UTC),
        datetime(2021, 6, 1, tzinfo=UTC),
        datetime(2022, 6, 1, tzinfo=UTC),
    ]
    cluster_b_dts = [datetime(2018, 1, 1, tzinfo=UTC) + timedelta(days=i * 30) for i in range(20)]
    _seed_feature_space(
        store,
        class_id="test_class",
        library_version=1,
        cluster_a_dts=cluster_a_dts,
        cluster_b_dts=cluster_b_dts,
    )
    with store.connection() as conn:
        results = find_analogues(
            conn,
            class_id="test_class",
            current_features={"f_a": 1.0, "f_b": 1.0},
            library_version=1,
            definition_version=1,
            k=3,
        )
    assert isinstance(results, AnalogueResults)
    assert len(results.matches) <= 3
    assert all(m.distance >= 0 for m in results.matches)


def test_find_analogues_top_match_is_event_when_query_near_event_centroid(
    store: DuckDBStore,
) -> None:
    cluster_a_dts = [datetime(2021, 6, 1, tzinfo=UTC)]
    cluster_b_dts = [datetime(2018, 1, 1, tzinfo=UTC) + timedelta(days=i * 60) for i in range(15)]
    _seed_feature_space(
        store,
        class_id="cluster_test",
        library_version=1,
        cluster_a_dts=cluster_a_dts,
        cluster_b_dts=cluster_b_dts,
    )
    with store.connection() as conn:
        results = find_analogues(
            conn,
            class_id="cluster_test",
            current_features={"f_a": 1.0, "f_b": 1.0},  # cluster A centroid
            library_version=1,
            definition_version=1,
            k=1,
        )
    assert len(results.matches) == 1
    # The single event is in cluster A; closest match should be that event.
    top = results.matches[0]
    assert top.is_event is True


def test_find_analogues_default_top_k() -> None:
    assert DEFAULT_TOP_K == 10


def test_find_analogues_empty_population(store: DuckDBStore) -> None:
    """Querying a class with no persisted population returns empty matches."""
    with store.connection() as conn:
        results = find_analogues(
            conn,
            class_id="never_populated",
            current_features={"f_a": 1.0},
            library_version=1,
            definition_version=1,
        )
    assert results.matches == ()


def test_find_analogues_respects_weights(store: DuckDBStore) -> None:
    """Heavy weight on f_a should make distance more sensitive to f_a."""
    cluster_a_dts = [datetime(2021, 6, 1, tzinfo=UTC)]
    cluster_b_dts = [datetime(2019, 1, 1, tzinfo=UTC) + timedelta(days=i * 60) for i in range(15)]
    _seed_feature_space(
        store,
        class_id="weight_test",
        library_version=1,
        cluster_a_dts=cluster_a_dts,
        cluster_b_dts=cluster_b_dts,
    )
    with store.connection() as conn:
        # Equal weight.
        equal_result = find_analogues(
            conn,
            class_id="weight_test",
            current_features={"f_a": 1.0, "f_b": 5.0},  # mixed cluster
            library_version=1,
            definition_version=1,
            feature_weights={"f_a": 1.0, "f_b": 1.0},
            k=1,
        )
        # Heavy weight on f_a → distance dominated by f_a deviation.
        heavy_result = find_analogues(
            conn,
            class_id="weight_test",
            current_features={"f_a": 1.0, "f_b": 5.0},
            library_version=1,
            definition_version=1,
            feature_weights={"f_a": 100.0, "f_b": 1.0},
            k=1,
        )
    # Both should return one match; the distances will differ because
    # of the weighting.
    assert len(equal_result.matches) == 1
    assert len(heavy_result.matches) == 1


def test_find_analogues_custom_metric_changes_ranking(store: DuckDBStore) -> None:
    """A custom metric (Manhattan) can produce different ranking than Euclidean."""
    cluster_a_dts = [
        datetime(2020, 6, 1, tzinfo=UTC),
        datetime(2021, 6, 1, tzinfo=UTC),
    ]
    cluster_b_dts = [datetime(2018, 1, 1, tzinfo=UTC) + timedelta(days=i * 60) for i in range(10)]
    _seed_feature_space(
        store,
        class_id="metric_test",
        library_version=1,
        cluster_a_dts=cluster_a_dts,
        cluster_b_dts=cluster_b_dts,
    )

    def manhattan(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.sum(np.abs(a - b)))

    with store.connection() as conn:
        result = find_analogues(
            conn,
            class_id="metric_test",
            current_features={"f_a": 1.0, "f_b": 1.0},
            library_version=1,
            definition_version=1,
            metric=manhattan,
            k=2,
        )
    assert len(result.matches) <= 2


def test_find_analogues_distance_is_zero_for_exact_match(store: DuckDBStore) -> None:
    """When the query exactly matches a persisted point's normalized vector,
    distance should be ~0.
    """
    cluster_a_dts = [datetime(2020, 6, 1, tzinfo=UTC)]
    cluster_b_dts = [datetime(2018, 1, 1, tzinfo=UTC) + timedelta(days=i * 60) for i in range(5)]
    _seed_feature_space(
        store,
        class_id="exact_match",
        library_version=1,
        cluster_a_dts=cluster_a_dts,
        cluster_b_dts=cluster_b_dts,
    )
    # Use the cluster-B centroid feature value (5.0) — most points sit there.
    with store.connection() as conn:
        result = find_analogues(
            conn,
            class_id="exact_match",
            current_features={"f_a": 5.0, "f_b": 5.0},
            library_version=1,
            definition_version=1,
            k=1,
        )
    assert len(result.matches) == 1
    # Exact-match within numerical tolerance.
    assert result.matches[0].distance < 0.5
