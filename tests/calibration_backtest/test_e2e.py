"""T-CB-053 — end-to-end smoke + idempotent re-run + golden-data audit.

This module ships three concentric layers of acceptance coverage:

* **End-to-end smoke** (``test_e2e_smoke_*``): seed all required upstream
  tables (``polymarket_resolutions``, ``class_market_mappings``,
  ``comparison_resolutions``, the four data_ingest canonical tables),
  invoke :func:`run_backtest` with default parameters, and assert
  ``status='complete'`` plus a non-NULL ``summary_json`` that hydrates
  into a :class:`ScoreSummary` via
  :func:`razor_rooster.calibration_backtest.engines.scoring.aggregate_run_summary`.
  Exercises precursor freezing, polarity resolution, Brier aggregation
  (non-``None`` ``overall_brier`` on the populated path), and
  reliability bins (REQ-CB-RUN-004, REQ-CB-SCORE-004).

* **Cache fast-path** (``test_e2e_cache_*``): invoke
  :func:`run_backtest` twice with identical params on the same
  persistence connection; the second call must short-circuit through
  :func:`persistence.fetch_run_status` (operator decision Q2,
  2026-06-01) and return the cached :class:`ReplayResult` in well under
  one second. The test spies on ``fetch_run_status`` to confirm the
  warm-path fired.

* **Golden-data audit** (``test_golden_*``): load the committed 90-day
  fixture (``fixtures/golden_90day_corpus.duckdb``), invoke
  :func:`run_backtest`, and compare the aggregated :class:`ScoreSummary`
  values against the recorded ``fixtures/golden_90day_reference.json``
  values within :func:`numpy.isclose` tolerance.

* **Zero-scored sub-test** (``test_e2e_zero_scored_*``): exercises the
  Q3 path — every prediction skipped, ``aggregate_run_summary`` returns
  ``overall_brier=None`` and emits :class:`SkippedRunWarning`.

These tests sit alongside the more focused per-layer test modules
(``test_replay.py``, ``test_replay_persistence.py``, ``test_score_summary.py``)
and intentionally exercise the integrated orchestration path, including
the persistence-layer side effects, the cache fast-path, and the
aggregator's Q3 zero-scored handling.
"""

from __future__ import annotations

import json
import time
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import duckdb
import pytest

from razor_rooster.calibration_backtest.engines import replay as replay_module
from razor_rooster.calibration_backtest.engines.freezer import FrozenState
from razor_rooster.calibration_backtest.engines.replay import (
    DEFAULT_RECENT_WINDOW_DAYS,
    run_backtest,
)
from razor_rooster.calibration_backtest.engines.scoring import aggregate_run_summary
from razor_rooster.calibration_backtest.errors import SkippedRunWarning
from razor_rooster.calibration_backtest.models import (
    BacktestStatus,
    PredictionStatus,
    RunParameters,
    ScoreSummary,
)
from razor_rooster.calibration_backtest.persistence import operations
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
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

if TYPE_CHECKING:
    from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore


_NOW: datetime = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
"""Pinned wall-clock so the recent-window guard is deterministic."""

_FAKE_STORE: DuckDBStore = cast("DuckDBStore", object())
"""Sentinel store passed to :func:`run_backtest` in the stubbed tests.

The orchestration tests stub :func:`evaluate_class_at_frozen_time` so
the ``store`` argument is never dereferenced. Mirrors the pattern in
``tests/calibration_backtest/test_replay_persistence.py``.
"""

_FIXTURES_DIR: Path = Path(__file__).resolve().parent / "fixtures"
"""Directory that holds the committed golden corpus + reference values."""


# ---------------------------------------------------------------------------
# Connection fixtures (upstream + calibration_backtest schemas on one conn)
# ---------------------------------------------------------------------------


def _build_e2e_conn() -> duckdb.DuckDBPyConnection:
    """Return an in-memory DuckDB conn with every required schema applied."""
    connection = duckdb.connect(":memory:")
    connection.execute(POLYMARKET_RESOLUTIONS_DDL)
    connection.execute(CLASS_MARKET_MAPPINGS_DDL)
    connection.execute(COMPARISON_CYCLES_DDL)
    connection.execute(COMPARISONS_DDL)
    connection.execute(COMPARISON_RESOLUTIONS_DDL)
    run_pending_calibration_backtest_migrations(connection)
    return connection


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield an in-memory DuckDB conn with every required schema applied."""
    connection = _build_e2e_conn()
    try:
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Pipeline stubs (no pattern_library / signal_scanner involvement)
# ---------------------------------------------------------------------------


def _stub_freeze(_conn: duckdb.DuckDBPyConnection, prediction_ts: datetime) -> FrozenState:
    """Always-frozen stub so the freezer's source-data short-circuit doesn't fire."""
    return FrozenState(
        source_publication_ts_boundary=prediction_ts,
        frozen_flag=True,
        registered_sources=frozenset({"fred"}),
    )


def _make_stub_evaluate(*, model_p: float = 0.42) -> Any:
    """Return an :func:`evaluate_class_at_frozen_time` stub yielding *model_p*.

    The trace dict mirrors the shape produced by
    :func:`signal_scanner.engines.trace.build_trace` so the persistence
    path's trace decoder round-trip stays honest.
    """

    def _evaluate(
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
        trace: dict[str, Any] = {
            "class": {
                "class_id": class_id,
                "definition_version": 3,
            },
            "data_as_of": prediction_ts.isoformat(),
            "library_version": library_version or 1,
            "posterior": {"mean": model_p, "ci_lower": model_p - 0.05, "ci_upper": model_p + 0.05},
        }
        return model_p, trace

    return _evaluate


@pytest.fixture
def patched_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the freezer + class evaluator so no upstream wiring is required."""
    monkeypatch.setattr(replay_module.freezer_module, "freeze", _stub_freeze)
    monkeypatch.setattr(
        replay_module, "evaluate_class_at_frozen_time", _make_stub_evaluate(model_p=0.42)
    )


# ---------------------------------------------------------------------------
# Seed helpers — mirror the wire-shape used by tests/test_replay_persistence.py
# ---------------------------------------------------------------------------


def _insert_resolution(
    target: duckdb.DuckDBPyConnection,
    *,
    condition_id: str,
    resolution_ts: datetime,
    winning_outcome_label: str | None = "yes",
    invalidated: bool = False,
) -> None:
    target.execute(
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
            condition_id,
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
    target: duckdb.DuckDBPyConnection,
    *,
    mapping_id: str,
    class_id: str,
    condition_id: str,
    polarity_value: str = "aligned",
    venue: str = "polymarket",
    removed_at: datetime | None = None,
) -> None:
    target.execute(
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
    until_ts: datetime | None = None,
    lag_days: int = 7,
    class_ids: tuple[str, ...] = ("cls-A",),
    sectors: tuple[str, ...] = (),
    venues: tuple[str, ...] = ("polymarket",),
    allow_recent: bool = False,
) -> RunParameters:
    """Build a :class:`RunParameters` mirroring the CLI ``run`` command."""
    if until_ts is None:
        until_ts = _NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1)
    return RunParameters(
        since_ts=since_ts,
        until_ts=until_ts,
        lag_days=lag_days,
        class_ids=class_ids,
        sectors=sectors,
        venues=venues,
        allow_recent=allow_recent,
    )


def _seed_corpus(
    target: duckdb.DuckDBPyConnection,
    *,
    sectors: tuple[str, ...] = ("public_health", "economics"),
    resolutions_per_sector: int = 5,
    base_ts: datetime = datetime(2025, 6, 1, tzinfo=UTC),
) -> tuple[str, ...]:
    """Seed *resolutions_per_sector* mapped resolutions per sector.

    The mappings are 1:1 (one mapping per resolution); polarity is
    ``aligned``; outcomes alternate ``yes``/``no`` so the Brier
    contributions span the prediction range.
    """
    class_ids: list[str] = []
    for sector_index, _sector in enumerate(sectors):
        class_id = f"cls-{sector_index}"
        class_ids.append(class_id)
        for resolution_index in range(resolutions_per_sector):
            condition_id = f"cond-{sector_index}-{resolution_index}"
            outcome = "yes" if resolution_index % 2 == 0 else "no"
            _insert_resolution(
                target,
                condition_id=condition_id,
                resolution_ts=base_ts + timedelta(days=sector_index * 30 + resolution_index),
                winning_outcome_label=outcome,
            )
            _insert_mapping(
                target,
                mapping_id=f"m-{sector_index}-{resolution_index}",
                class_id=class_id,
                condition_id=condition_id,
            )
    return tuple(class_ids)


# ---------------------------------------------------------------------------
# E2E smoke — full orchestration produces a complete run row
# ---------------------------------------------------------------------------


def test_e2e_smoke_complete_run_with_summary(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """A complete run row with a hydratable summary lands after a full call."""
    class_ids = _seed_corpus(conn, sectors=("public_health", "economics"))
    params = _make_params(
        since_ts=datetime(2025, 1, 1, tzinfo=UTC),
        class_ids=class_ids,
    )

    result = run_backtest(
        params,
        conn=conn,
        store=_FAKE_STORE,
        now=_NOW,
        max_workers=1,
        persistence_conn=conn,
    )

    assert result.run.status is BacktestStatus.COMPLETE
    assert result.run.predictions_total == len(result.predictions)

    persisted = operations.fetch_run(conn, result.run.run_id)
    assert persisted is not None
    assert persisted.status is BacktestStatus.COMPLETE
    assert persisted.summary_json is not None

    summary = aggregate_run_summary(
        conn,
        result.run.run_id,
        bin_count_global=10,
        bin_count_per_sector={},
    )
    # Populated path: at least one row scored, so overall_brier is set.
    assert summary.overall_brier is not None
    assert 0.0 <= summary.overall_brier <= 1.0
    assert summary.fallback_polarity_count >= 0
    # Reliability diagrams covered every sector with at least one scored row.
    assert set(summary.reliability_diagrams).issubset(
        {p.sector for p in result.predictions if p.status is PredictionStatus.SCORED}
    )


# ---------------------------------------------------------------------------
# E2E cache fast-path — second call short-circuits via fetch_run_status
# ---------------------------------------------------------------------------


def test_e2e_cache_fast_path_short_circuits(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second invocation with identical params returns the cached result.

    The persistence helper :func:`fetch_run_status` is wrapped with a
    spy so we can verify the warm-path fired (operator decision Q2,
    2026-06-01). Latency is asserted under one second so a regression
    that accidentally re-runs the resolution iterator surfaces as a
    test failure rather than a silent slowdown.
    """
    class_ids = _seed_corpus(conn, sectors=("public_health", "economics"))
    params = _make_params(
        since_ts=datetime(2025, 1, 1, tzinfo=UTC),
        class_ids=class_ids,
    )

    # Cold path — first call populates the cache.
    cold = run_backtest(
        params,
        conn=conn,
        store=_FAKE_STORE,
        now=_NOW,
        max_workers=1,
        persistence_conn=conn,
    )

    # Spy on fetch_run_status so we can confirm the warm-path fired.
    real_fetch = operations.fetch_run_status
    calls: list[str] = []

    def _spy_fetch(target: duckdb.DuckDBPyConnection, run_id: str) -> BacktestStatus | None:
        calls.append(run_id)
        return real_fetch(target, run_id)

    # ``replay.persistence`` is ``operations`` — patching either alias
    # is equivalent because ``setattr`` mutates the underlying module
    # object. We patch via the ``operations`` import to keep the patch
    # site narrow and to avoid a string-based ``setattr`` lookup that
    # mypy cannot type-check.
    monkeypatch.setattr(operations, "fetch_run_status", _spy_fetch)

    start = time.perf_counter()
    warm = run_backtest(
        params,
        conn=conn,
        store=_FAKE_STORE,
        now=_NOW,
        max_workers=1,
        persistence_conn=conn,
    )
    elapsed = time.perf_counter() - start

    assert calls == [cold.run.run_id], (
        "cache fast-path must call fetch_run_status exactly once for the "
        f"cached run_id; got {calls!r}"
    )
    assert elapsed < 1.0, f"cache fast-path must return in under one second, took {elapsed:.3f}s"

    # Cached return preserves the cold-path shape: same run_id, same
    # status, identical prediction count.
    assert warm.run.run_id == cold.run.run_id
    assert warm.run.status is BacktestStatus.COMPLETE
    assert len(warm.predictions) == len(cold.predictions)
    assert set(warm.traces) == set(cold.traces)


# ---------------------------------------------------------------------------
# E2E zero-scored — every prediction skipped triggers SkippedRunWarning
# ---------------------------------------------------------------------------


def test_e2e_zero_scored_run_emits_skipped_warning(
    conn: duckdb.DuckDBPyConnection,
    patched_pipeline: None,
) -> None:
    """A run where every prediction skips returns ``overall_brier=None``.

    Seed two resolutions both flagged ``invalidated=True`` so the
    replay loop routes both to ``skip_reason='invalid_resolution'``.
    The persisted run row still transitions to COMPLETE (the orchestrator
    treats every-prediction-skipped as a successful, non-degenerate
    completion); :func:`aggregate_run_summary` then emits
    :class:`SkippedRunWarning` and the :class:`ScoreSummary` carries
    ``overall_brier=None`` (operator decision Q3, 2026-06-01).
    """
    base_ts = datetime(2025, 6, 1, tzinfo=UTC)
    for index in range(2):
        condition_id = f"skip-{index}"
        _insert_resolution(
            conn,
            condition_id=condition_id,
            resolution_ts=base_ts + timedelta(days=index),
            invalidated=True,
        )
        _insert_mapping(
            conn,
            mapping_id=f"sm-{index}",
            class_id="cls-A",
            condition_id=condition_id,
        )
    params = _make_params(
        since_ts=base_ts - timedelta(days=10),
        class_ids=("cls-A",),
    )
    result = run_backtest(
        params,
        conn=conn,
        store=_FAKE_STORE,
        now=_NOW,
        max_workers=1,
        persistence_conn=conn,
    )
    assert result.run.status is BacktestStatus.COMPLETE
    assert all(p.status is PredictionStatus.SKIPPED for p in result.predictions)

    with pytest.warns(SkippedRunWarning):
        summary = aggregate_run_summary(
            conn,
            result.run.run_id,
            bin_count_global=10,
            bin_count_per_sector={},
        )
    assert summary.overall_brier is None
    assert summary.per_sector_brier == {}
    assert summary.fallback_polarity_count == 0


# ---------------------------------------------------------------------------
# Golden-data audit — load committed corpus, compare aggregates within tol
# ---------------------------------------------------------------------------


def _golden_corpus_path() -> Path:
    return _FIXTURES_DIR / "golden_90day_corpus.duckdb"


def _golden_reference_path() -> Path:
    return _FIXTURES_DIR / "golden_90day_reference.json"


@pytest.fixture
def golden_corpus_conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield a DuckDB connection on a copy of the committed golden corpus.

    The fixture file is opened read-only via a temp-directory copy so a
    rogue test run cannot mutate the committed corpus on disk; mirrors
    the pattern used by ``tests/data_ingest/test_persistence_*``.
    """
    src = _golden_corpus_path()
    if not src.exists():
        pytest.skip(
            "golden corpus not yet built; run "
            "tests/calibration_backtest/fixtures/build_golden_corpus.py"
        )
    dst = tmp_path / src.name
    dst.write_bytes(src.read_bytes())
    connection = duckdb.connect(str(dst))
    try:
        yield connection
    finally:
        connection.close()


_GOLDEN_SECTOR_MODEL_P: dict[str, float] = {
    "public_health": 0.15,
    "economics": 0.35,
    "politics": 0.55,
    "climate": 0.72,
    "technology": 0.88,
}
"""Sector-specific predicted probabilities used by both the corpus
builder and this audit test. The two surfaces must keep this mapping
in sync — the reference JSON's Brier values are derived from these
exact predictions, so any drift here invalidates the corpus."""


def _golden_stub_evaluate(
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
    """Sector-aware evaluator stub used by the golden audit re-run.

    Mirrors the same stub used by the corpus builder
    (``fixtures/build_golden_corpus.py``). The class_id encodes the
    sector via the ``cls-<sector>`` prefix so the per-sector Brier
    expectations stay aligned with the reference JSON.
    """
    sector = class_id.removeprefix("cls-")
    model_p = _GOLDEN_SECTOR_MODEL_P.get(sector, 0.5)
    trace: dict[str, Any] = {
        "class": {
            "class_id": class_id,
            "definition_version": 1,
        },
        "data_as_of": prediction_ts.isoformat(),
        "library_version": library_version or 1,
        "posterior": {"mean": model_p, "ci_lower": model_p - 0.05, "ci_upper": model_p + 0.05},
    }
    return model_p, trace


def test_golden_90day_corpus_matches_reference(
    golden_corpus_conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Golden audit: re-run on the committed corpus, compare to reference.

    The reference JSON carries the per-sector Brier scores recorded
    when the corpus was built. Re-running :func:`run_backtest` against
    the same upstream rows must reproduce the same aggregates within
    :func:`numpy.isclose(atol=1e-6)` — the canonical determinism gate
    for the calibration audit (REQ-CB-RUN-004, REQ-CB-SCORE-004).
    """
    import numpy as np

    monkeypatch.setattr(replay_module.freezer_module, "freeze", _stub_freeze)
    monkeypatch.setattr(
        replay_module,
        "evaluate_class_at_frozen_time",
        _golden_stub_evaluate,
    )

    reference_path = _golden_reference_path()
    if not reference_path.exists():
        pytest.skip("golden reference JSON not yet generated")
    reference = json.loads(reference_path.read_text(encoding="utf-8"))

    params = RunParameters(
        since_ts=datetime.fromisoformat(reference["since_ts"]),
        until_ts=datetime.fromisoformat(reference["until_ts"]),
        lag_days=int(reference["lag_days"]),
        class_ids=tuple(reference["class_ids"]),
        sectors=tuple(reference["sectors"]),
        venues=tuple(reference["venues"]),
        allow_recent=bool(reference["allow_recent"]),
    )
    pinned_now = datetime.fromisoformat(reference["pinned_now"])

    # Drop any cached run row from a prior invocation so we re-compute.
    golden_corpus_conn.execute("DELETE FROM backtest_traces")
    golden_corpus_conn.execute("DELETE FROM backtest_predictions")
    golden_corpus_conn.execute("DELETE FROM backtest_runs")

    result = run_backtest(
        params,
        conn=golden_corpus_conn,
        store=_FAKE_STORE,
        now=pinned_now,
        max_workers=1,
        persistence_conn=golden_corpus_conn,
    )
    assert result.run.status is BacktestStatus.COMPLETE

    with warnings.catch_warnings():
        # The reference corpus is engineered to score every prediction,
        # so a SkippedRunWarning would indicate a regression. Promote
        # the warning to a hard error to surface drift loudly.
        warnings.simplefilter("error", SkippedRunWarning)
        summary: ScoreSummary = aggregate_run_summary(
            golden_corpus_conn,
            result.run.run_id,
            bin_count_global=int(reference["bin_count_global"]),
            bin_count_per_sector={},
        )

    assert summary.overall_brier is not None
    expected_overall = float(reference["overall_brier"])
    assert np.isclose(summary.overall_brier, expected_overall, atol=1e-6), (
        f"overall_brier drift: got {summary.overall_brier!r} expected {expected_overall!r}"
    )
    expected_per_sector: dict[str, float] = reference["per_sector_brier"]
    assert set(summary.per_sector_brier) == set(expected_per_sector)
    for sector, expected in expected_per_sector.items():
        actual = summary.per_sector_brier[sector]
        assert np.isclose(actual, expected, atol=1e-6), (
            f"per_sector_brier[{sector!r}] drift: got {actual!r} expected {expected!r}"
        )

    expected_diagram_edges: dict[str, list[float]] = reference["reliability_bin_edges"]
    for sector, expected_edges in expected_diagram_edges.items():
        diagram = summary.reliability_diagrams[sector]
        actual_edges: list[float] = []
        for bin_ in diagram.bins:
            actual_edges.append(float(bin_.lower_p))
        actual_edges.append(float(diagram.bins[-1].upper_p))
        assert len(actual_edges) == len(expected_edges)
        for idx, expected in enumerate(expected_edges):
            assert np.isclose(actual_edges[idx], expected, atol=1e-6), (
                f"reliability bin edge drift sector={sector!r} "
                f"index={idx}: got {actual_edges[idx]!r} expected {expected!r}"
            )
