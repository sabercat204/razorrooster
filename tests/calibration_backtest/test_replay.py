"""T-CB-018 — main replay loop tests.

Covers :func:`razor_rooster.calibration_backtest.engines.replay.run_backtest`
and its supporting helpers (:func:`iter_mapped_resolutions`,
:func:`polarity_correct`):

* **Recent-window guard (REQ-CB-RUN-002)** — ``until_ts == now()`` with
  ``allow_recent=False`` raises :class:`RecentWindowError` *before* any
  iterator/persistence work fires; ``allow_recent=True`` clears the
  guard and the synthesised :class:`BacktestRun` records
  ``allow_recent=True``; the boundary case ``until_ts == now() - 30d``
  is admitted (REQ-CB-FREEZE-001 boundary equality semantics).
* **Iterator pre-filter** — :func:`iter_mapped_resolutions` returns one
  row per (resolution, mapping) pair, sorted by ``resolution_ts``,
  filtered to in-scope ``venues``/``class_ids``, and excludes
  ``superseded_at``/``removed_at`` rows.
* **Integration: 3 resolved markets x 2 mapped classes = 6 attempts**
  (REQ-CB-REPLAY-001) — confirms the inner loop processes every JOIN
  row exactly once and routes each through the per-prediction
  pipeline.
* **Skip-reason routing** — invalidated resolution maps to
  ``invalid_resolution``; an :class:`InsufficientPrecursorData` from
  the evaluator maps to ``insufficient_precursor_data``; a
  :class:`NoPolarityError` from the resolver maps to
  ``no_polarity_resolution``.
* **Polarity-correct** — table-driven tests pin every
  ``(polarity, label)`` combination to the expected ``observed`` bit.

The integration tests stub :func:`freezer.freeze` and
:func:`evaluate_class_at_frozen_time` via monkeypatching the
``replay`` module's namespace (the same pattern T-CB-017's tests use).
This keeps the test corpus self-contained — no pattern_library
registry seeding, no signal_scanner posterior, no data_ingest canonical
tables — while still exercising the full T-CB-018 orchestration.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
import pytest

from razor_rooster.calibration_backtest.engines import polarity as polarity_module
from razor_rooster.calibration_backtest.engines import replay as replay_module
from razor_rooster.calibration_backtest.engines.freezer import FrozenState
from razor_rooster.calibration_backtest.engines.replay import (
    DEFAULT_RECENT_WINDOW_DAYS,
    MappedResolution,
    iter_mapped_resolutions,
    polarity_correct,
    run_backtest,
)
from razor_rooster.calibration_backtest.errors import (
    BacktestConfigError,
    InsufficientPrecursorData,
    RecentWindowError,
)
from razor_rooster.calibration_backtest.models import (
    BacktestStatus,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
    RunParameters,
    SkipReason,
)
from razor_rooster.mispricing_detector.persistence.schemas import (
    CLASS_MARKET_MAPPINGS_DDL,
    COMPARISON_CYCLES_DDL,
    COMPARISON_RESOLUTIONS_DDL,
    COMPARISONS_DDL,
)
from razor_rooster.polymarket_connector.persistence.schemas import (
    POLYMARKET_RESOLUTIONS_DDL,
)

# ---------------------------------------------------------------------------
# Constants and fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
"""Pinned wall-clock for deterministic recent-window guard testing."""


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """In-memory DuckDB connection with the upstream tables created.

    The replay loop reads from ``polymarket_resolutions`` (polymarket
    connector) and ``class_market_mappings``/``comparisons``/
    ``comparison_resolutions`` (mispricing_detector). The freezer's
    canonical-tables and ``sources`` registry are *not* created here —
    tests stub :func:`freezer.freeze` directly so the freezer's
    ``source_data_not_frozen`` short-circuit does not fire.
    """
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(POLYMARKET_RESOLUTIONS_DDL)
        connection.execute(CLASS_MARKET_MAPPINGS_DDL)
        connection.execute(COMPARISON_CYCLES_DDL)
        connection.execute(COMPARISONS_DDL)
        connection.execute(COMPARISON_RESOLUTIONS_DDL)
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Seed helpers (mirroring tests/calibration_backtest/test_polarity.py shapes)
# ---------------------------------------------------------------------------


def _insert_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str,
    resolution_ts: datetime,
    winning_outcome_label: str | None = "yes",
    invalidated: bool = False,
    record_id: str | None = None,
) -> None:
    """Insert one ``polymarket_resolutions`` row with the provenance prefix."""
    conn.execute(
        "INSERT INTO polymarket_resolutions ("
        "source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, source_payload_json, superseded_at, "
        "condition_id, winning_outcome_token_id, winning_outcome_label, "
        "resolution_ts, resolution_source, resolution_metadata, "
        "final_yes_price, final_no_price, total_volume_at_resolution, "
        "invalidated"
        ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, 'polymarket', "
        "NULL, NULL, NULL, NULL, ?)",
        [
            "polymarket",
            record_id or condition_id,
            resolution_ts,
            resolution_ts,
            "v1.0.0",
            "{}",
            condition_id,
            winning_outcome_label,
            resolution_ts,
            invalidated,
        ],
    )


def _insert_mapping(
    conn: duckdb.DuckDBPyConnection,
    *,
    mapping_id: str,
    class_id: str,
    condition_id: str,
    polarity_value: str = "aligned",
    venue: str = "polymarket",
    removed_at: datetime | None = None,
) -> None:
    """Insert one ``class_market_mappings`` row."""
    conn.execute(
        "INSERT INTO class_market_mappings ("
        "mapping_id, class_id, condition_id, mapping_type, "
        "mapping_confidence, polarity, mapped_by, mapped_at, "
        "removed_at, notes, venue"
        ") VALUES (?, ?, ?, 'direct', 'high', ?, 'op', ?, ?, NULL, ?)",
        [
            mapping_id,
            class_id,
            condition_id,
            polarity_value,
            datetime(2025, 1, 1, tzinfo=UTC),
            removed_at,
            venue,
        ],
    )


def _make_params(
    *,
    since_ts: datetime = datetime(2025, 1, 1, tzinfo=UTC),
    until_ts: datetime = datetime(2025, 12, 31, tzinfo=UTC),
    lag_days: int = 7,
    class_ids: tuple[str, ...] = ("cls-A",),
    sectors: tuple[str, ...] = (),
    venues: tuple[str, ...] = ("polymarket",),
    allow_recent: bool = False,
) -> RunParameters:
    """Build a :class:`RunParameters` with sensible defaults."""
    return RunParameters(
        since_ts=since_ts,
        until_ts=until_ts,
        lag_days=lag_days,
        class_ids=class_ids,
        sectors=sectors,
        venues=venues,
        allow_recent=allow_recent,
    )


def _stub_freeze(_conn: duckdb.DuckDBPyConnection, prediction_ts: datetime) -> FrozenState:
    """Freezer stub returning a successful :class:`FrozenState` for any input."""
    return FrozenState(
        source_publication_ts_boundary=prediction_ts,
        frozen_flag=True,
        registered_sources=frozenset({"fred"}),
    )


def _stub_evaluate(
    class_id: str,
    prediction_ts: datetime,
    frozen: FrozenState,
    *,
    store: Any,
    library_version: int | None = None,
    min_support: int = 1,
    n_samples: int | None = None,
    co_occurrence_correction: float = 0.0,
) -> tuple[float, dict[str, Any]]:
    """``evaluate_class_at_frozen_time`` stub returning a fixed posterior + trace."""
    trace = {
        "class": {
            "class_id": class_id,
            "definition_version": 3,
        },
        "data_as_of": prediction_ts.isoformat(),
        "library_version": library_version or 1,
    }
    return 0.42, trace


@pytest.fixture
def patched_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the replay module's freezer + evaluator with deterministic stubs."""
    monkeypatch.setattr(replay_module.freezer_module, "freeze", _stub_freeze)
    monkeypatch.setattr(replay_module, "evaluate_class_at_frozen_time", _stub_evaluate)


# ---------------------------------------------------------------------------
# Recent-window guard (REQ-CB-RUN-002)
# ---------------------------------------------------------------------------


def test_recent_window_guard_raises_when_until_ts_is_now_and_not_allow_recent(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``until_ts=now()`` with default ``allow_recent=False`` raises before work."""
    params = _make_params(
        since_ts=_NOW - timedelta(days=90),
        until_ts=_NOW,
        allow_recent=False,
    )
    with pytest.raises(RecentWindowError) as exc_info:
        run_backtest(params, conn=conn, store=object(), now=_NOW)
    err = exc_info.value
    assert err.until_ts == params.until_ts
    assert err.cutoff == _NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS)
    assert err.recommended_until_ts == err.cutoff


def test_recent_window_guard_does_not_query_resolutions(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Guard fires *before* any SQL/iteration; no rows materialised even if seeded."""
    _insert_resolution(
        conn,
        condition_id="cond-skip",
        resolution_ts=_NOW - timedelta(days=5),
    )
    _insert_mapping(conn, mapping_id="m1", class_id="cls-A", condition_id="cond-skip")
    params = _make_params(
        since_ts=_NOW - timedelta(days=90),
        until_ts=_NOW - timedelta(days=1),
        allow_recent=False,
    )
    with pytest.raises(RecentWindowError):
        run_backtest(params, conn=conn, store=object(), now=_NOW)


def test_allow_recent_clears_recent_window_guard(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """``allow_recent=True`` proceeds and the run row records ``allow_recent=True``."""
    params = _make_params(
        since_ts=_NOW - timedelta(days=90),
        until_ts=_NOW,
        allow_recent=True,
    )
    result = run_backtest(params, conn=conn, store=object(), now=_NOW)
    assert result.run.allow_recent is True
    assert result.run.status is BacktestStatus.COMPLETE


def test_recent_window_boundary_equality_admitted(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """``until_ts == now() - 30d`` exactly is admitted (boundary equality)."""
    boundary = _NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS)
    params = _make_params(
        since_ts=_NOW - timedelta(days=180),
        until_ts=boundary,
        allow_recent=False,
    )
    # No resolutions seeded — the guard should pass and the loop yields zero
    # predictions, but the run still completes.
    result = run_backtest(params, conn=conn, store=object(), now=_NOW)
    assert result.run.status is BacktestStatus.COMPLETE
    assert result.predictions == ()


# ---------------------------------------------------------------------------
# iter_mapped_resolutions
# ---------------------------------------------------------------------------


def test_iter_mapped_resolutions_yields_one_row_per_resolution_mapping_pair(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """3 resolutions x 2 mappings = 6 yielded rows (REQ-CB-REPLAY-001)."""
    base_ts = datetime(2025, 6, 1, tzinfo=UTC)
    for index in range(3):
        condition_id = f"cond-{index}"
        _insert_resolution(
            conn,
            condition_id=condition_id,
            resolution_ts=base_ts + timedelta(days=index),
        )
        _insert_mapping(
            conn,
            mapping_id=f"m-A-{index}",
            class_id="cls-A",
            condition_id=condition_id,
        )
        _insert_mapping(
            conn,
            mapping_id=f"m-B-{index}",
            class_id="cls-B",
            condition_id=condition_id,
        )
    rows = list(
        iter_mapped_resolutions(
            conn,
            since_ts=base_ts - timedelta(days=1),
            until_ts=base_ts + timedelta(days=10),
            venues=("polymarket",),
            class_ids=("cls-A", "cls-B"),
        )
    )
    assert len(rows) == 6
    # ASC ordering by resolution_ts.
    timestamps = [row.resolution_ts for row in rows]
    assert timestamps == sorted(timestamps)
    # Each resolution paired with both classes.
    pairs = {(row.condition_id, row.class_id) for row in rows}
    assert pairs == {(f"cond-{i}", c) for i in range(3) for c in ("cls-A", "cls-B")}


def test_iter_mapped_resolutions_excludes_removed_mappings(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """``cm.removed_at IS NOT NULL`` rows are excluded by the JOIN filter."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(conn, condition_id="cond-1", resolution_ts=ts)
    _insert_mapping(
        conn,
        mapping_id="m-active",
        class_id="cls-A",
        condition_id="cond-1",
    )
    _insert_mapping(
        conn,
        mapping_id="m-removed",
        class_id="cls-A",
        condition_id="cond-1",
        polarity_value="inverted",
        removed_at=datetime(2025, 5, 1, tzinfo=UTC),
    )
    rows = list(
        iter_mapped_resolutions(
            conn,
            since_ts=ts - timedelta(days=1),
            until_ts=ts + timedelta(days=1),
            venues=("polymarket",),
            class_ids=("cls-A",),
        )
    )
    assert len(rows) == 1
    assert rows[0].mapping_polarity == "aligned"


def test_iter_mapped_resolutions_filters_by_venue(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Mappings on a different venue are not yielded."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(conn, condition_id="cond-1", resolution_ts=ts)
    _insert_mapping(
        conn,
        mapping_id="m-poly",
        class_id="cls-A",
        condition_id="cond-1",
        venue="polymarket",
    )
    _insert_mapping(
        conn,
        mapping_id="m-kalshi",
        class_id="cls-A",
        condition_id="cond-1",
        venue="kalshi",
    )
    rows = list(
        iter_mapped_resolutions(
            conn,
            since_ts=ts - timedelta(days=1),
            until_ts=ts + timedelta(days=1),
            venues=("polymarket",),
            class_ids=("cls-A",),
        )
    )
    assert [row.venue for row in rows] == ["polymarket"]


def test_iter_mapped_resolutions_rejects_empty_inputs(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Empty ``venues`` / ``class_ids`` raise :class:`BacktestConfigError`."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    with pytest.raises(BacktestConfigError, match="venues must be non-empty"):
        list(
            iter_mapped_resolutions(
                conn,
                since_ts=ts,
                until_ts=ts + timedelta(days=1),
                venues=(),
                class_ids=("cls-A",),
            )
        )
    with pytest.raises(BacktestConfigError, match="class_ids must be non-empty"):
        list(
            iter_mapped_resolutions(
                conn,
                since_ts=ts,
                until_ts=ts + timedelta(days=1),
                venues=("polymarket",),
                class_ids=(),
            )
        )


# ---------------------------------------------------------------------------
# polarity_correct
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("polarity_value", "label", "expected"),
    [
        ("aligned", "yes", 1.0),
        ("aligned", "no", 0.0),
        ("inverted", "yes", 0.0),
        ("inverted", "no", 1.0),
        ("direct", "yes", 1.0),
        ("forward", "yes", 1.0),
    ],
)
def test_polarity_correct_table(polarity_value: str, label: str, expected: float) -> None:
    """Every (polarity, label) tuple maps to the expected ``observed`` bit."""
    assert polarity_correct(label, polarity_value) == expected


def test_polarity_correct_none_label_returns_zero() -> None:
    """A missing ``winning_outcome_label`` is mapped to ``0.0`` defensively."""
    assert polarity_correct(None, "aligned") == 0.0


def test_polarity_correct_unknown_polarity_raises() -> None:
    """Unknown polarity strings surface a typed configuration error."""
    with pytest.raises(BacktestConfigError, match="unrecognised polarity_value"):
        polarity_correct("yes", "sideways")


# ---------------------------------------------------------------------------
# Integration: 3 resolutions x 2 classes -> 6 prediction attempts
# ---------------------------------------------------------------------------


def test_run_backtest_integration_three_resolutions_two_classes_six_predictions(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """3 resolved markets x 2 mapped classes = 6 prediction attempts (REQ-CB-REPLAY-001)."""
    base_ts = datetime(2025, 6, 1, tzinfo=UTC)
    for index in range(3):
        condition_id = f"cond-{index}"
        _insert_resolution(
            conn,
            condition_id=condition_id,
            resolution_ts=base_ts + timedelta(days=index),
            winning_outcome_label="yes",
        )
        _insert_mapping(
            conn,
            mapping_id=f"m-A-{index}",
            class_id="cls-A",
            condition_id=condition_id,
        )
        _insert_mapping(
            conn,
            mapping_id=f"m-B-{index}",
            class_id="cls-B",
            condition_id=condition_id,
            polarity_value="inverted",
        )

    params = _make_params(
        since_ts=base_ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
        class_ids=("cls-A", "cls-B"),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=object(),
        now=_NOW,
        max_workers=1,
    )
    assert len(result.predictions) == 6
    assert all(p.status is PredictionStatus.SCORED for p in result.predictions)
    # cls-A (aligned, yes) -> observed=1.0; cls-B (inverted, yes) -> observed=0.0.
    by_class = {p.class_id: p for p in result.predictions}
    assert by_class["cls-A"].observed == 1.0
    assert by_class["cls-B"].observed == 0.0
    assert by_class["cls-A"].polarity is PolarityValue.FORWARD
    assert by_class["cls-B"].polarity is PolarityValue.INVERTED
    # Tier 2 fallback was used (no comparison_resolutions seeded).
    assert all(
        p.polarity_source is PolaritySource.CURRENT_MAPPING_FALLBACK for p in result.predictions
    )
    assert all(p.mapping_mismatch_warning is True for p in result.predictions)
    assert result.run.predictions_total == 6
    assert result.run.predictions_scored == 6
    assert result.run.predictions_skipped == 0
    assert result.run.fallback_polarity_count == 6
    # Trace dict is captured for every scored prediction.
    assert set(result.traces.keys()) == {p.prediction_id for p in result.predictions}


def test_run_backtest_skips_invalidated_resolution(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """``polymarket_resolutions.invalidated=TRUE`` -> ``invalid_resolution`` skip."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(
        conn,
        condition_id="cond-bad",
        resolution_ts=ts,
        invalidated=True,
    )
    _insert_mapping(
        conn,
        mapping_id="m-1",
        class_id="cls-A",
        condition_id="cond-bad",
    )
    params = _make_params(
        since_ts=ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=object(),
        now=_NOW,
        max_workers=1,
    )
    assert len(result.predictions) == 1
    prediction = result.predictions[0]
    assert prediction.status is PredictionStatus.SKIPPED
    assert prediction.skip_reason is SkipReason.INVALID_RESOLUTION
    assert result.traces == {}


def test_run_backtest_routes_insufficient_data_skip(
    conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """:class:`InsufficientPrecursorData` -> ``insufficient_precursor_data`` skip."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(conn, condition_id="cond-1", resolution_ts=ts)
    _insert_mapping(conn, mapping_id="m-1", class_id="cls-A", condition_id="cond-1")

    monkeypatch.setattr(replay_module.freezer_module, "freeze", _stub_freeze)

    def _raise_insufficient(*_args: Any, **_kwargs: Any) -> tuple[float, dict[str, Any]]:
        raise InsufficientPrecursorData("synthetic")

    monkeypatch.setattr(replay_module, "evaluate_class_at_frozen_time", _raise_insufficient)

    params = _make_params(
        since_ts=ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=object(),
        now=_NOW,
        max_workers=1,
    )
    assert len(result.predictions) == 1
    prediction = result.predictions[0]
    assert prediction.status is PredictionStatus.SKIPPED
    assert prediction.skip_reason is SkipReason.INSUFFICIENT_PRECURSOR_DATA


def test_run_backtest_routes_no_polarity_skip(
    conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver that never resolves -> ``no_polarity_resolution`` skip."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(conn, condition_id="cond-1", resolution_ts=ts)
    _insert_mapping(conn, mapping_id="m-1", class_id="cls-A", condition_id="cond-1")

    monkeypatch.setattr(replay_module.freezer_module, "freeze", _stub_freeze)
    # Stub the polarity resolver to always raise so the skip routes via Tier 3.
    from razor_rooster.calibration_backtest.errors import NoPolarityError

    def _no_polarity(*_args: Any, **_kwargs: Any) -> tuple[str, str]:
        raise NoPolarityError(prediction_ts=ts, condition_id="cond-1", class_id="cls-A")

    monkeypatch.setattr(replay_module.polarity_module, "resolve", _no_polarity)
    # Ensure evaluator is not reached in this path.
    monkeypatch.setattr(replay_module, "evaluate_class_at_frozen_time", _stub_evaluate)

    params = _make_params(
        since_ts=ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=object(),
        now=_NOW,
        max_workers=1,
    )
    assert len(result.predictions) == 1
    prediction = result.predictions[0]
    assert prediction.status is PredictionStatus.SKIPPED
    assert prediction.skip_reason is SkipReason.NO_POLARITY_RESOLUTION


def test_run_backtest_routes_source_data_not_frozen_skip(
    conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``freezer.freeze`` returning ``None`` -> ``source_data_not_frozen`` skip."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(conn, condition_id="cond-1", resolution_ts=ts)
    _insert_mapping(conn, mapping_id="m-1", class_id="cls-A", condition_id="cond-1")

    def _no_freeze(*_args: Any, **_kwargs: Any) -> FrozenState | None:
        return None

    monkeypatch.setattr(replay_module.freezer_module, "freeze", _no_freeze)
    monkeypatch.setattr(replay_module, "evaluate_class_at_frozen_time", _stub_evaluate)

    params = _make_params(
        since_ts=ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=object(),
        now=_NOW,
        max_workers=1,
    )
    assert len(result.predictions) == 1
    prediction = result.predictions[0]
    assert prediction.status is PredictionStatus.SKIPPED
    assert prediction.skip_reason is SkipReason.SOURCE_DATA_NOT_FROZEN


def test_run_backtest_routes_insufficient_lag_skip(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """A resolution within ``lag_days`` of ``until_ts`` would yield the lag skip
    only when the derived ``prediction_ts`` falls before ``since_ts``; in
    practice the lag floor is a strict ``>= lag_days`` predicate so this
    path is exercised when the test pins ``lag_days`` higher than the gap
    between ``resolution_ts`` and ``prediction_ts``. We achieve that by
    monkeypatching :func:`validate_lag` to always return ``False``.
    """
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(conn, condition_id="cond-1", resolution_ts=ts)
    _insert_mapping(conn, mapping_id="m-1", class_id="cls-A", condition_id="cond-1")

    import razor_rooster.calibration_backtest.engines.replay as replay_internal

    def _always_false(*_args: Any, **_kwargs: Any) -> bool:
        return False

    # Patch the bound symbol in the replay module so the loop short-circuits.
    replay_internal.validate_lag = _always_false  # type: ignore[assignment]
    try:
        params = _make_params(
            since_ts=ts - timedelta(days=10),
            until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
        )
        result = run_backtest(
            params,
            conn=conn,
            store=object(),
            now=_NOW,
            max_workers=1,
        )
    finally:
        # Restore the original so subsequent tests are not contaminated.
        from razor_rooster.calibration_backtest.engines.lag import validate_lag

        replay_internal.validate_lag = validate_lag  # type: ignore[assignment]

    assert len(result.predictions) == 1
    prediction = result.predictions[0]
    assert prediction.status is PredictionStatus.SKIPPED
    assert prediction.skip_reason is SkipReason.INSUFFICIENT_LAG


def test_run_backtest_uses_comparison_resolutions_polarity_when_available(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """Tier 1 hit -> ``polarity_source=COMPARISON_RESOLUTIONS`` and no warning."""
    ts = datetime(2025, 6, 1, tzinfo=UTC)
    _insert_resolution(conn, condition_id="cond-1", resolution_ts=ts)
    _insert_mapping(
        conn,
        mapping_id="m-1",
        class_id="cls-A",
        condition_id="cond-1",
        polarity_value="aligned",
    )
    # Seed Tier 1 row: comparison_resolutions row with a future
    # ``resolution_ts`` relative to ``prediction_ts = ts - 7d``.
    conn.execute(
        "INSERT INTO comparison_cycles ("
        "cycle_id, started_at, completed_at, comparisons_total, "
        "surfaced_count, suppressed_breakdown, library_version_at_cycle, "
        "scan_id_consumed, error_summary"
        ") VALUES ('cycle-1', ?, NULL, 0, 0, '{}', 1, 'scan-1', NULL)",
        [datetime(2025, 1, 1, tzinfo=UTC)],
    )
    conn.execute(
        "INSERT INTO comparisons ("
        "comparison_id, cycle_id, mapping_id, class_id, condition_id, "
        "outcome_token_id, polarity, scan_id, model_probability, "
        "model_ci_lower, model_ci_upper, computed_at, venue"
        ") VALUES ('cmp-1', 'cycle-1', 'm-1', 'cls-A', 'cond-1', 'tok', "
        "'aligned', 'scan-1', 0.5, 0.4, 0.6, ?, 'polymarket')",
        [datetime(2025, 1, 1, tzinfo=UTC)],
    )
    conn.execute(
        "INSERT INTO comparison_resolutions ("
        "comparison_id, condition_id, resolution_outcome, resolution_ts, "
        "model_probability_at_comparison, market_probability_at_comparison, "
        "polarity_at_comparison, outcome_observed, linked_at, venue"
        ") VALUES ('cmp-1', 'cond-1', 'yes', ?, 0.5, 0.5, 'aligned', 1, ?, 'polymarket')",
        [ts, ts],
    )

    params = _make_params(
        since_ts=ts - timedelta(days=10),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=object(),
        now=_NOW,
        max_workers=1,
    )
    assert len(result.predictions) == 1
    prediction = result.predictions[0]
    assert prediction.polarity_source is PolaritySource.COMPARISON_RESOLUTIONS
    assert prediction.mapping_mismatch_warning is False
    assert result.run.fallback_polarity_count == 0


# ---------------------------------------------------------------------------
# MappedResolution dataclass sanity
# ---------------------------------------------------------------------------


def test_mapped_resolution_is_frozen() -> None:
    """The yielded row dataclass is immutable so the iterator cannot be tampered with."""
    row = MappedResolution(
        condition_id="x",
        resolution_ts=_NOW,
        invalidated=False,
        winning_outcome_label="yes",
        class_id="cls",
        mapping_polarity="aligned",
        venue="polymarket",
    )
    with pytest.raises(AttributeError):
        row.condition_id = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Polarity-source enum mapping (smoke test for _polarity_source_enum coverage)
# ---------------------------------------------------------------------------


def test_polarity_source_enum_maps_known_sentinels() -> None:
    """Internal mapper rejects unknown sentinels and accepts the two production ones."""
    from razor_rooster.calibration_backtest.engines.replay import _polarity_source_enum

    assert (
        _polarity_source_enum(polarity_module.SOURCE_COMPARISON_RESOLUTIONS)
        is PolaritySource.COMPARISON_RESOLUTIONS
    )
    assert (
        _polarity_source_enum(polarity_module.SOURCE_CURRENT_MAPPING_FALLBACK)
        is PolaritySource.CURRENT_MAPPING_FALLBACK
    )
    with pytest.raises(BacktestConfigError, match="unrecognised polarity source"):
        _polarity_source_enum("magic")
