"""Integration tests proving compare engine integrates with T-CB-023's ScoreSummary.

These tests verify that :func:`compare_runs` (T-CB-024) and
:func:`aggregate_run_summary` (T-CB-023) share the canonical aggregation
function ``AVG(brier_contribution) FILTER status='scored' AND
brier_contribution IS NOT NULL`` so a self-compare produces zero deltas.

**Convention difference (documented):** ``ScoreSummary.per_sector_brier``
groups predictions by ``sector`` only; ``ScoreSummary.per_class_brier``
groups by ``class_id`` only. :func:`compare_runs`, by contrast, groups
by the ``(sector, class_id)`` cross product — strictly more granular
than either ScoreSummary breakdown. The two aggregations therefore
agree only when, for a given sector, every scored prediction shares the
same ``class_id`` (and vice versa). Outside that degenerate case the
expected per-cell Brier values must be computed directly from the
underlying ``brier_contribution`` rows.

The tests below exercise both regimes:

* :func:`test_compare_self_matches_score_summary_in_degenerate_layout`
  pins the case where every ``sector`` carries exactly one ``class_id``,
  so ``compare_runs`` cells equal ``ScoreSummary.per_sector_brier`` and
  ``ScoreSummary.per_class_brier`` element-for-element.
* :func:`test_compare_self_matches_brier_contribution_avg_in_general_case`
  exercises the more granular cross-product layout where multiple
  classes share a sector; the expected ``brier_a == brier_b`` per cell
  is computed by averaging the relevant ``brier_contribution`` values
  directly.
* :func:`test_self_compare_all_deltas_zero` asserts the cross-aggregator
  parity property — every cell from ``compare_runs(conn, run, run)``
  has ``delta_absolute == 0.0`` regardless of layout.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import duckdb
import pytest

from razor_rooster.calibration_backtest.engines.compare import compare_runs
from razor_rooster.calibration_backtest.engines.scoring import aggregate_run_summary
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
    PresentIn,
    SkipReason,
)
from razor_rooster.calibration_backtest.persistence import operations
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    """Yield an in-memory DuckDB connection with calibration_backtest schema."""
    connection = duckdb.connect(":memory:")
    try:
        run_pending_calibration_backtest_migrations(connection)
        yield connection
    finally:
        connection.close()


_SINCE = datetime(2024, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2024, 6, 1, tzinfo=UTC)
_STARTED = datetime(2024, 6, 1, 0, 0, 0, tzinfo=UTC)
_PRED_TS = datetime(2024, 1, 8, tzinfo=UTC)
_RES_TS = datetime(2024, 1, 15, tzinfo=UTC)


def _make_run(run_id: str, **overrides: Any) -> BacktestRun:
    base: dict[str, Any] = {
        "run_id": run_id,
        "since_ts": _SINCE,
        "until_ts": _UNTIL,
        "lag_days": 7,
        "class_ids": ("flu_h2h",),
        "sectors": ("public_health",),
        "venues": ("polymarket",),
        "library_version": 1,
        "system_revision": "deadbeef",
        "started_at": _STARTED,
        "completed_at": None,
        "status": BacktestStatus.IN_PROGRESS,
        "error_summary": None,
        "predictions_total": 0,
        "predictions_scored": 0,
        "predictions_skipped": 0,
        "overall_brier": None,
        "summary_json": None,
        "bin_count_global": 10,
        "bin_count_per_sector": {},
        "fallback_polarity_count": 0,
        "allow_recent": False,
        "disclaimer_version": "v1",
    }
    base.update(overrides)
    return BacktestRun(**base)


def _make_scored(
    *,
    run_id: str,
    prediction_id: str,
    sector: str,
    class_id: str,
    brier_contribution: float,
    model_p: float = 0.4,
    observed: float = 1.0,
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


def _make_skipped(
    *,
    run_id: str,
    prediction_id: str,
    sector: str,
    class_id: str,
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
        model_p=None,
        observed=None,
        polarity=None,
        polarity_source=PolaritySource.NO_POLARITY,
        mapping_mismatch_warning=False,
        definition_version=1,
        status=PredictionStatus.SKIPPED,
        skip_reason=SkipReason.NO_POLARITY_RESOLUTION,
        brier_contribution=None,
    )


# ---------------------------------------------------------------------------
# Degenerate layout (one class per sector): ScoreSummary <-> compare parity
# ---------------------------------------------------------------------------


def test_compare_self_matches_score_summary_in_degenerate_layout(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """When each sector carries one class, compare cells equal ScoreSummary maps.

    The cross-product ``(sector, class_id)`` collapses to the same
    grouping as either of ScoreSummary's per-sector or per-class maps,
    so :func:`compare_runs` cells must equal both
    :attr:`ScoreSummary.per_sector_brier` and
    :attr:`ScoreSummary.per_class_brier` element-for-element on a
    self-compare.
    """
    run_id = "run-degenerate"
    operations.insert_run(conn, _make_run(run_id))
    # One class per sector: cross-product layout collapses to ScoreSummary's
    # per-sector / per-class layouts simultaneously.
    operations.insert_predictions_batch(
        conn,
        [
            _make_scored(
                run_id=run_id,
                prediction_id="p1",
                sector="public_health",
                class_id="flu_h2h",
                brier_contribution=0.16,
            ),
            _make_scored(
                run_id=run_id,
                prediction_id="p2",
                sector="public_health",
                class_id="flu_h2h",
                brier_contribution=0.36,
            ),
            _make_scored(
                run_id=run_id,
                prediction_id="p3",
                sector="economics",
                class_id="rate_hike",
                brier_contribution=0.04,
            ),
        ],
    )

    summary = aggregate_run_summary(
        conn,
        run_id,
        bin_count_global=10,
        bin_count_per_sector={},
    )
    cells = compare_runs(conn, run_id, run_id)

    # ScoreSummary aggregations.
    assert summary.per_sector_brier == {
        "public_health": pytest.approx((0.16 + 0.36) / 2, abs=1e-9),
        "economics": pytest.approx(0.04, abs=1e-9),
    }
    assert summary.per_class_brier == {
        "flu_h2h": pytest.approx((0.16 + 0.36) / 2, abs=1e-9),
        "rate_hike": pytest.approx(0.04, abs=1e-9),
    }

    # compare_runs cells, indexed by (sector, class_id).
    by_key = {(c.sector, c.class_id): c for c in cells}
    assert set(by_key) == {("economics", "rate_hike"), ("public_health", "flu_h2h")}

    # Both sides equal in self-compare.
    for key, cell in by_key.items():
        assert cell.present_in is PresentIn.BOTH, key
        assert cell.brier_a is not None
        assert cell.brier_b is not None
        assert cell.brier_a == pytest.approx(cell.brier_b, abs=1e-12)
        assert cell.delta_absolute == 0.0
        # brier_a > 0 in this fixture for every cell.
        assert cell.delta_percent == 0.0

    # Each cell's brier matches the matching per-sector entry exactly.
    flu = by_key[("public_health", "flu_h2h")]
    assert flu.brier_a == pytest.approx(summary.per_sector_brier["public_health"], abs=1e-12)
    assert flu.brier_a == pytest.approx(summary.per_class_brier["flu_h2h"], abs=1e-12)

    rate = by_key[("economics", "rate_hike")]
    assert rate.brier_a == pytest.approx(summary.per_sector_brier["economics"], abs=1e-12)
    assert rate.brier_a == pytest.approx(summary.per_class_brier["rate_hike"], abs=1e-12)


# ---------------------------------------------------------------------------
# General layout: cross-product is more granular than either ScoreSummary map
# ---------------------------------------------------------------------------


def test_compare_self_matches_brier_contribution_avg_in_general_case(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Cross-product ``(sector, class_id)`` cells equal direct ``AVG`` of rows.

    When multiple classes share a sector, ``ScoreSummary.per_sector_brier``
    averages across all classes in the sector while ``compare_runs`` cells
    keep the classes separate. The expected per-cell ``brier_a`` /
    ``brier_b`` is therefore computed directly from the underlying
    ``brier_contribution`` rows, not from ScoreSummary.

    This test also asserts the documented convention difference: there
    exists at least one ``(sector, class_id)`` cell whose Brier value
    differs from ``ScoreSummary.per_sector_brier[sector]`` by design,
    confirming the cross-product is strictly finer than the per-sector
    breakdown.
    """
    run_id = "run-general"
    operations.insert_run(conn, _make_run(run_id))
    # Two classes share sector "public_health": flu_h2h (brier 0.10, 0.30 ->
    # AVG 0.20) and covid_h2h (brier 0.50). per_sector_brier['public_health']
    # = AVG(0.10, 0.30, 0.50) = 0.30 -> different from either cross-product
    # cell. per_class_brier['flu_h2h'] = 0.20, per_class_brier['covid_h2h']
    # = 0.50. Sector "economics" has only one class, so its cell does
    # collapse to the per-sector entry.
    rows: list[tuple[str, str, float]] = [
        ("public_health", "flu_h2h", 0.10),
        ("public_health", "flu_h2h", 0.30),
        ("public_health", "covid_h2h", 0.50),
        ("economics", "rate_hike", 0.04),
        ("economics", "rate_hike", 0.08),
    ]
    operations.insert_predictions_batch(
        conn,
        [
            _make_scored(
                run_id=run_id,
                prediction_id=f"p{i}",
                sector=sector,
                class_id=class_id,
                brier_contribution=brier,
            )
            for i, (sector, class_id, brier) in enumerate(rows)
        ],
    )

    summary = aggregate_run_summary(
        conn,
        run_id,
        bin_count_global=10,
        bin_count_per_sector={},
    )
    cells = compare_runs(conn, run_id, run_id)
    by_key = {(c.sector, c.class_id): c for c in cells}

    # Compute expected per-cell averages directly from the seeded rows.
    expected_cells: dict[tuple[str, str], float] = {
        ("economics", "rate_hike"): (0.04 + 0.08) / 2,
        ("public_health", "covid_h2h"): 0.50,
        ("public_health", "flu_h2h"): (0.10 + 0.30) / 2,
    }
    assert set(by_key) == set(expected_cells)

    for key, expected in expected_cells.items():
        cell = by_key[key]
        assert cell.present_in is PresentIn.BOTH, key
        assert cell.brier_a is not None
        assert cell.brier_b is not None
        # Aggregation parity: self-compare yields equal sides, both equal
        # the direct AVG of the underlying brier_contribution rows.
        assert cell.brier_a == pytest.approx(expected, abs=1e-12), key
        assert cell.brier_b == pytest.approx(expected, abs=1e-12), key
        assert cell.delta_absolute == 0.0, key
        assert cell.delta_percent == 0.0, key

    # Convention difference: per_sector_brier['public_health'] averages all
    # three rows in the sector, so it does NOT equal either cross-product
    # cell within the sector.
    expected_sector_avg = (0.10 + 0.30 + 0.50) / 3
    assert summary.per_sector_brier["public_health"] == pytest.approx(
        expected_sector_avg, abs=1e-12
    )
    assert by_key[("public_health", "flu_h2h")].brier_a != pytest.approx(
        summary.per_sector_brier["public_health"], abs=1e-9
    )
    assert by_key[("public_health", "covid_h2h")].brier_a != pytest.approx(
        summary.per_sector_brier["public_health"], abs=1e-9
    )

    # per_class_brier collapses across sectors, so per-class entries equal
    # the cross-product cell only because (in this fixture) each class
    # appears in exactly one sector. The match is incidental, not
    # structural — see degenerate-layout test for the structural case.
    assert summary.per_class_brier["flu_h2h"] == pytest.approx(
        by_key[("public_health", "flu_h2h")].brier_a, abs=1e-12
    )
    assert summary.per_class_brier["covid_h2h"] == pytest.approx(
        by_key[("public_health", "covid_h2h")].brier_a, abs=1e-12
    )

    # Single-class sector: cross-product cell does collapse to the
    # per-sector entry, demonstrating both regimes coexist within one run.
    assert by_key[("economics", "rate_hike")].brier_a == pytest.approx(
        summary.per_sector_brier["economics"], abs=1e-12
    )


# ---------------------------------------------------------------------------
# Self-compare delta property — independent of layout
# ---------------------------------------------------------------------------


def test_self_compare_all_deltas_zero(conn: duckdb.DuckDBPyConnection) -> None:
    """Every cell from a self-compare has ``delta_absolute == 0.0``.

    This is the cross-aggregator parity property: regardless of how
    classes and sectors are laid out, the canonical
    ``AVG(brier_contribution)`` aggregation in ``compare_runs`` matches
    itself, and the same row set drives ``aggregate_run_summary``. The
    integration is therefore correct iff every self-compare cell carries
    a zero absolute delta and (where ``brier_a > 0``) a zero percent
    delta. Skipped rows and ``brier_contribution IS NULL`` rows are
    ignored on both sides.
    """
    run_id = "run-mixed"
    operations.insert_run(conn, _make_run(run_id))
    operations.insert_predictions_batch(
        conn,
        [
            _make_scored(
                run_id=run_id,
                prediction_id="p1",
                sector="public_health",
                class_id="flu_h2h",
                brier_contribution=0.10,
            ),
            _make_scored(
                run_id=run_id,
                prediction_id="p2",
                sector="public_health",
                class_id="flu_h2h",
                brier_contribution=0.30,
            ),
            _make_scored(
                run_id=run_id,
                prediction_id="p3",
                sector="public_health",
                class_id="covid_h2h",
                brier_contribution=0.0,  # exercises brier_a == 0 -> delta_percent None
            ),
            _make_scored(
                run_id=run_id,
                prediction_id="p4",
                sector="economics",
                class_id="rate_hike",
                brier_contribution=0.20,
            ),
            # Skipped rows must not perturb the aggregation on either side.
            _make_skipped(
                run_id=run_id,
                prediction_id="s1",
                sector="public_health",
                class_id="flu_h2h",
            ),
            _make_skipped(
                run_id=run_id,
                prediction_id="s2",
                sector="climate",
                class_id="heatwave",
            ),
        ],
    )

    # Trigger ScoreSummary so the integration test exercises both
    # surfaces; we don't compare its values cell-for-cell here (that's
    # the previous test) but we do confirm both pipelines run without
    # raising on the same row set.
    summary = aggregate_run_summary(
        conn,
        run_id,
        bin_count_global=10,
        bin_count_per_sector={},
    )

    cells = compare_runs(conn, run_id, run_id)
    assert cells, "self-compare must surface at least one cell when scored rows exist"

    for cell in cells:
        assert cell.present_in is PresentIn.BOTH
        assert cell.brier_a is not None
        assert cell.brier_b is not None
        # Bit-for-bit equality on the same SQL aggregation -> delta == 0.
        assert cell.brier_a == cell.brier_b
        assert cell.delta_absolute == 0.0
        if cell.brier_a == 0.0:
            assert cell.delta_percent is None
        else:
            assert cell.delta_percent == 0.0

    # Skipped-only sector / class surfaces in zero-resolutions, not in
    # compare cells — the pipelines agree on which rows count as scored.
    assert "climate" in summary.zero_resolutions_sectors
    assert "heatwave" in summary.zero_resolutions_classes
    cell_sectors = {c.sector for c in cells}
    cell_classes = {c.class_id for c in cells}
    assert "climate" not in cell_sectors
    assert "heatwave" not in cell_classes
