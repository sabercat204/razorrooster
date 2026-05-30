"""T-PL-043 + T-PL-044 — signature engine and multi-variable combination tests."""

from __future__ import annotations

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
from razor_rooster.pattern_library.engines.signatures import (
    CombinedScore,
    build_co_occurrence_table,
    combine_variables,
    compute_signature,
)
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    PrecursorVariable,
    Sector,
    ThresholdMethod,
)
from razor_rooster.pattern_library.models.outcomes import OutcomeRecord
from razor_rooster.pattern_library.models.signature import SignatureResult
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "pl_signatures.duckdb"
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


def _build_class(
    *,
    class_id: str = "test_class",
    precursors: tuple[PrecursorVariable, ...] = (),
) -> EventClass:
    return EventClass(
        class_id=class_id,
        title="Test Class",
        description="A test class.",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_stub_query,
        precursors=precursors,
        baseline_sample_size=100,
        refractory_months=3,
    )


def _make_outcome(idx: int, year: int) -> OutcomeRecord:
    return OutcomeRecord(
        class_id="test_class",
        occurrence_id=f"occ-{idx}",
        occurrence_ts=datetime(year, 6, 1, tzinfo=UTC),
    )


def _signal_query(
    elevated_around: list[datetime],
    *,
    elevated_value: float = 5.0,
    baseline_value: float = 1.0,
    pre_event_lead_days: int = 60,
) -> object:
    """Build a precursor query that returns an elevated signal in the lead-up
    window before each elevated_around timestamp, baseline elsewhere.
    """
    elevated_set = set(elevated_around)

    def query(
        _conn: duckdb.DuckDBPyConnection,
        window_start: datetime,
        window_end: datetime,
    ) -> pd.Series:
        # Daily series spanning the window.
        index = pd.date_range(start=window_start, end=window_end, freq="D", tz="UTC")
        values = np.full(index.shape[0], baseline_value, dtype=float)
        for elevated in elevated_set:
            elevated_pd = pd.Timestamp(elevated)
            lead_window_start = elevated_pd - pd.Timedelta(days=pre_event_lead_days)
            mask = (index >= lead_window_start) & (index < elevated_pd)
            values[mask] = elevated_value
        return pd.Series(values, index=index)

    return query


# -- compute_signature ----------------------------------------------------


def test_compute_signature_recovers_strong_signal(store: DuckDBStore) -> None:
    """A precursor that's elevated before each event should produce high
    hit rate, low FPR, and a non-zero confidence score.
    """
    occurrences = [_make_outcome(i, 2018 + i) for i in range(8)]
    occurrence_dts = [o.occurrence_ts for o in occurrences]
    precursor = PrecursorVariable(
        variable_id="strong_signal",
        title="Strong",
        query=_signal_query(occurrence_dts),
        direction="high_signals_event",
        lead_time_window=timedelta(days=60),
    )
    cls = _build_class(precursors=(precursor,))

    rng = np.random.default_rng(42)
    with store.connection() as conn:
        signatures, samples = compute_signature(
            conn,
            cls,
            outcomes=occurrences,
            library_version=1,
            bootstrap_iterations=20,
            rng=rng,
            now=datetime(2026, 5, 14, tzinfo=UTC),
        )

    assert len(signatures) == 1
    sig = signatures[0]
    assert isinstance(sig, SignatureResult)
    assert sig.variable_id == "strong_signal"
    assert sig.hit_rate is not None
    assert sig.hit_rate > 0.7  # most events show the elevated signal
    assert sig.false_positive_rate is not None
    assert sig.false_positive_rate < 0.3
    assert sig.confidence_score > 0.0
    assert sig.threshold_value is not None
    assert "strong_signal" in samples


def test_compute_signature_low_signal_low_confidence(store: DuckDBStore) -> None:
    """A noise-only precursor produces low confidence and low_confidence_warning."""

    def noise_query(
        _conn: duckdb.DuckDBPyConnection,
        window_start: datetime,
        window_end: datetime,
    ) -> pd.Series:
        index = pd.date_range(window_start, window_end, freq="D", tz="UTC")
        rng = np.random.default_rng(123)
        return pd.Series(rng.normal(size=index.shape[0]), index=index)

    occurrences = [_make_outcome(i, 2018 + i) for i in range(6)]
    precursor = PrecursorVariable(
        variable_id="noise",
        title="Noise",
        query=noise_query,
        direction="high_signals_event",
    )
    cls = _build_class(precursors=(precursor,))

    rng = np.random.default_rng(7)
    with store.connection() as conn:
        signatures, _ = compute_signature(
            conn,
            cls,
            outcomes=occurrences,
            library_version=1,
            bootstrap_iterations=20,
            rng=rng,
        )

    assert len(signatures) == 1
    sig = signatures[0]
    # Noise → effect size near zero → low confidence.
    assert sig.confidence_score < 0.3


def test_compute_signature_low_signals_event_direction(store: DuckDBStore) -> None:
    """A precursor that LOWS before events should still be detected via
    direction='low_signals_event'.
    """
    occurrences = [_make_outcome(i, 2018 + i) for i in range(8)]
    occurrence_dts = [o.occurrence_ts for o in occurrences]
    # Construct a "low-before-event" precursor: baseline_value=5,
    # elevated_value=1 in the lead window.
    precursor = PrecursorVariable(
        variable_id="low_signal",
        title="Low",
        query=_signal_query(
            occurrence_dts,
            elevated_value=1.0,
            baseline_value=5.0,
        ),
        direction="low_signals_event",
        lead_time_window=timedelta(days=60),
    )
    cls = _build_class(precursors=(precursor,))

    rng = np.random.default_rng(42)
    with store.connection() as conn:
        signatures, _ = compute_signature(
            conn, cls, outcomes=occurrences, library_version=1, rng=rng
        )

    sig = signatures[0]
    # Pre-event values should be lower than baseline — engine should
    # discover that the threshold is somewhere between the two.
    assert sig.hit_rate is not None
    assert sig.hit_rate > 0.7
    # Threshold sits between 1 (elevated) and 5 (baseline).
    assert sig.threshold_value is not None
    assert 1.0 <= sig.threshold_value <= 5.0


def test_compute_signature_with_no_outcomes_returns_empty(store: DuckDBStore) -> None:
    cls = _build_class(
        precursors=(
            PrecursorVariable(
                variable_id="v",
                title="V",
                query=_signal_query([]),
                direction="high_signals_event",
            ),
        )
    )
    with store.connection() as conn:
        signatures, samples = compute_signature(conn, cls, outcomes=[], library_version=1)
    assert signatures == ()
    assert samples == {}


def test_compute_signature_with_no_precursors_returns_empty(store: DuckDBStore) -> None:
    cls = _build_class(precursors=())
    occurrences = [_make_outcome(i, 2018 + i) for i in range(4)]
    with store.connection() as conn:
        signatures, samples = compute_signature(conn, cls, outcomes=occurrences, library_version=1)
    assert signatures == ()
    assert samples == {}


def test_compute_signature_handles_extraction_error(store: DuckDBStore) -> None:
    """A precursor whose query raises produces a zero-confidence signature."""

    def raising_query(*_args: object, **_kwargs: object) -> pd.Series:
        raise RuntimeError("synthetic query failure")

    cls = _build_class(
        precursors=(
            PrecursorVariable(
                variable_id="broken",
                title="Broken",
                query=raising_query,
                direction="high_signals_event",
            ),
        )
    )
    occurrences = [_make_outcome(i, 2018 + i) for i in range(4)]
    with store.connection() as conn:
        signatures, samples = compute_signature(conn, cls, outcomes=occurrences, library_version=1)
    assert len(signatures) == 1
    assert signatures[0].confidence_score == 0.0
    assert signatures[0].low_confidence_warning is True
    assert "broken" not in samples  # failed extraction → no samples persisted


def test_compute_signature_invalid_query_return_type(store: DuckDBStore) -> None:
    """Query returning non-Series → captured as failure, not raised."""

    def bad_query(*_args: object, **_kwargs: object) -> str:
        return "not a series"

    cls = _build_class(
        precursors=(
            PrecursorVariable(
                variable_id="bad",
                title="Bad",
                query=bad_query,
                direction="high_signals_event",
            ),
        )
    )
    occurrences = [_make_outcome(i, 2018 + i) for i in range(4)]
    with store.connection() as conn:
        signatures, _ = compute_signature(conn, cls, outcomes=occurrences, library_version=1)
    assert signatures[0].confidence_score == 0.0
    assert signatures[0].low_confidence_warning is True


def test_signature_threshold_method_quantile_95(store: DuckDBStore) -> None:
    """Quantile-95 method picks a threshold from the baseline distribution."""
    occurrences = [_make_outcome(i, 2018 + i) for i in range(6)]
    occurrence_dts = [o.occurrence_ts for o in occurrences]
    precursor = PrecursorVariable(
        variable_id="p95",
        title="P95",
        query=_signal_query(occurrence_dts),
        direction="high_signals_event",
        threshold_method=ThresholdMethod.QUANTILE_95,
    )
    cls = _build_class(precursors=(precursor,))
    with store.connection() as conn:
        signatures, _ = compute_signature(conn, cls, outcomes=occurrences, library_version=1)
    assert signatures[0].threshold_method == "quantile_95"
    assert signatures[0].threshold_value is not None


def test_signature_threshold_method_manual(store: DuckDBStore) -> None:
    occurrences = [_make_outcome(i, 2018 + i) for i in range(4)]
    occurrence_dts = [o.occurrence_ts for o in occurrences]
    precursor = PrecursorVariable(
        variable_id="manual_t",
        title="Manual Threshold",
        query=_signal_query(occurrence_dts),
        direction="high_signals_event",
        threshold_method=ThresholdMethod.MANUAL,
        manual_threshold=2.5,
    )
    cls = _build_class(precursors=(precursor,))
    with store.connection() as conn:
        signatures, _ = compute_signature(conn, cls, outcomes=occurrences, library_version=1)
    assert signatures[0].threshold_value == pytest.approx(2.5)


# -- combine_variables / build_co_occurrence_table ------------------------


def _make_signature_result(
    variable_id: str,
    *,
    hit_rate: float,
    threshold: float = 0.5,
    direction: str = "high_signals_event",
    confidence: float = 0.7,
) -> SignatureResult:
    return SignatureResult(
        class_id="c",
        variable_id=variable_id,
        library_version=1,
        definition_version=1,
        threshold_method="youden_j",
        threshold_value=threshold,
        direction=direction,  # type: ignore[arg-type]
        lead_time_window_days=180,
        pre_event_mean=None,
        pre_event_p25=None,
        pre_event_p50=None,
        pre_event_p75=None,
        baseline_mean=None,
        baseline_p25=None,
        baseline_p50=None,
        baseline_p75=None,
        hit_rate=hit_rate,
        false_positive_rate=0.1,
        sample_size_events=10,
        sample_size_baseline=100,
        confidence_score=confidence,
        computed_at=datetime(2026, 5, 14, tzinfo=UTC),
    )


def test_combine_no_signal_returns_zero() -> None:
    sig_a = _make_signature_result("a", hit_rate=0.8, threshold=0.5)
    sig_b = _make_signature_result("b", hit_rate=0.7, threshold=0.5)
    # No variables firing.
    result = combine_variables(
        signatures=(sig_a, sig_b),
        current_values={"a": 0.1, "b": 0.2},
        co_occurrence={},
    )
    assert isinstance(result, CombinedScore)
    assert result.score == 0.0
    assert result.method == "no_signal"
    assert result.contributing_variable_ids == ()


def test_combine_uses_co_occurrence_when_subset_match() -> None:
    sig_a = _make_signature_result("a", hit_rate=0.8, threshold=0.5)
    sig_b = _make_signature_result("b", hit_rate=0.7, threshold=0.5)
    co = {frozenset({"a", "b"}): 0.92}
    result = combine_variables(
        signatures=(sig_a, sig_b),
        current_values={"a": 0.7, "b": 0.6},
        co_occurrence=co,
    )
    assert result.method == "co_occurrence"
    assert result.score == pytest.approx(0.92)
    assert result.contributing_variable_ids == ("a", "b")


def test_combine_falls_back_to_geometric_mean() -> None:
    sig_a = _make_signature_result("a", hit_rate=0.8, threshold=0.5)
    sig_b = _make_signature_result("b", hit_rate=0.5, threshold=0.5)
    result = combine_variables(
        signatures=(sig_a, sig_b),
        current_values={"a": 0.7, "b": 0.6},
        co_occurrence={},  # no co-occurrence entry → fallback
    )
    assert result.method == "geometric_mean"
    expected = (0.8 * 0.5) ** 0.5
    assert result.score == pytest.approx(expected, rel=1e-6)
    assert result.contributing_variable_ids == ("a", "b")


def test_combine_low_signals_event_direction() -> None:
    """Variable with direction='low_signals_event' fires when current value <= threshold."""
    sig = _make_signature_result("a", hit_rate=0.8, threshold=0.3, direction="low_signals_event")
    result = combine_variables(
        signatures=(sig,),
        current_values={"a": 0.2},  # below threshold → fires
        co_occurrence={},
    )
    assert "a" in result.contributing_variable_ids


def test_combine_ignores_nan_current_values() -> None:
    sig = _make_signature_result("a", hit_rate=0.8, threshold=0.5)
    result = combine_variables(
        signatures=(sig,),
        current_values={"a": float("nan")},
        co_occurrence={},
    )
    assert result.method == "no_signal"


def test_build_co_occurrence_table_basic() -> None:
    """Two precursors that fire together in 100% of events get co-occurrence 1.0."""
    sig_a = _make_signature_result("a", hit_rate=0.9, threshold=2.0)
    sig_b = _make_signature_result("b", hit_rate=0.85, threshold=3.0)
    samples = {
        "a": {
            "pre_event": np.array([3.0, 3.5, 4.0]),  # all > threshold
            "baseline": np.array([1.0, 1.5]),
        },
        "b": {
            "pre_event": np.array([4.0, 5.0, 6.0]),  # all > threshold
            "baseline": np.array([1.0, 2.0]),
        },
    }
    table = build_co_occurrence_table(samples_by_variable=samples, signatures=(sig_a, sig_b))
    assert table.get(frozenset({"a", "b"})) == pytest.approx(1.0)


def test_build_co_occurrence_table_partial_co_occurrence() -> None:
    """Three events: a-only (1), b-only (1), a+b (1) → each subset = 1/3."""
    sig_a = _make_signature_result("a", hit_rate=0.6, threshold=2.0)
    sig_b = _make_signature_result("b", hit_rate=0.6, threshold=3.0)
    samples = {
        "a": {
            "pre_event": np.array([3.0, 1.0, 4.0]),  # event 0 and 2 fire a
            "baseline": np.array([1.0]),
        },
        "b": {
            "pre_event": np.array([2.0, 4.0, 5.0]),  # event 1 and 2 fire b
            "baseline": np.array([1.0]),
        },
    }
    table = build_co_occurrence_table(samples_by_variable=samples, signatures=(sig_a, sig_b))
    # event 0 → frozenset({"a"}), event 1 → frozenset({"b"}), event 2 → frozenset({"a","b"})
    assert table.get(frozenset({"a"})) == pytest.approx(1 / 3, rel=0.01)
    assert table.get(frozenset({"b"})) == pytest.approx(1 / 3, rel=0.01)
    assert table.get(frozenset({"a", "b"})) == pytest.approx(1 / 3, rel=0.01)


def test_build_co_occurrence_table_empty_signatures() -> None:
    table = build_co_occurrence_table(samples_by_variable={}, signatures=())
    assert table == {}
