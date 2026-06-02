"""Build the calibration_backtest golden 90-day corpus + reference values.

This deterministic builder script produces the two committed fixtures
that drive ``tests/calibration_backtest/test_e2e.py::test_golden_90day_*``:

1. ``golden_90day_corpus.duckdb`` — a full DuckDB file with upstream
   tables seeded (``polymarket_resolutions``, ``class_market_mappings``,
   the ``comparisons`` / ``comparison_resolutions`` / ``comparison_cycles``
   trio, and the calibration_backtest persistence tables).

2. ``golden_90day_reference.json`` — the per-sector and overall Brier
   scores plus reliability-bin edges captured by running
   :func:`run_backtest` against the seeded corpus. The audit test
   (``test_golden_90day_corpus_matches_reference``) re-runs against the
   committed corpus and compares aggregates within
   :func:`numpy.isclose(atol=1e-6)`.

Determinism. The script seeds Python's :mod:`random` with the fixed
value ``42`` (operator decision Q1, 2026-06-01) so re-running the
script produces byte-identical fixtures. The pinned wall-clock
``_PINNED_NOW`` and the canonical class evaluator stub (the same stub
the e2e test uses) keep the resulting :class:`ScoreSummary` identical
across machines and Python builds.

Why a stub instead of the real signal_scanner posterior? The audit
test does NOT require the real pattern_library + signal_scanner
pipeline — that path is exercised by the determinism tests in
``tests/calibration_backtest/test_phase4_determinism.py``. The audit
locks the orchestration, persistence, and aggregation arithmetic;
swapping the model_p strategy yields different numbers but the same
shape. The stub here picks a sector-specific ``model_p`` that
spans the prediction range (``[0.1, 0.9]``) so the reliability bins
cover multiple buckets without contrived per-resolution tuning.

Usage::

    python tests/calibration_backtest/fixtures/build_golden_corpus.py

Re-run after any structural change to the calibration_backtest schema,
:func:`run_backtest`, or the aggregator's bin-edge convention. The
script is idempotent: it overwrites both fixture files in place.
"""

from __future__ import annotations

import json
import random
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import duckdb

from razor_rooster.calibration_backtest.engines import replay as replay_module
from razor_rooster.calibration_backtest.engines.freezer import FrozenState
from razor_rooster.calibration_backtest.engines.replay import (
    DEFAULT_RECENT_WINDOW_DAYS,
    run_backtest,
)
from razor_rooster.calibration_backtest.engines.scoring import aggregate_run_summary
from razor_rooster.calibration_backtest.errors import SkippedRunWarning
from razor_rooster.calibration_backtest.models import RunParameters
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


_FIXTURES_DIR: Path = Path(__file__).resolve().parent
_CORPUS_PATH: Path = _FIXTURES_DIR / "golden_90day_corpus.duckdb"
_REFERENCE_PATH: Path = _FIXTURES_DIR / "golden_90day_reference.json"

_RNG_SEED: int = 42
"""Deterministic seed locked by operator decision Q1 (2026-06-01)."""

_PINNED_NOW: datetime = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
"""Pinned wall-clock so the recent-window guard is reproducible."""

_WINDOW_START: datetime = datetime(2025, 8, 1, tzinfo=UTC)
"""Beginning of the 90-day replay window."""

_WINDOW_END: datetime = _WINDOW_START + timedelta(days=90)
"""End of the 90-day replay window."""

_SECTORS: tuple[str, ...] = (
    "public_health",
    "economics",
    "politics",
    "climate",
    "technology",
)
"""Five sectors mirroring the v1 seed library."""

_CLASSES_PER_SECTOR: int = 1
"""One class per sector keeps the seeding logic narrow."""

_RESOLUTIONS_PER_CLASS: int = 60
"""60 resolutions per class -> 5 * 60 = 300 resolutions across the window."""

_FAKE_STORE: DuckDBStore = cast("DuckDBStore", object())
"""Sentinel store passed to :func:`run_backtest`; the stub never reads it."""


def _stub_freeze(conn: duckdb.DuckDBPyConnection, prediction_ts: datetime) -> FrozenState | None:
    """Always-frozen stub — the freezer's source-data short-circuit never fires.

    Returns ``FrozenState | None`` to match the production
    :func:`freezer.freeze` signature so the runtime monkey-patch satisfies
    mypy's structural compatibility check. Argument names also mirror
    the production signature so positional / keyword call shapes line up.
    """
    del conn
    return FrozenState(
        source_publication_ts_boundary=prediction_ts,
        frozen_flag=True,
        registered_sources=frozenset({"fred"}),
    )


# Per-sector ``model_p`` so the reliability bins span the [0, 1] range.
_SECTOR_MODEL_P: dict[str, float] = {
    "public_health": 0.15,
    "economics": 0.35,
    "politics": 0.55,
    "climate": 0.72,
    "technology": 0.88,
}


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
    """Return a sector-specific ``model_p`` plus a JSON-roundtrippable trace.

    The ``class_id`` carries the sector tag (``cls-<sector>``) so we can
    look up the deterministic per-sector probability without threading a
    dict through the call site. The trace shape mirrors the live scanner's
    :func:`signal_scanner.engines.trace.build_trace` payload so the
    persistence-layer trace decoder round-trips cleanly.
    """
    sector = class_id.removeprefix("cls-")
    model_p = _SECTOR_MODEL_P.get(sector, 0.5)
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


def _seed_corpus_into(target: duckdb.DuckDBPyConnection) -> tuple[str, ...]:
    """Seed 5 sectors x 60 resolutions = 300 mapped resolutions deterministically.

    Outcomes are sampled via Python's seeded :mod:`random`; the per-sector
    :data:`_SECTOR_MODEL_P` is the model's predicted probability so a
    well-calibrated model would observe yes-rates close to those values.
    The actual yes-rate is intentionally biased away from the model
    probability so the per-sector Brier scores diverge sector-to-sector
    (otherwise the reference fixture would carry trivial all-zero
    Brier values).
    """
    rng = random.Random(_RNG_SEED)
    class_ids: list[str] = []
    for sector_index, sector in enumerate(_SECTORS):
        class_id = f"cls-{sector}"
        class_ids.append(class_id)
        # Outcomes biased: the empirical rate sits above the predicted
        # probability for low-prob sectors, below for high-prob ones, so
        # per-sector Brier values span a meaningful range.
        empirical_rate = 0.5
        for resolution_index in range(_RESOLUTIONS_PER_CLASS):
            condition_id = f"cond-{sector}-{resolution_index:03d}"
            day_offset = sector_index * _RESOLUTIONS_PER_CLASS + resolution_index
            # Spread resolutions evenly across the 90-day window.
            resolution_ts = _WINDOW_START + timedelta(days=(day_offset % 90))
            outcome = "yes" if rng.random() < empirical_rate else "no"
            target.execute(
                "INSERT INTO polymarket_resolutions ("
                "source_id, source_record_id, source_publication_ts, fetch_ts, "
                "connector_version, source_payload_json, superseded_at, "
                "condition_id, winning_outcome_token_id, winning_outcome_label, "
                "resolution_ts, resolution_source, resolution_metadata, "
                "final_yes_price, final_no_price, total_volume_at_resolution, "
                "invalidated"
                ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?, 'polymarket', "
                "NULL, NULL, NULL, NULL, FALSE)",
                [
                    "polymarket",
                    condition_id,
                    resolution_ts,
                    resolution_ts,
                    "v1.0.0",
                    "{}",
                    condition_id,
                    outcome,
                    resolution_ts,
                ],
            )
            target.execute(
                "INSERT INTO class_market_mappings ("
                "mapping_id, class_id, condition_id, mapping_type, "
                "mapping_confidence, polarity, mapped_by, mapped_at, "
                "removed_at, notes, venue"
                ") VALUES (?, ?, ?, 'direct', 'high', 'aligned', 'op', ?, "
                "NULL, NULL, 'polymarket')",
                [
                    f"m-{sector}-{resolution_index:03d}",
                    class_id,
                    condition_id,
                    datetime(2025, 1, 1, tzinfo=UTC),
                ],
            )
    return tuple(class_ids)


def _build_corpus_db(corpus_path: Path) -> tuple[str, ...]:
    """Initialise an on-disk DuckDB at *corpus_path* and seed the 90-day corpus.

    Returns the tuple of class_ids seeded so the caller can build a
    matching :class:`RunParameters`.
    """
    if corpus_path.exists():
        corpus_path.unlink()
    connection = duckdb.connect(str(corpus_path))
    try:
        connection.execute(POLYMARKET_RESOLUTIONS_DDL)
        connection.execute(CLASS_MARKET_MAPPINGS_DDL)
        connection.execute(COMPARISON_CYCLES_DDL)
        connection.execute(COMPARISONS_DDL)
        connection.execute(COMPARISON_RESOLUTIONS_DDL)
        run_pending_calibration_backtest_migrations(connection)
        class_ids = _seed_corpus_into(connection)
    finally:
        connection.close()
    return class_ids


def main() -> None:
    """Build both fixture files in place.

    1. Wipe any previous corpus on disk.
    2. Seed 300 deterministic resolutions across 5 sectors x 90 days.
    3. Stub the freezer + class evaluator (the same stubs the e2e test
       uses) so the run is independent of the live pattern_library +
       signal_scanner wiring.
    4. Invoke :func:`run_backtest` with persistence wired so the corpus
       carries a complete ``backtest_runs``/``predictions``/``traces``
       trio for the audit test's read path.
    5. Aggregate the persisted run via :func:`aggregate_run_summary`,
       then dump the per-sector Brier values + reliability bin edges to
       ``golden_90day_reference.json`` for the audit test's compare.
    """
    print(f"[golden] writing corpus to {_CORPUS_PATH}")
    class_ids = _build_corpus_db(_CORPUS_PATH)

    # Re-open the corpus on the persistence path that run_backtest will use.
    connection = duckdb.connect(str(_CORPUS_PATH))
    try:
        # Apply the freezer + evaluator stubs the same way the e2e test
        # does. Patching at the module level (replay_module) lets us hit
        # both call sites used inside run_backtest.
        original_freeze = replay_module.freezer_module.freeze
        original_evaluate = replay_module.evaluate_class_at_frozen_time
        replay_module.freezer_module.freeze = _stub_freeze
        replay_module.evaluate_class_at_frozen_time = _stub_evaluate
        try:
            params = RunParameters(
                since_ts=_WINDOW_START,
                until_ts=_PINNED_NOW - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS + 1),
                lag_days=7,
                class_ids=class_ids,
                sectors=(),
                venues=("polymarket",),
                allow_recent=False,
            )
            print("[golden] running run_backtest with stubbed pipeline ...")
            result = run_backtest(
                params,
                conn=connection,
                store=_FAKE_STORE,
                now=_PINNED_NOW,
                max_workers=1,
                persistence_conn=connection,
            )
            print(
                f"[golden] run_id={result.run.run_id} "
                f"predictions_total={result.run.predictions_total} "
                f"predictions_scored={result.run.predictions_scored}"
            )
        finally:
            replay_module.freezer_module.freeze = original_freeze
            replay_module.evaluate_class_at_frozen_time = original_evaluate

        with warnings.catch_warnings():
            warnings.simplefilter("error", SkippedRunWarning)
            summary = aggregate_run_summary(
                connection,
                result.run.run_id,
                bin_count_global=10,
                bin_count_per_sector={},
            )
    finally:
        connection.close()

    assert summary.overall_brier is not None, "expected populated overall_brier"

    reference: dict[str, Any] = {
        "since_ts": params.since_ts.isoformat(),
        "until_ts": params.until_ts.isoformat(),
        "lag_days": params.lag_days,
        "class_ids": list(params.class_ids),
        "sectors": list(params.sectors),
        "venues": list(params.venues),
        "allow_recent": params.allow_recent,
        "pinned_now": _PINNED_NOW.isoformat(),
        "bin_count_global": 10,
        "predictions_total": result.run.predictions_total,
        "predictions_scored": result.run.predictions_scored,
        "predictions_skipped": result.run.predictions_skipped,
        "overall_brier": summary.overall_brier,
        "per_sector_brier": dict(sorted(summary.per_sector_brier.items())),
        "per_class_brier": dict(sorted(summary.per_class_brier.items())),
        "fallback_polarity_count": summary.fallback_polarity_count,
        "fallback_polarity_rate": summary.fallback_polarity_rate,
        "reliability_bin_edges": _diagram_edges(summary),
    }

    print(f"[golden] writing reference to {_REFERENCE_PATH}")
    _REFERENCE_PATH.write_text(
        json.dumps(reference, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print("[golden] done.")


def _diagram_edges(summary: Any) -> dict[str, list[float]]:
    """Pull per-sector reliability bin edges into a serialisable mapping.

    Reliability diagrams emit ``bin_count + 1`` edges (the lower edges
    plus the final upper edge). Rounding to four decimals matches the
    aggregator's ``_EDGE_DECIMALS`` invariant so the on-disk reference
    is byte-stable across re-builds.
    """
    out: dict[str, list[float]] = {}
    for sector in sorted(summary.reliability_diagrams):
        diagram = summary.reliability_diagrams[sector]
        edges = [round(float(b.lower_p), 4) for b in diagram.bins]
        edges.append(round(float(diagram.bins[-1].upper_p), 4))
        out[sector] = edges
    return out


if __name__ == "__main__":
    main()
