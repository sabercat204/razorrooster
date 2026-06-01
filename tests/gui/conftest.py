"""Shared fixtures for the GUI route tests.

Provides a populated DuckDB store and a FastAPI ``TestClient`` bound
to a freshly-built ``create_app`` instance per test.

Calibration-backtest GUI tests share the ``populated_backtest_db`` and
``backtest_client`` fixtures defined here (T-CB-040). The fixtures seed
three ``BacktestRun`` rows spanning the lifecycle states
(``complete`` / ``in_progress`` / ``failed``), >=40 ``BacktestPrediction``
rows on the healthy run (mix of ``scored`` / ``skipped`` with multiple
``skip_reason`` values), and a couple of ``BacktestTrace`` rows so the
detail view's reliability + trace surfaces have rows to point at. The
existing per-file fixture in ``test_calibration_backtest_routes.py``
remains in place — it shadows this conftest fixture inside that module
so the route-level pagination tests keep their tight 3-run seed. Other
suites (``test_calibration_backtest_framing.py``, future GUI tests)
consume the conftest fixture.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from razor_rooster.calibration_backtest.engines.trace_codec import build_trace
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
    ReliabilityBin,
    ReliabilityDiagram,
    ScoreSummary,
    SkipReason,
)
from razor_rooster.calibration_backtest.persistence import operations as cb_operations
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.gui.app import create_app
from razor_rooster.position_engine.models import Analysis, BankrollConfig
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import (
    append_watch_state,
    persist_analysis,
    write_bankroll_config,
)
from razor_rooster.report_generator.models import ReportRecord
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import persist_report


def _make_record(
    *,
    report_id: str,
    generated_at: datetime,
    sections_rendered: tuple[str, ...] = ("system_health", "surfaced"),
    sections_failed: tuple[dict[str, str], ...] = (),
    library_version: int = 1,
    disclaimer_hash: str = "abc123",
    terminal_text: str = "REPORT TEXT",
    rendered_html_text: str | None = None,
    markdown_path: str | None = None,
    html_path: str | None = None,
) -> ReportRecord:
    return ReportRecord(
        report_id=report_id,
        generated_at=generated_at,
        since_ts=generated_at - timedelta(days=1),
        until_ts=generated_at,
        sections_enabled=tuple(sections_rendered),
        sections_rendered=sections_rendered,
        sections_failed=sections_failed,
        library_version=library_version,
        disclaimer_version_hash=disclaimer_hash,
        rendered_terminal_text=terminal_text,
        rendered_html_text=rendered_html_text,
        markdown_path=markdown_path,
        html_path=html_path,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create a DuckDB store with every GUI-facing schema applied.

    The GUI's calibration-backtest routes hit ``backtest_runs`` even on
    an empty store, so the calibration-backtest migrations run alongside
    the report-generator and position-engine ones.
    """
    path = tmp_path / "gui.duckdb"
    s = DuckDBStore(path)
    try:
        with s.connection() as c:
            run_pending_report_generator_migrations(c)
            run_pending_position_engine_migrations(c)
            run_pending_calibration_backtest_migrations(c)
    finally:
        s.close()
    return path


def _make_analysis(
    *,
    analysis_id: str,
    class_id: str,
    venue: str = "polymarket",
    bankroll_config_id: str = "bk-1",
    cycle_id: str = "cycle-1",
    model_probability: float = 0.65,
    market_probability: float | None = 0.50,
    suggested_dollar_size: float = 50.0,
    computed_at: datetime | None = None,
) -> Analysis:
    return Analysis(
        analysis_id=analysis_id,
        cycle_id=cycle_id,
        comparison_id=f"cmp-{analysis_id}",
        class_id=class_id,
        condition_id=f"cond-{analysis_id}",
        bankroll_config_id=bankroll_config_id,
        model_probability=model_probability,
        market_probability=market_probability,
        kelly_unclamped=0.05,
        kelly_negative=False,
        kelly_clamped_by_max_cap=False,
        kelly_clamped_by_liquidity=False,
        suggested_fraction=0.05,
        suggested_dollar_size=suggested_dollar_size,
        ev_per_dollar=0.07,
        bankroll_after_1_loss_pct=0.95,
        bankroll_after_3_losses_pct=0.85,
        bankroll_after_5_losses_pct=0.75,
        suggested_pct_of_24h_volume=0.02,
        days_to_resolution=14,
        long_time_to_resolution=False,
        sub_threshold=False,
        sensitivity_analysis=None,
        computed_at=computed_at,
        venue=venue,  # type: ignore[arg-type]
    )


@pytest.fixture
def populated_db(db_path: Path) -> Path:
    """Seed the store with three reports plus four position-engine analyses
    spanning every watch state.
    """
    base = datetime.now(tz=UTC)
    s = DuckDBStore(db_path)
    try:
        with s.connection() as c:
            persist_report(
                c,
                _make_record(
                    report_id="r-newest",
                    generated_at=base,
                    sections_rendered=("system_health", "surfaced", "watched"),
                    terminal_text="NEWEST CYCLE\n",
                    rendered_html_text=("<!DOCTYPE html><html><body>NEWEST</body></html>"),
                    markdown_path="/tmp/r-newest.md",
                ),
            )
            persist_report(
                c,
                _make_record(
                    report_id="r-middle",
                    generated_at=base - timedelta(days=2),
                    sections_rendered=("system_health", "surfaced"),
                    sections_failed=({"section": "calibration", "error": "x"},),
                    terminal_text="MIDDLE CYCLE\n",
                ),
            )
            persist_report(
                c,
                _make_record(
                    report_id="r-oldest",
                    generated_at=base - timedelta(days=5),
                    sections_rendered=("system_health",),
                    terminal_text="OLDEST CYCLE\n",
                ),
            )

            # Seed bankroll config + analyses + watch states.
            write_bankroll_config(
                c,
                BankrollConfig(
                    config_id="bk-1",
                    analytical_bankroll_usd=1000.0,
                    max_single_position_pct=0.10,
                    kelly_fraction_default=0.5,
                    min_edge_threshold=0.03,
                    effective_at=base - timedelta(days=10),
                ),
            )
            for ai, cls, st in (
                ("a-watching", "election", "watching"),
                ("a-acted", "regulation", "acted_on"),
                ("a-dismissed", "weather", "dismissed"),
                ("a-expired", "commodity", "expired"),
            ):
                persist_analysis(
                    c,
                    _make_analysis(
                        analysis_id=ai,
                        class_id=cls,
                        computed_at=base - timedelta(hours=12),
                    ),
                )
                append_watch_state(
                    c,
                    analysis_id=ai,
                    state=st,  # type: ignore[arg-type]
                    notes=f"seed-{st}",
                    set_by="operator" if st != "expired" else "system",
                    when=base - timedelta(hours=6),
                )
            # Add a watch_states row referencing a missing analysis to
            # confirm the GUI degrades gracefully (Analysis is None).
            append_watch_state(
                c,
                analysis_id="a-orphaned",
                state="watching",
                notes="analysis row absent on purpose",
                set_by="operator",
                when=base - timedelta(hours=1),
            )
    finally:
        s.close()
    return db_path


@pytest.fixture
def client(populated_db: Path) -> Iterator[TestClient]:
    """FastAPI TestClient bound to a fresh app reading from populated_db."""
    app = create_app(db_path=populated_db)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def empty_client(db_path: Path) -> Iterator[TestClient]:
    """TestClient bound to a store with the schema but zero reports."""
    app = create_app(db_path=db_path)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Calibration-backtest seed (T-CB-040)
# ---------------------------------------------------------------------------
#
# The fixtures below populate ``backtest_runs`` / ``backtest_predictions`` /
# ``backtest_traces`` so the calibration-backtest GUI surfaces have rows to
# point at. The route-level pagination tests in
# ``test_calibration_backtest_routes.py`` keep their own tighter 3-run seed
# (it overrides this fixture by name), but framing tests, future regression
# coverage, and any cross-suite invariant checks share these helpers.


_CB_BASE_STARTED = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_CB_SINCE = datetime(2025, 1, 1, tzinfo=UTC)
_CB_UNTIL = datetime(2025, 12, 1, tzinfo=UTC)

_CB_HEALTHY_RUN_ID = "run-healthy-aaaaaaaaaaaaaaa"
_CB_FALLBACK_RUN_ID = "run-fallback-bbbbbbbbbbbbbb"
_CB_FAILED_RUN_ID = "run-failed-cccccccccccccccc"


def _cb_make_diagram() -> ReliabilityDiagram:
    """A small two-bin reliability diagram used by the seed runs."""

    return ReliabilityDiagram(
        bin_count=2,
        bins=(
            ReliabilityBin(
                lower_p=0.0,
                upper_p=0.5,
                count=2,
                mean_predicted_p=0.25,
                empirical_rate=0.0,
            ),
            ReliabilityBin(
                lower_p=0.5,
                upper_p=1.0,
                count=2,
                mean_predicted_p=0.75,
                empirical_rate=1.0,
            ),
        ),
    )


def _cb_make_summary(*, fallback_count: int, scored: int) -> ScoreSummary:
    return ScoreSummary(
        overall_brier=0.16,
        per_sector_brier={"public_health": 0.16},
        per_class_brier={"flu_h2h": 0.16},
        reliability_diagrams={"public_health": _cb_make_diagram()},
        zero_resolutions_sectors=(),
        zero_resolutions_classes=(),
        fallback_polarity_count=fallback_count,
        fallback_polarity_rate=(fallback_count / scored) if scored > 0 else 0.0,
    )


def _cb_make_run(
    *,
    run_id: str,
    started_at: datetime,
    status: BacktestStatus,
    predictions_total: int,
    predictions_scored: int,
    predictions_skipped: int,
    fallback_count: int,
    overall_brier: float | None,
    summary: ScoreSummary | None,
    completed_at: datetime | None,
    error_summary: str | None = None,
) -> BacktestRun:
    summary_json = summary.as_mapping() if summary is not None else None
    return BacktestRun(
        run_id=run_id,
        since_ts=_CB_SINCE,
        until_ts=_CB_UNTIL,
        lag_days=7,
        class_ids=("flu_h2h",),
        sectors=("public_health",),
        venues=("polymarket",),
        library_version=1,
        system_revision="deadbeef" * 4,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        error_summary=error_summary,
        predictions_total=predictions_total,
        predictions_scored=predictions_scored,
        predictions_skipped=predictions_skipped,
        overall_brier=overall_brier,
        summary_json=summary_json,
        bin_count_global=10,
        bin_count_per_sector={"public_health": 5},
        fallback_polarity_count=fallback_count,
        allow_recent=False,
        disclaimer_version="v1",
    )


def _cb_make_scored_prediction(*, run_id: str, prediction_id: str) -> BacktestPrediction:
    return BacktestPrediction(
        run_id=run_id,
        prediction_id=prediction_id,
        class_id="flu_h2h",
        condition_id=f"cond-{prediction_id}",
        venue="polymarket",
        sector="public_health",
        prediction_ts=_CB_SINCE + timedelta(days=1),
        resolution_ts=_CB_SINCE + timedelta(days=8),
        model_p=0.4,
        observed=1.0,
        polarity=PolarityValue.FORWARD,
        polarity_source=PolaritySource.COMPARISON_RESOLUTIONS,
        mapping_mismatch_warning=False,
        definition_version=1,
        status=PredictionStatus.SCORED,
        skip_reason=None,
        brier_contribution=0.36,
    )


def _cb_make_skipped_prediction(
    *,
    run_id: str,
    prediction_id: str,
    skip_reason: SkipReason,
) -> BacktestPrediction:
    return BacktestPrediction(
        run_id=run_id,
        prediction_id=prediction_id,
        class_id="flu_h2h",
        condition_id=f"cond-{prediction_id}",
        venue="polymarket",
        sector="public_health",
        prediction_ts=_CB_SINCE + timedelta(days=1),
        resolution_ts=_CB_SINCE + timedelta(days=8),
        model_p=None,
        observed=None,
        polarity=None,
        polarity_source=PolaritySource.NO_POLARITY,
        mapping_mismatch_warning=False,
        definition_version=1,
        status=PredictionStatus.SKIPPED,
        skip_reason=skip_reason,
        brier_contribution=None,
    )


@pytest.fixture
def populated_backtest_db(tmp_path: Path) -> Path:
    """Seed ``backtest_*`` tables with three runs + 40+ predictions + traces.

    Layout:

    * ``run-healthy-...`` — ``COMPLETE`` with low fallback rate (2/40 = 5%);
      40 scored predictions plus a couple of skipped predictions to keep
      the skip-reason filter dropdown populated. Two predictions also
      have a ``BacktestTrace`` row so the detail view's trace surfaces
      have data to render once the AJAX endpoint lands.
    * ``run-fallback-...`` — ``COMPLETE`` with high fallback rate
      (15/40 = 37.5%); exercises the >5% banner path. 40 scored
      predictions + 5 skipped (mixed reasons).
    * ``run-failed-...`` — ``FAILED`` with no scored predictions; smoke
      coverage for the failed-status badge and the empty-summary
      degenerate path on the detail view.

    Each run carries enough rows that pagination (``limit=20``) crosses
    a page boundary without being so large that test runtime suffers.
    Prediction IDs are zero-padded so ``ORDER BY prediction_id ASC``
    sorts deterministically.
    """

    path = tmp_path / "test_backtest.duckdb"
    store = DuckDBStore(path)
    try:
        with store.connection() as conn:
            # Calibration-backtest migrations are self-contained — the
            # design pins the migration-version range clear of every
            # upstream subsystem so a standalone run produces a fully
            # functional schema.
            run_pending_calibration_backtest_migrations(conn)

            healthy_run = _cb_make_run(
                run_id=_CB_HEALTHY_RUN_ID,
                started_at=_CB_BASE_STARTED,
                status=BacktestStatus.COMPLETE,
                predictions_total=42,
                predictions_scored=40,
                predictions_skipped=2,
                fallback_count=2,
                overall_brier=0.16,
                summary=_cb_make_summary(fallback_count=2, scored=40),
                completed_at=_CB_BASE_STARTED + timedelta(minutes=5),
            )
            fallback_run = _cb_make_run(
                run_id=_CB_FALLBACK_RUN_ID,
                started_at=_CB_BASE_STARTED - timedelta(hours=2),
                status=BacktestStatus.COMPLETE,
                predictions_total=45,
                predictions_scored=40,
                predictions_skipped=5,
                fallback_count=15,
                overall_brier=0.20,
                summary=_cb_make_summary(fallback_count=15, scored=40),
                completed_at=_CB_BASE_STARTED - timedelta(hours=2) + timedelta(minutes=5),
            )
            failed_run = _cb_make_run(
                run_id=_CB_FAILED_RUN_ID,
                started_at=_CB_BASE_STARTED - timedelta(hours=4),
                status=BacktestStatus.FAILED,
                predictions_total=0,
                predictions_scored=0,
                predictions_skipped=0,
                fallback_count=0,
                overall_brier=None,
                summary=None,
                completed_at=_CB_BASE_STARTED - timedelta(hours=4) + timedelta(seconds=12),
                error_summary="seeded failure for fixture coverage",
            )
            for run in (healthy_run, fallback_run, failed_run):
                cb_operations.insert_run(conn, run)

            # Healthy run: 40 scored + 2 skipped (mixed reasons).
            for idx in range(40):
                cb_operations.insert_prediction(
                    conn,
                    _cb_make_scored_prediction(
                        run_id=_CB_HEALTHY_RUN_ID,
                        prediction_id=f"pred-h-{idx:03d}",
                    ),
                )
            cb_operations.insert_prediction(
                conn,
                _cb_make_skipped_prediction(
                    run_id=_CB_HEALTHY_RUN_ID,
                    prediction_id="pred-h-skip-mapping",
                    skip_reason=SkipReason.MAPPING_NOT_FOUND,
                ),
            )
            cb_operations.insert_prediction(
                conn,
                _cb_make_skipped_prediction(
                    run_id=_CB_HEALTHY_RUN_ID,
                    prediction_id="pred-h-skip-invalid",
                    skip_reason=SkipReason.INVALID_RESOLUTION,
                ),
            )

            # Fallback run: 40 scored + 5 skipped (mixed reasons).
            for idx in range(40):
                cb_operations.insert_prediction(
                    conn,
                    _cb_make_scored_prediction(
                        run_id=_CB_FALLBACK_RUN_ID,
                        prediction_id=f"pred-f-{idx:03d}",
                    ),
                )
            for idx in range(3):
                cb_operations.insert_prediction(
                    conn,
                    _cb_make_skipped_prediction(
                        run_id=_CB_FALLBACK_RUN_ID,
                        prediction_id=f"pred-f-skip-mapping-{idx:03d}",
                        skip_reason=SkipReason.MAPPING_NOT_FOUND,
                    ),
                )
            for idx in range(2):
                cb_operations.insert_prediction(
                    conn,
                    _cb_make_skipped_prediction(
                        run_id=_CB_FALLBACK_RUN_ID,
                        prediction_id=f"pred-f-skip-invalid-{idx:03d}",
                        skip_reason=SkipReason.INVALID_RESOLUTION,
                    ),
                )

            # Two trace rows on the healthy run so trace-aware tests can
            # round-trip a real BLOB through ``fetch_trace``.
            for prediction_id in ("pred-h-000", "pred-h-001"):
                cb_operations.insert_trace(
                    conn,
                    build_trace(
                        run_id=_CB_HEALTHY_RUN_ID,
                        prediction_id=prediction_id,
                        payload={
                            "prediction_id": prediction_id,
                            "model_p": 0.4,
                            "observed": 1.0,
                            "support_count": 17,
                        },
                    ),
                )
    finally:
        store.close()
    return path


@pytest.fixture
def backtest_client(populated_backtest_db: Path) -> Iterator[TestClient]:
    """TestClient bound to a fresh app reading from ``populated_backtest_db``."""

    app = create_app(db_path=populated_backtest_db)
    with TestClient(app) as test_client:
        yield test_client
