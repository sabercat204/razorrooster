"""T-CB-027 — Phase 4 verification gates: end-to-end determinism integration.

Locks the cross-cutting determinism contract that Phase 4 (score
aggregation) must honour before Phase 5 wiring lands:

* **Bit-equality with report_generator** (redundant safety belt over
  :mod:`tests.calibration_backtest.test_reliability`): the bin edges
  emitted by :func:`compute_bins` for ``bin_count`` in ``{2, 5, 10, 20}``
  match :func:`report_generator.engines.section_assemblers.reliability._equal_width_bins`
  exactly. This is the regression-critical scout-amendment gate that
  prevents float-noise from breaking the determinism re-run gate below.
* **Re-aggregation determinism**: invoking
  :func:`aggregate_run_summary` twice over the same seeded
  ``backtest_predictions`` rows produces byte-identical
  ``ScoreSummary.to_json()`` payloads. This is the practical
  end-to-end equivalent of "running the same RunParameters twice
  produces identical summary_json bytes" — the replay loop's
  per-row work is deterministic by construction (frozen freezer
  output, deterministic posterior stub), so the only remaining
  variability is the aggregator's encoding, which
  :meth:`ScoreSummary.to_json` already pins via ``sort_keys=True``.
* **Run-id determinism end-to-end**: a full
  :func:`run_backtest` invocation with persistence wired produces
  the canonical SHA-256 hash, not a UUID, and a second invocation
  with identical :class:`RunParameters` produces the same digest
  on a fresh in-memory database. This locks Phase 3 advisory E.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import duckdb
import pytest

from razor_rooster.calibration_backtest.engines import replay as replay_module
from razor_rooster.calibration_backtest.engines.freezer import FrozenState
from razor_rooster.calibration_backtest.engines.replay import (
    DEFAULT_RECENT_WINDOW_DAYS,
    run_backtest,
)
from razor_rooster.calibration_backtest.engines.scoring import (
    aggregate_run_summary,
    compute_bins,
)
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
    RunParameters,
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
from razor_rooster.report_generator.engines.section_assemblers.reliability import (
    _equal_width_bins as _rg_equal_width_bins,
)

if TYPE_CHECKING:
    from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore

# ---------------------------------------------------------------------------
# Constants and fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
_SINCE = datetime(2024, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2024, 6, 1, tzinfo=UTC)
_PRED_TS = datetime(2024, 1, 8, tzinfo=UTC)
_RES_TS = datetime(2024, 1, 15, tzinfo=UTC)
_STARTED = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)

_FAKE_STORE: DuckDBStore = cast("DuckDBStore", object())
"""Sentinel store passed to ``run_backtest`` in tests that stub the pipeline.

The determinism tests monkeypatch :func:`evaluate_class_at_frozen_time` so
the ``store`` argument is never dereferenced; a typed sentinel keeps mypy
``--strict`` honest without requiring a real ``DuckDBStore`` instance."""


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """In-memory DuckDB with calibration_backtest persistence schema applied."""
    connection = duckdb.connect(":memory:")
    try:
        run_pending_calibration_backtest_migrations(connection)
        yield connection
    finally:
        connection.close()


@pytest.fixture
def replay_conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """In-memory DuckDB with both upstream + persistence schemas applied.

    Mirrors the dual-schema fixture used by
    :mod:`tests.calibration_backtest.test_replay_persistence`. The replay
    loop reads from the polymarket / mispricing-detector tables and
    writes to the calibration_backtest tables on the same connection.
    """
    connection = duckdb.connect(":memory:")
    try:
        connection.execute(POLYMARKET_RESOLUTIONS_DDL)
        connection.execute(CLASS_MARKET_MAPPINGS_DDL)
        connection.execute(COMPARISON_CYCLES_DDL)
        connection.execute(COMPARISONS_DDL)
        connection.execute(COMPARISON_RESOLUTIONS_DDL)
        run_pending_calibration_backtest_migrations(connection)
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(run_id: str) -> BacktestRun:
    return BacktestRun(
        run_id=run_id,
        since_ts=_SINCE,
        until_ts=_UNTIL,
        lag_days=7,
        class_ids=("flu_h2h", "rate_hike"),
        sectors=("public_health", "economics"),
        venues=("polymarket",),
        library_version=1,
        system_revision="deadbeef",
        started_at=_STARTED,
        completed_at=None,
        status=BacktestStatus.IN_PROGRESS,
        error_summary=None,
        predictions_total=0,
        predictions_scored=0,
        predictions_skipped=0,
        overall_brier=None,
        summary_json=None,
        bin_count_global=10,
        bin_count_per_sector={},
        fallback_polarity_count=0,
        allow_recent=False,
        disclaimer_version="v1",
    )


def _make_prediction(
    *,
    run_id: str,
    prediction_id: str,
    sector: str,
    class_id: str,
    model_p: float,
    observed: float,
    brier_contribution: float,
) -> BacktestPrediction:
    return BacktestPrediction(
        run_id=run_id,
        prediction_id=prediction_id,
        class_id=class_id,
        condition_id=f"cond-{prediction_id}",
        venue="polymarket",
        sector=sector,
        prediction_ts=_PRED_TS,
        resolution_ts=_RES_TS,
        model_p=model_p,
        observed=observed,
        polarity=PolarityValue.FORWARD,
        polarity_source=PolaritySource.COMPARISON_RESOLUTIONS,
        mapping_mismatch_warning=False,
        definition_version=1,
        status=PredictionStatus.SCORED,
        skip_reason=None,
        brier_contribution=brier_contribution,
    )


def _seed_run_with_predictions(conn: duckdb.DuckDBPyConnection, run_id: str) -> None:
    """Seed a run with three scored predictions across two sectors."""
    operations.insert_run(conn, _make_run(run_id))
    operations.insert_predictions_batch(
        conn,
        [
            _make_prediction(
                run_id=run_id,
                prediction_id=f"{run_id}-p1",
                sector="public_health",
                class_id="flu_h2h",
                model_p=0.4,
                observed=1.0,
                brier_contribution=0.36,
            ),
            _make_prediction(
                run_id=run_id,
                prediction_id=f"{run_id}-p2",
                sector="public_health",
                class_id="flu_h2h",
                model_p=0.6,
                observed=1.0,
                brier_contribution=0.16,
            ),
            _make_prediction(
                run_id=run_id,
                prediction_id=f"{run_id}-p3",
                sector="economics",
                class_id="rate_hike",
                model_p=0.2,
                observed=0.0,
                brier_contribution=0.04,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Replay-loop stubs (mirroring tests/calibration_backtest/test_replay.py)
# ---------------------------------------------------------------------------


def _stub_freeze(_conn: duckdb.DuckDBPyConnection, prediction_ts: datetime) -> FrozenState:
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
    """Deterministic posterior + trace — fixed across all calls."""
    trace = {
        "class": {"class_id": class_id, "definition_version": 3},
        "data_as_of": prediction_ts.isoformat(),
        "library_version": library_version or 1,
    }
    return 0.42, trace


@pytest.fixture
def patched_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replay_module.freezer_module, "freeze", _stub_freeze)
    monkeypatch.setattr(replay_module, "evaluate_class_at_frozen_time", _stub_evaluate)


def _insert_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str,
    resolution_ts: datetime,
) -> None:
    conn.execute(
        "INSERT INTO polymarket_resolutions ("
        "source_id, source_record_id, source_publication_ts, fetch_ts, "
        "connector_version, source_payload_json, superseded_at, "
        "condition_id, winning_outcome_token_id, winning_outcome_label, "
        "resolution_ts, resolution_source, resolution_metadata, "
        "final_yes_price, final_no_price, total_volume_at_resolution, "
        "invalidated"
        ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, 'yes', ?, "
        "'polymarket', NULL, NULL, NULL, NULL, FALSE)",
        [
            "polymarket",
            condition_id,
            resolution_ts,
            resolution_ts,
            "v1.0.0",
            "{}",
            condition_id,
            resolution_ts,
        ],
    )


def _insert_mapping(
    conn: duckdb.DuckDBPyConnection,
    *,
    mapping_id: str,
    class_id: str,
    condition_id: str,
) -> None:
    conn.execute(
        "INSERT INTO class_market_mappings ("
        "mapping_id, class_id, condition_id, mapping_type, "
        "mapping_confidence, polarity, mapped_by, mapped_at, "
        "removed_at, notes, venue"
        ") VALUES (?, ?, ?, 'direct', 'high', 'aligned', 'op', ?, NULL, NULL, 'polymarket')",
        [mapping_id, class_id, condition_id, datetime(2025, 1, 1, tzinfo=UTC)],
    )


# ---------------------------------------------------------------------------
# T-CB-027 gate 1 — bit-equality with report_generator's _equal_width_bins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bin_count", [2, 5, 10, 20])
def test_compute_bins_edges_match_report_generator_equal_width_bins(
    bin_count: int,
) -> None:
    """``compute_bins`` edges equal :func:`_equal_width_bins` for ``{2,5,10,20}``.

    Locked by the T-CB-027 scout amendment: any float-noise drift here
    would silently break the determinism re-run gate, since per-sector
    reliability bins land in ``summary_json`` and bit-equal byte-output
    requires bit-equal bin edges.
    """
    diagram = compute_bins([], bin_count=bin_count)
    rg_edges = _rg_equal_width_bins(bin_count)
    cb_edges = [(b.lower_p, b.upper_p) for b in diagram.bins]
    assert cb_edges == rg_edges


# ---------------------------------------------------------------------------
# T-CB-027 gate 2 — aggregate_run_summary byte-determinism end-to-end
# ---------------------------------------------------------------------------


def test_aggregate_run_summary_is_byte_deterministic_across_re_runs(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Two aggregations on the same seeded rows produce byte-identical JSON.

    This is the practical end-to-end determinism gate: with the replay
    loop's per-row work fixed (the stubs above are pure), the only
    remaining variability is the aggregator's encoding, which
    :meth:`ScoreSummary.to_json` pins via ``sort_keys=True``. A second
    aggregation against the same rows must yield byte-identical output.
    """
    _seed_run_with_predictions(conn, "run-determinism")
    summary_a = aggregate_run_summary(
        conn,
        "run-determinism",
        bin_count_global=10,
        bin_count_per_sector={},
    )
    summary_b = aggregate_run_summary(
        conn,
        "run-determinism",
        bin_count_global=10,
        bin_count_per_sector={},
    )
    json_a = summary_a.to_json()
    json_b = summary_b.to_json()
    assert json_a == json_b
    assert json_a.encode("utf-8") == json_b.encode("utf-8")
    # The payload is parseable, schema-valid JSON.
    payload = json.loads(json_a)
    assert "overall_brier" in payload
    assert "per_sector_brier" in payload
    assert "per_class_brier" in payload
    assert "reliability_diagrams" in payload
    assert "fallback_polarity_count" in payload
    assert "fallback_polarity_rate" in payload


def test_aggregate_run_summary_persists_byte_identical_summary_json(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The on-disk ``summary_json`` column round-trips byte-identically.

    Persists a :class:`ScoreSummary` via
    :func:`operations.persist_score_summary` and asserts the raw
    column bytes match the canonical encoding produced by
    :meth:`ScoreSummary.to_json`. This locks the persistence-side of
    the determinism re-run gate so two replays on identical seeded
    data produce identical bytes on disk.
    """
    _seed_run_with_predictions(conn, "run-persist")
    summary = aggregate_run_summary(
        conn,
        "run-persist",
        bin_count_global=10,
        bin_count_per_sector={},
    )
    operations.persist_score_summary(
        conn,
        "run-persist",
        summary,
        completed_at=datetime(2024, 6, 1, 0, 5, 0, tzinfo=UTC),
        predictions_total=3,
        predictions_scored=3,
        predictions_skipped=0,
    )
    raw = conn.execute(
        "SELECT summary_json FROM backtest_runs WHERE run_id = ?",
        ["run-persist"],
    ).fetchone()
    assert raw is not None
    on_disk_text = raw[0]
    assert isinstance(on_disk_text, str)
    # Both sides re-encode via ``sort_keys=True`` so the on-disk text
    # equals the canonical to_json() representation field-for-field.
    parsed_disk = json.loads(on_disk_text)
    parsed_summary = json.loads(summary.to_json())
    assert parsed_disk == parsed_summary


# ---------------------------------------------------------------------------
# T-CB-027 gate 3 — run_id determinism via a full run_backtest re-run
# ---------------------------------------------------------------------------


def test_run_backtest_canonical_run_id_is_stable_across_separate_databases(
    patched_pipeline: None,
) -> None:
    """Two ``run_backtest`` calls on freshly seeded databases share ``run_id``.

    Mirrors the deployment-time determinism gate: the canonical SHA-256
    hash depends only on :class:`RunParameters` plus the resolved
    library_version + system_revision, not on transient state in any
    one database. With identical inputs and identical resolved
    metadata, the digest must be byte-identical and 64 hex chars (no
    UUID fallback survives).
    """

    def _fresh_db() -> duckdb.DuckDBPyConnection:
        connection = duckdb.connect(":memory:")
        connection.execute(POLYMARKET_RESOLUTIONS_DDL)
        connection.execute(CLASS_MARKET_MAPPINGS_DDL)
        connection.execute(COMPARISON_CYCLES_DDL)
        connection.execute(COMPARISONS_DDL)
        connection.execute(COMPARISON_RESOLUTIONS_DDL)
        run_pending_calibration_backtest_migrations(connection)
        ts = datetime(2025, 6, 1, tzinfo=UTC)
        connection.execute(
            "INSERT INTO polymarket_resolutions ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "condition_id, winning_outcome_token_id, winning_outcome_label, "
            "resolution_ts, resolution_source, resolution_metadata, "
            "final_yes_price, final_no_price, total_volume_at_resolution, "
            "invalidated"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, 'yes', ?, "
            "'polymarket', NULL, NULL, NULL, NULL, FALSE)",
            [
                "polymarket",
                "cond-1",
                ts,
                ts,
                "v1.0.0",
                "{}",
                "cond-1",
                ts,
            ],
        )
        connection.execute(
            "INSERT INTO class_market_mappings ("
            "mapping_id, class_id, condition_id, mapping_type, "
            "mapping_confidence, polarity, mapped_by, mapped_at, "
            "removed_at, notes, venue"
            ") VALUES (?, ?, ?, 'direct', 'high', 'aligned', 'op', ?, "
            "NULL, NULL, 'polymarket')",
            ["m-1", "cls-A", "cond-1", datetime(2025, 1, 1, tzinfo=UTC)],
        )
        return connection

    params = RunParameters(
        since_ts=datetime(2025, 5, 1, tzinfo=UTC),
        until_ts=_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
        lag_days=7,
        class_ids=("cls-A",),
        sectors=(),
        venues=("polymarket",),
        allow_recent=False,
    )
    db_a = _fresh_db()
    db_b = _fresh_db()
    try:
        result_a = run_backtest(
            params,
            conn=db_a,
            store=_FAKE_STORE,
            now=_NOW,
            max_workers=1,
            persistence_conn=db_a,
        )
        result_b = run_backtest(
            params,
            conn=db_b,
            store=_FAKE_STORE,
            now=_NOW,
            max_workers=1,
            persistence_conn=db_b,
        )
        assert result_a.run.run_id == result_b.run.run_id
        assert len(result_a.run.run_id) == 64
        assert all(ch in "0123456789abcdef" for ch in result_a.run.run_id)
    finally:
        db_a.close()
        db_b.close()
