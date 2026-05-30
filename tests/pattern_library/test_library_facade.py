"""T-PL-060 — public library facade tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library import library
from razor_rooster.pattern_library.engines.refresh import run_refresh
from razor_rooster.pattern_library.library import (
    EventClassSummary,
    base_rate,
    calibration,
    current_version,
    find_analogues_by_class_id,
    list_classes,
    signature,
)
from razor_rooster.pattern_library.models.event_class import (
    AnalogueFeature,
    EventClass,
    PrecursorVariable,
    Sector,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.pattern_library.registry import (
    _clear_for_tests,
    _set_discovered_for_tests,
    register,
)


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    _clear_for_tests()
    _set_discovered_for_tests(True)
    yield
    _clear_for_tests()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "pl_facade.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_pattern_library_migrations(conn)
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


def _make_class(
    *,
    class_id: str = "alpha",
    n_occurrences: int = 12,
    sector: Sector = Sector.PUBLIC_HEALTH,
    with_features: bool = True,
) -> EventClass:
    occurrences = [datetime(2014 + i, 6, 1, tzinfo=UTC) for i in range(n_occurrences)]
    df = pd.DataFrame({"occurrence_ts": occurrences})

    def occ_query(_conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
        return df

    def signal_query(
        _conn: duckdb.DuckDBPyConnection,
        window_start: datetime,
        window_end: datetime,
    ) -> pd.Series:
        index = pd.date_range(window_start, window_end, freq="D", tz="UTC")
        values = [
            5.0
            if any(
                (idx >= pd.Timestamp(o) - pd.Timedelta(days=60)) and (idx < pd.Timestamp(o))
                for o in occurrences
            )
            else 1.0
            for idx in index
        ]
        return pd.Series(values, index=index)

    precursor = PrecursorVariable(
        variable_id="signal",
        title="Signal",
        query=signal_query,
        direction="high_signals_event",
        lead_time_window=timedelta(days=60),
    )
    features: tuple[AnalogueFeature, ...] = ()
    if with_features:
        features = (
            AnalogueFeature(
                feature_id="month",
                query=lambda _c, ts: float(ts.month),
                weight=1.0,
            ),
        )
    return EventClass(
        class_id=class_id,
        title=f"{class_id} title",
        description=f"{class_id} description",
        domain_sector=sector,
        occurrence_query=occ_query,
        precursors=(precursor,),
        analogue_features=features,
        baseline_sample_size=50,
        refractory_months=3,
        base_rate_window_default=timedelta(days=365 * 30),
    )


def _do_refresh(store: DuckDBStore, *, lock_dir: Path, trace_dir: Path) -> None:
    run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
        now=datetime(2026, 5, 14, tzinfo=UTC),
    )


# -- current_version ------------------------------------------------------


def test_current_version_returns_positive_int() -> None:
    assert isinstance(current_version(), int)
    assert current_version() >= 1


def test_facade_re_exports_current_version() -> None:
    assert library.current_version() == current_version()


# -- list_classes ---------------------------------------------------------


def test_list_classes_empty(store: DuckDBStore) -> None:
    assert list_classes(store) == ()


def test_list_classes_returns_summaries(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="alpha"))
    register(_make_class(class_id="beta", sector=Sector.GEOPOLITICAL))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    summaries = list_classes(store)
    assert len(summaries) == 2
    assert all(isinstance(s, EventClassSummary) for s in summaries)
    by_id = {s.class_id: s for s in summaries}
    assert by_id["alpha"].domain_sector == Sector.PUBLIC_HEALTH
    assert by_id["beta"].domain_sector == Sector.GEOPOLITICAL


def test_list_classes_filters_by_sector(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="alpha", sector=Sector.PUBLIC_HEALTH))
    register(_make_class(class_id="beta", sector=Sector.GEOPOLITICAL))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    public_health = list_classes(store, sector=Sector.PUBLIC_HEALTH)
    assert {s.class_id for s in public_health} == {"alpha"}


def test_list_classes_excludes_removed_by_default(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="alpha"))
    register(_make_class(class_id="beta"))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    # Drop alpha from registry then refresh again to mark it removed.
    _clear_for_tests()
    _set_discovered_for_tests(True)
    register(_make_class(class_id="beta"))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    summaries = list_classes(store)
    assert {s.class_id for s in summaries} == {"beta"}


def test_list_classes_include_removed_returns_them(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="alpha"))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    _clear_for_tests()
    _set_discovered_for_tests(True)
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    summaries = list_classes(store, include_removed=True)
    assert {s.class_id for s in summaries} == {"alpha"}
    by_id = {s.class_id: s for s in summaries}
    assert by_id["alpha"].removed_at is not None


# -- base_rate ------------------------------------------------------------


def test_base_rate_returns_none_for_unknown_class(store: DuckDBStore) -> None:
    assert base_rate(store, "not_a_class") is None


def test_base_rate_returns_persisted_value(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="alpha"))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    result = base_rate(store, "alpha")
    assert result is not None
    assert result.class_id == "alpha"
    assert result.library_version >= 1
    assert result.occurrences == 12


def test_base_rate_filters_by_library_version(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="alpha"))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    # Filtering for a non-existent library version returns None.
    assert base_rate(store, "alpha", library_version=999) is None


# -- signature ------------------------------------------------------------


def test_signature_returns_empty_for_unknown_class(store: DuckDBStore) -> None:
    assert signature(store, "no_such") == ()


def test_signature_returns_persisted_signatures(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="alpha"))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    sigs = signature(store, "alpha")
    assert len(sigs) == 1
    assert sigs[0].variable_id == "signal"
    assert sigs[0].class_id == "alpha"
    assert sigs[0].library_version >= 1


def test_signature_with_explicit_library_version(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="alpha"))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    # Filtering for a non-existent library version returns empty.
    assert signature(store, "alpha", library_version=999) == ()


# -- find_analogues_by_class_id -----------------------------------------


def test_find_analogues_returns_results_for_known_class(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="alpha"))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    results = find_analogues_by_class_id(
        store,
        class_id="alpha",
        current_features={"month": 6.0},
        k=3,
    )
    assert results.class_id == "alpha"
    assert len(results.matches) <= 3


def test_find_analogues_empty_for_unknown_class(store: DuckDBStore) -> None:
    results = find_analogues_by_class_id(
        store,
        class_id="never_seen",
        current_features={"f": 1.0},
        k=3,
    )
    assert results.matches == ()


def test_find_analogues_with_explicit_library_version(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="alpha"))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    # Querying a version that doesn't exist returns no matches.
    results = find_analogues_by_class_id(
        store,
        class_id="alpha",
        current_features={"month": 6.0},
        library_version=999,
    )
    assert results.matches == ()


# -- calibration ---------------------------------------------------------


def test_calibration_returns_persisted_output(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="alpha", n_occurrences=12))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    result = calibration(store, "alpha")
    assert result is not None
    assert result.class_id == "alpha"
    assert result.method == "leave_one_out_signature"
    assert result.brier_score is not None


def test_calibration_returns_none_for_unknown_class(store: DuckDBStore) -> None:
    assert calibration(store, "not_present") is None


def test_calibration_for_class_with_few_occurrences(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="rare", n_occurrences=3))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    result = calibration(store, "rare")
    assert result is not None
    assert result.method == "insufficient_data"
    assert result.brier_score is None


def test_calibration_filters_by_library_version(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_class(class_id="alpha"))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    assert calibration(store, "alpha", library_version=999) is None


# -- versioning ----------------------------------------------------------


def test_returned_dataclasses_carry_library_version(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """Every public dataclass surfaces ``library_version`` so consumers can detect mismatches."""
    register(_make_class(class_id="alpha"))
    _do_refresh(store, lock_dir=lock_dir, trace_dir=trace_dir)
    br = base_rate(store, "alpha")
    sigs = signature(store, "alpha")
    cal = calibration(store, "alpha")
    analogues = find_analogues_by_class_id(store, class_id="alpha", current_features={"month": 6.0})
    assert br is not None and br.library_version >= 1
    assert sigs and all(s.library_version >= 1 for s in sigs)
    assert cal is not None and cal.library_version >= 1
    assert analogues.library_version >= 1
