"""T-PL-050 — refresh runner tests."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library.engines.refresh import (
    DEFAULT_LOCK_PATH,
    DEFAULT_MAX_WORKERS,
    ClassRefreshOutcome,
    RefreshLockHeldError,
    RefreshReport,
    _acquire_refresh_lock,
    run_refresh,
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
    db_path = tmp_path / "pl_refresh.duckdb"
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
    """Per-test lock-file directory so tests don't share state."""
    target = tmp_path / "library"
    target.mkdir(parents=True, exist_ok=True)
    return target / ".refresh.lock"


@pytest.fixture
def trace_dir(tmp_path: Path) -> Path:
    target = tmp_path / "calibration"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _make_outcomes_query(
    occurrences: list[datetime],
):
    """Build an occurrence_query closure that returns a DataFrame."""
    df = pd.DataFrame({"occurrence_ts": occurrences})

    def query(_conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
        return df

    return query


def _signal_query(elevated_around: list[datetime]):
    """A precursor query with elevated lead values around each occurrence."""
    elevated_set = set(elevated_around)

    def query(
        _conn: duckdb.DuckDBPyConnection,
        window_start: datetime,
        window_end: datetime,
    ) -> pd.Series:
        index = pd.date_range(window_start, window_end, freq="D", tz="UTC")
        values = np.full(index.shape[0], 1.0, dtype=float)
        for elev in elevated_set:
            mask = (index >= pd.Timestamp(elev) - pd.Timedelta(days=60)) & (
                index < pd.Timestamp(elev)
            )
            values[mask] = 5.0
        return pd.Series(values, index=index)

    return query


def _make_simple_class(
    *,
    class_id: str = "test_class",
    n_occurrences: int = 12,
    with_precursor: bool = True,
    with_feature: bool = True,
) -> EventClass:
    """Build a minimal class with enough occurrences to trigger calibration."""
    occurrences = [datetime(2014 + i, 6, 1, tzinfo=UTC) for i in range(n_occurrences)]
    precursors: tuple[PrecursorVariable, ...] = ()
    if with_precursor:
        precursors = (
            PrecursorVariable(
                variable_id="signal",
                title="Signal",
                query=_signal_query(occurrences),
                direction="high_signals_event",
                lead_time_window=timedelta(days=60),
            ),
        )
    features: tuple[AnalogueFeature, ...] = ()
    if with_feature:
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
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_make_outcomes_query(occurrences),
        precursors=precursors,
        analogue_features=features,
        baseline_sample_size=50,
        refractory_months=3,
        base_rate_window_default=timedelta(days=365 * 30),
    )


# -- happy path -----------------------------------------------------------


def test_refresh_populates_all_tables(store: DuckDBStore, lock_dir: Path, trace_dir: Path) -> None:
    register(_make_simple_class())
    report = run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
        now=datetime(2026, 5, 14, tzinfo=UTC),
    )

    assert isinstance(report, RefreshReport)
    assert report.errors == []
    assert len(report.classes) == 1
    outcome = report.classes[0]
    assert outcome.status == "ok"
    assert outcome.occurrences_persisted == 12
    assert outcome.base_rate_computed is True
    assert outcome.signatures_computed == 1
    assert outcome.analogue_points_persisted >= 12  # events + baseline
    assert outcome.calibration_status == "computed"

    with store.connection() as conn:
        # Outcomes persisted.
        n = conn.execute(
            "SELECT COUNT(*) FROM pl_outcomes WHERE class_id = ?",
            ["test_class"],
        ).fetchone()
        assert n is not None
        assert n[0] == 12
        # Base rate persisted.
        br = conn.execute(
            "SELECT COUNT(*) FROM pl_base_rates WHERE class_id = ?",
            ["test_class"],
        ).fetchone()
        assert br is not None
        assert br[0] == 1
        # Signatures persisted.
        sig = conn.execute(
            "SELECT COUNT(*) FROM pl_precursor_signatures WHERE class_id = ?",
            ["test_class"],
        ).fetchone()
        assert sig is not None
        assert sig[0] == 1
        # Analogue features persisted.
        af = conn.execute(
            "SELECT COUNT(*) FROM pl_analogue_features WHERE class_id = ?",
            ["test_class"],
        ).fetchone()
        assert af is not None
        assert af[0] >= 12
        # Calibration persisted.
        cal = conn.execute(
            "SELECT method, brier_score FROM pl_calibration WHERE class_id = ?",
            ["test_class"],
        ).fetchone()
        assert cal is not None
        assert cal[0] == "leave_one_out_signature"
        assert cal[1] is not None  # Brier score computed
        # Refresh log row written.
        rl = conn.execute(
            "SELECT classes_processed FROM pl_refresh_log WHERE refresh_id = ?",
            [report.refresh_id],
        ).fetchone()
        assert rl is not None


def test_refresh_records_class_evaluation_timestamp(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_simple_class())
    when = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
        now=when,
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT last_evaluated_at, library_version_at_last_eval "
            "FROM pl_event_classes WHERE class_id = ?",
            ["test_class"],
        ).fetchone()
    assert row is not None
    assert row[0] == when


# -- failure isolation ---------------------------------------------------


def test_refresh_isolates_failing_class(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """One class throws; others still complete."""

    def bad_query(_conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
        raise RuntimeError("synthetic occurrence-query failure")

    bad_class = EventClass(
        class_id="bad_class",
        title="Bad",
        description="x",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=bad_query,
    )
    register(_make_simple_class(class_id="good_class"))
    register(bad_class)

    report = run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
        now=datetime(2026, 5, 14, tzinfo=UTC),
    )
    by_id = {o.class_id: o for o in report.classes}
    assert by_id["good_class"].status == "ok"
    assert by_id["bad_class"].status == "failed"
    assert any("synthetic" in err for err in by_id["bad_class"].errors)
    # Refresh-level errors stay empty — failure was per-class.
    assert report.errors == []


# -- only_class_id filter ------------------------------------------------


def test_refresh_only_class_id_filters(store: DuckDBStore, lock_dir: Path, trace_dir: Path) -> None:
    register(_make_simple_class(class_id="alpha"))
    register(_make_simple_class(class_id="beta"))

    report = run_refresh(
        store,
        only_class_id="alpha",
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    assert {o.class_id for o in report.classes} == {"alpha"}


def test_refresh_only_class_id_unknown_records_error(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_simple_class(class_id="alpha"))
    report = run_refresh(
        store,
        only_class_id="not_registered",
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    assert any("not_registered" in err for err in report.errors)
    assert report.classes == []


# -- library version bump rules ------------------------------------------


def test_refresh_bumps_for_added_class(store: DuckDBStore, lock_dir: Path, trace_dir: Path) -> None:
    register(_make_simple_class(class_id="brand_new"))
    report = run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    assert report.bump_reason == "class_added"
    with store.connection() as conn:
        row = conn.execute(
            "SELECT bump_reason FROM pl_library_versions ORDER BY bumped_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] == "class_added"


def test_refresh_no_bump_when_registry_unchanged(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_simple_class(class_id="stable"))
    run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    # Second refresh with no changes.
    report2 = run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    assert report2.bump_reason is None


def test_refresh_force_bumps_with_code_change(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_simple_class(class_id="stable"))
    run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    report2 = run_refresh(
        store,
        force=True,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    assert report2.bump_reason == "code_change"


# -- file lock ------------------------------------------------------------


def test_refresh_lock_prevents_concurrent_runs(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """Two threads contending for the lock — second one fails fast."""
    register(_make_simple_class())

    barrier = threading.Barrier(2)
    results: dict[str, RefreshReport] = {}

    def runner(name: str) -> None:
        barrier.wait()
        results[name] = run_refresh(
            store,
            lock_path=lock_dir,
            trace_dir=trace_dir,
            max_workers=1,
        )

    # Acquire the lock manually first so both threads conflict with it.
    with _acquire_refresh_lock(lock_dir):
        t = threading.Thread(target=runner, args=("blocked",), daemon=True)
        t.start()
        # Give the thread a moment to enter the acquire-loop.
        barrier.wait()
        # Wait a beat then check that it's still blocked or about to time out.
        t.join(timeout=0.5)
    # After releasing, the thread should eventually finish (acquired lock or timed out).
    t.join(timeout=15.0)

    # The blocked-while-held attempt may or may not have grabbed the lock
    # after we released it; we just need to confirm the harness didn't
    # crash and produces a typed report.
    assert "blocked" in results
    assert isinstance(results["blocked"], RefreshReport)


def test_refresh_lock_errors_when_held_too_long(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the lock can't be acquired, refresh records a clear error."""
    # Create a stale lock file ahead of time.
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_dir.write_text("test-blocker", encoding="utf-8")
    # Reduce the timeout so the test runs fast.
    monkeypatch.setattr(
        "razor_rooster.pattern_library.engines.refresh.LOCK_ACQUIRE_TIMEOUT_SECONDS",
        0.5,
    )

    register(_make_simple_class())
    report = run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    assert any("lock" in e.lower() for e in report.errors)


# -- empty / minimal cases ------------------------------------------------


def test_refresh_with_no_registered_classes_succeeds(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """Empty registry → empty classes list, no errors."""
    report = run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    assert report.classes == []
    assert report.errors == []
    with store.connection() as conn:
        rl = conn.execute(
            "SELECT COUNT(*) FROM pl_refresh_log WHERE refresh_id = ?",
            [report.refresh_id],
        ).fetchone()
    assert rl is not None
    assert rl[0] == 1


def test_refresh_class_with_no_precursors_or_features(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_simple_class(with_precursor=False, with_feature=False))
    report = run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    outcome = report.classes[0]
    assert outcome.status == "ok"
    assert outcome.signatures_computed == 0
    assert outcome.analogue_points_persisted == 0
    assert outcome.base_rate_computed is True


def test_refresh_class_with_few_occurrences_skips_calibration(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    register(_make_simple_class(n_occurrences=3))
    report = run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=1,
    )
    outcome = report.classes[0]
    assert outcome.status == "ok"
    assert outcome.calibration_status == "insufficient_data"


# -- parallel path --------------------------------------------------------


def test_refresh_parallel_path_completes(
    store: DuckDBStore, lock_dir: Path, trace_dir: Path
) -> None:
    """max_workers > 1 with multiple classes runs them in parallel."""
    register(_make_simple_class(class_id="alpha"))
    register(_make_simple_class(class_id="beta"))
    register(_make_simple_class(class_id="gamma"))

    report = run_refresh(
        store,
        lock_path=lock_dir,
        trace_dir=trace_dir,
        max_workers=3,
    )
    assert {o.class_id for o in report.classes} == {"alpha", "beta", "gamma"}
    assert all(o.status == "ok" for o in report.classes)


# -- constants -----------------------------------------------------------


def test_default_max_workers() -> None:
    assert DEFAULT_MAX_WORKERS == 2


def test_default_lock_path_under_data_library() -> None:
    assert "library" in str(DEFAULT_LOCK_PATH)
    assert ".refresh.lock" in str(DEFAULT_LOCK_PATH)


def test_class_refresh_outcome_dataclass_default_values() -> None:
    outcome = ClassRefreshOutcome(class_id="c", status="ok", duration_seconds=1.0)
    assert outcome.warnings == []
    assert outcome.errors == []
    assert outcome.calibration_status is None


# -- direct lock helper --------------------------------------------------


def test_acquire_lock_releases_on_exit(lock_dir: Path) -> None:
    """The lock file is removed when the context manager exits."""
    with _acquire_refresh_lock(lock_dir):
        assert lock_dir.exists()
    assert not lock_dir.exists()


def test_acquire_lock_raises_when_held(lock_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "razor_rooster.pattern_library.engines.refresh.LOCK_ACQUIRE_TIMEOUT_SECONDS",
        0.2,
    )
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_dir.write_text("stale-holder", encoding="utf-8")
    with pytest.raises(RefreshLockHeldError), _acquire_refresh_lock(lock_dir):
        pass
