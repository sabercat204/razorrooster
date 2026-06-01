"""T-CB-032 — ``list``/``show``/``compare``/``prune`` CLI subcommand tests.

Exercises the four read-side / maintenance subcommands against an
in-memory DuckDB seeded with synthetic ``backtest_runs``,
``backtest_predictions``, and ``backtest_traces`` rows. The tests pin the
contracts specified by T-CB-032:

* ``list`` orders by ``started_at DESC``, honours ``--limit`` and
  ``--since``, and renders a fixed-width table with truncated
  ``run_id`` / ``system_revision``.
* ``show`` renders an existing run via the same renderers as ``run``;
  a missing ``run_id`` triggers :class:`RunNotFoundError` and exit 1.
* ``compare`` produces zero deltas on a self-compare, ranks by
  absolute / percent magnitude, and respects ``--top``.
* ``prune`` refuses without ``--confirm`` (exit 1), and on confirmed
  invocation deletes runs whose ``started_at`` predates ``--before``,
  printing a deterministic summary line.

The CLI's ``_open_store`` helper runs every upstream migration when it
opens the DB, so the seed helpers only need to insert the rows the
test cares about. The ``CliRunner`` invocation is the only path that
exercises end-to-end click parsing and renderer dispatch — separate
unit tests for the engines / persistence layer live under
``test_compare*.py`` and ``test_prune_run.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from click.testing import CliRunner

from razor_rooster.calibration_backtest import cli as cli_module
from razor_rooster.calibration_backtest.cli import calibration_backtest
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
    BacktestTrace,
    CompressionAlgorithm,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
)
from razor_rooster.calibration_backtest.persistence import operations as persistence_ops

# ---------------------------------------------------------------------------
# Seed helpers — kept local so the fixture surface mirrors test_prune_run.py
# ---------------------------------------------------------------------------


_SINCE = datetime(2024, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2024, 6, 1, tzinfo=UTC)
_PRED_TS = datetime(2024, 1, 8, tzinfo=UTC)
_RES_TS = datetime(2024, 1, 15, tzinfo=UTC)


def _make_run(
    run_id: str,
    *,
    started_at: datetime,
    overall_brier: float | None = 0.18,
    library_version: int = 1,
    system_revision: str = "deadbeefcafef00d",
    predictions_total: int = 2,
    predictions_scored: int = 2,
    predictions_skipped: int = 0,
    fallback_polarity_count: int = 0,
    status: BacktestStatus = BacktestStatus.COMPLETE,
    completed_at: datetime | None = None,
    summary_json: dict[str, Any] | None = None,
) -> BacktestRun:
    if completed_at is None and status is BacktestStatus.COMPLETE:
        completed_at = started_at
    if summary_json is None and status is BacktestStatus.COMPLETE:
        summary_json = {
            "fallback_polarity_count": fallback_polarity_count,
            "fallback_polarity_rate": (
                fallback_polarity_count / predictions_scored if predictions_scored else 0.0
            ),
            "overall_brier": overall_brier or 0.0,
            "per_class_brier": {"cls-A": overall_brier or 0.0},
            "per_sector_brier": {"public_health": overall_brier or 0.0},
            "reliability_diagrams": {},
            "zero_resolutions_classes": [],
            "zero_resolutions_sectors": [],
        }
    return BacktestRun(
        run_id=run_id,
        since_ts=_SINCE,
        until_ts=_UNTIL,
        lag_days=7,
        class_ids=("cls-A",),
        sectors=("public_health",),
        venues=("polymarket",),
        library_version=library_version,
        system_revision=system_revision,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        error_summary=None,
        predictions_total=predictions_total,
        predictions_scored=predictions_scored,
        predictions_skipped=predictions_skipped,
        overall_brier=overall_brier,
        summary_json=summary_json,
        bin_count_global=10,
        bin_count_per_sector={},
        fallback_polarity_count=fallback_polarity_count,
        allow_recent=False,
        disclaimer_version="v1",
    )


def _make_prediction(
    run_id: str,
    prediction_id: str,
    *,
    sector: str = "public_health",
    class_id: str = "cls-A",
    brier_contribution: float = 0.20,
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


def _make_trace(run_id: str, prediction_id: str) -> BacktestTrace:
    return BacktestTrace(
        run_id=run_id,
        prediction_id=prediction_id,
        trace_json_compressed=b"\x28\xb5\x2f\xfd\x00\x01\x02\x03",
        decompressed_size_bytes=512,
        compression_algorithm=CompressionAlgorithm.ZSTD,
    )


def _seed_run(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    started_at: datetime,
    brier_contributions: tuple[float, ...] = (0.20,),
    sectors: tuple[str, ...] | None = None,
    class_ids: tuple[str, ...] | None = None,
    overall_brier: float | None = None,
) -> None:
    """Insert one run with len(brier_contributions) scored predictions."""

    if overall_brier is None and brier_contributions:
        overall_brier = sum(brier_contributions) / len(brier_contributions)
    sector_list = sectors or ("public_health",) * len(brier_contributions)
    class_list = class_ids or ("cls-A",) * len(brier_contributions)
    persistence_ops.insert_run(
        conn,
        _make_run(
            run_id,
            started_at=started_at,
            overall_brier=overall_brier,
            predictions_total=len(brier_contributions),
            predictions_scored=len(brier_contributions),
        ),
    )
    # The run was inserted with status=COMPLETE; flip via update would
    # fail (only IN_PROGRESS -> COMPLETE/FAILED is allowed). Insert
    # directly with the desired terminal status by bypassing the
    # in_progress staging — _make_run already builds COMPLETE rows with
    # summary_json populated.
    for idx, contrib in enumerate(brier_contributions, start=1):
        persistence_ops.insert_prediction(
            conn,
            _make_prediction(
                run_id,
                f"pred-{idx:03d}",
                sector=sector_list[idx - 1],
                class_id=class_list[idx - 1],
                brier_contribution=contrib,
            ),
        )
        persistence_ops.insert_trace(conn, _make_trace(run_id, f"pred-{idx:03d}"))


@pytest.fixture
def seeded_db_path(tmp_path: Path) -> Iterator[Path]:
    """Yield a DuckDB file with three completed runs at staggered timestamps."""

    path = tmp_path / "trough.duckdb"
    conn, store = cli_module._open_store(path, require_exists=False)
    try:
        _seed_run(
            conn,
            "run-aaaaaaaaaaaa",
            started_at=datetime(2024, 1, 10, 12, 0, tzinfo=UTC),
            brier_contributions=(0.10, 0.20),
            overall_brier=0.15,
        )
        _seed_run(
            conn,
            "run-bbbbbbbbbbbb",
            started_at=datetime(2024, 2, 10, 12, 0, tzinfo=UTC),
            brier_contributions=(0.30, 0.40),
            overall_brier=0.35,
        )
        _seed_run(
            conn,
            "run-cccccccccccc",
            started_at=datetime(2024, 3, 10, 12, 0, tzinfo=UTC),
            brier_contributions=(0.05, 0.10),
            overall_brier=0.075,
        )
    finally:
        conn.close()
        store.close()
    yield path


@pytest.fixture
def empty_db_path(tmp_path: Path) -> Iterator[Path]:
    """Yield a DuckDB file with the schema applied but no runs seeded."""

    path = tmp_path / "trough_empty.duckdb"
    conn, store = cli_module._open_store(path, require_exists=False)
    try:
        pass
    finally:
        conn.close()
        store.close()
    yield path


# ---------------------------------------------------------------------------
# list — ordering, --limit, --since
# ---------------------------------------------------------------------------


def test_list_renders_runs_in_started_at_desc_order(seeded_db_path: Path) -> None:
    """``list`` returns runs ordered ``started_at DESC`` (newest first)."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        ["list", "--db", str(seeded_db_path), "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    run_ids = [row["run_id"] for row in payload["runs"]]
    assert run_ids == [
        "run-cccccccccccc",
        "run-bbbbbbbbbbbb",
        "run-aaaaaaaaaaaa",
    ]


def test_list_limit_caps_results(seeded_db_path: Path) -> None:
    """``--limit 1`` returns only the most recent run."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        ["list", "--db", str(seeded_db_path), "--limit", "1", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["runs"]) == 1
    assert payload["runs"][0]["run_id"] == "run-cccccccccccc"


def test_list_since_filter(seeded_db_path: Path) -> None:
    """``--since`` excludes runs whose ``started_at`` precedes the cutoff."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "list",
            "--db",
            str(seeded_db_path),
            "--since",
            "2024-02-01T00:00:00+00:00",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    run_ids = sorted(row["run_id"] for row in payload["runs"])
    assert run_ids == ["run-bbbbbbbbbbbb", "run-cccccccccccc"]


def test_list_terminal_passes_through_linter(seeded_db_path: Path) -> None:
    """Terminal-format ``list`` passes the framing linter and shows table chrome."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        ["list", "--db", str(seeded_db_path), "--format", "terminal"],
    )
    assert result.exit_code == 0, result.output
    assert "Calibration Backtest runs" in result.output
    assert "run_id" in result.output
    assert "started_at" in result.output


def test_list_empty_table_renders_no_runs_message(empty_db_path: Path) -> None:
    """An empty table renders a friendly message rather than an empty grid."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        ["list", "--db", str(empty_db_path), "--format", "terminal"],
    )
    assert result.exit_code == 0, result.output
    assert "(no runs found)" in result.output


# ---------------------------------------------------------------------------
# show — existing run, missing run
# ---------------------------------------------------------------------------


def test_show_existing_run_renders(seeded_db_path: Path) -> None:
    """``show`` against an existing run renders the disclaimer + run header."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "show",
            "run-aaaaaaaaaaaa",
            "--db",
            str(seeded_db_path),
            "--format",
            "terminal",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Disclaimer:" in result.output
    assert "Run header" in result.output
    # The truncated run_id renders without the full string.
    assert "run-aaaaaaaa" in result.output


def test_show_existing_run_json_format(seeded_db_path: Path) -> None:
    """``show --format json`` round-trips through :func:`json.loads`."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "show",
            "run-aaaaaaaaaaaa",
            "--db",
            str(seeded_db_path),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_id"] == "run-aaaaaaaaaaaa"
    assert payload["status"] == "complete"


def test_show_missing_run_raises_runnotfounderror_exit_1(
    seeded_db_path: Path,
) -> None:
    """A missing ``run_id`` exits 1 with a deterministic ``Run not found`` message."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "show",
            "ghost-run-id",
            "--db",
            str(seeded_db_path),
            "--format",
            "terminal",
        ],
    )
    assert result.exit_code == 1
    assert "Run not found: ghost-run-id" in result.output


# ---------------------------------------------------------------------------
# compare — self-compare zeros, --compare-rank-by, --top
# ---------------------------------------------------------------------------


def test_compare_self_compare_zero_deltas(seeded_db_path: Path) -> None:
    """Self-compare returns zero deltas on every BOTH cell."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "compare",
            "run-aaaaaaaaaaaa",
            "run-aaaaaaaaaaaa",
            "--db",
            str(seeded_db_path),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["cells"], "self-compare must produce at least one cell"
    for cell in payload["cells"]:
        assert cell["present_in"] == "both"
        assert cell["delta_absolute"] == pytest.approx(0.0)
        assert cell["delta_percent"] == pytest.approx(0.0)


def test_compare_rank_by_absolute(tmp_path: Path) -> None:
    """``rank_by='absolute'`` orders cells by ``abs(delta_absolute)`` descending."""

    path = tmp_path / "compare_abs.duckdb"
    conn, store = cli_module._open_store(path, require_exists=False)
    try:
        # Run A: brier_a is 0.10 / 0.20 / 0.30 across three sectors.
        _seed_run(
            conn,
            "run-A",
            started_at=datetime(2024, 1, 10, tzinfo=UTC),
            brier_contributions=(0.10, 0.20, 0.30),
            sectors=("alpha", "beta", "gamma"),
            class_ids=("cls-A",) * 3,
        )
        # Run B widens the gap on gamma the most (delta=0.40), beta moderately
        # (delta=0.20), alpha the least (delta=0.05).
        _seed_run(
            conn,
            "run-B",
            started_at=datetime(2024, 1, 11, tzinfo=UTC),
            brier_contributions=(0.15, 0.40, 0.70),
            sectors=("alpha", "beta", "gamma"),
            class_ids=("cls-A",) * 3,
        )
    finally:
        conn.close()
        store.close()

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "compare",
            "run-A",
            "run-B",
            "--db",
            str(path),
            "--compare-rank-by",
            "absolute",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    cells = json.loads(result.output)["cells"]
    sectors_order = [c["sector"] for c in cells]
    # Largest |delta| first: gamma (+0.40), beta (+0.20), alpha (+0.05).
    assert sectors_order == ["gamma", "beta", "alpha"]


def test_compare_rank_by_percent(tmp_path: Path) -> None:
    """``rank_by='percent'`` orders cells by ``abs(delta_percent)`` descending."""

    path = tmp_path / "compare_pct.duckdb"
    conn, store = cli_module._open_store(path, require_exists=False)
    try:
        # alpha small absolute delta but huge % (0.01 -> 0.05 = +400%).
        # beta:  0.50 -> 0.55 = +10%.
        # gamma: 0.20 -> 0.30 = +50%.
        _seed_run(
            conn,
            "run-A",
            started_at=datetime(2024, 1, 10, tzinfo=UTC),
            brier_contributions=(0.01, 0.50, 0.20),
            sectors=("alpha", "beta", "gamma"),
            class_ids=("cls-A",) * 3,
        )
        _seed_run(
            conn,
            "run-B",
            started_at=datetime(2024, 1, 11, tzinfo=UTC),
            brier_contributions=(0.05, 0.55, 0.30),
            sectors=("alpha", "beta", "gamma"),
            class_ids=("cls-A",) * 3,
        )
    finally:
        conn.close()
        store.close()

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "compare",
            "run-A",
            "run-B",
            "--db",
            str(path),
            "--compare-rank-by",
            "percent",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    cells = json.loads(result.output)["cells"]
    sectors_order = [c["sector"] for c in cells]
    # Largest |delta_percent|: alpha (+400%), gamma (+50%), beta (+10%).
    assert sectors_order == ["alpha", "gamma", "beta"]


def test_compare_top_n_slices(tmp_path: Path) -> None:
    """``--top N`` truncates the rendered list to the first N ranked cells."""

    path = tmp_path / "compare_top.duckdb"
    conn, store = cli_module._open_store(path, require_exists=False)
    try:
        _seed_run(
            conn,
            "run-A",
            started_at=datetime(2024, 1, 10, tzinfo=UTC),
            brier_contributions=(0.10, 0.20, 0.30, 0.40),
            sectors=("s1", "s2", "s3", "s4"),
            class_ids=("cls-A",) * 4,
        )
        _seed_run(
            conn,
            "run-B",
            started_at=datetime(2024, 1, 11, tzinfo=UTC),
            brier_contributions=(0.15, 0.30, 0.55, 0.50),
            sectors=("s1", "s2", "s3", "s4"),
            class_ids=("cls-A",) * 4,
        )
    finally:
        conn.close()
        store.close()

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "compare",
            "run-A",
            "run-B",
            "--db",
            str(path),
            "--compare-rank-by",
            "absolute",
            "--top",
            "2",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    cells = json.loads(result.output)["cells"]
    assert len(cells) == 2


# ---------------------------------------------------------------------------
# prune — without --confirm, with --confirm, summary format
# ---------------------------------------------------------------------------


def test_prune_without_confirm_exits_1_with_warning(seeded_db_path: Path) -> None:
    """Without ``--confirm`` the command refuses and exits 1 with a warning."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "prune",
            "--db",
            str(seeded_db_path),
            "--before",
            "2025-01-01T00:00:00+00:00",
        ],
    )
    assert result.exit_code == 1
    assert "refusing to prune without --confirm" in result.output

    # The seeded rows are still present.
    conn = duckdb.connect(database=str(seeded_db_path), read_only=True)
    try:
        row = conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()
        assert row is not None
        assert int(row[0]) == 3
    finally:
        conn.close()


def test_prune_with_confirm_deletes_old_runs(seeded_db_path: Path) -> None:
    """``--confirm`` deletes runs older than ``--before`` and prints a summary."""

    runner = CliRunner()
    # Cutoff falls between run-aaaaaaaa (Jan 10) and run-bbbbbbbb (Feb 10) so
    # only the earliest run is pruned.
    result = runner.invoke(
        calibration_backtest,
        [
            "prune",
            "--db",
            str(seeded_db_path),
            "--before",
            "2024-02-01T00:00:00+00:00",
            "--confirm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Pruned 1 runs / 2 predictions / 2 traces" in result.output

    conn = duckdb.connect(database=str(seeded_db_path), read_only=True)
    try:
        rows = conn.execute("SELECT run_id FROM backtest_runs ORDER BY started_at ASC").fetchall()
        run_ids = [row[0] for row in rows]
        assert run_ids == ["run-bbbbbbbbbbbb", "run-cccccccccccc"]

        preds = conn.execute(
            "SELECT COUNT(*) FROM backtest_predictions WHERE run_id = ?",
            ["run-aaaaaaaaaaaa"],
        ).fetchone()
        assert preds is not None
        assert int(preds[0]) == 0
    finally:
        conn.close()


def test_prune_summary_format(seeded_db_path: Path) -> None:
    """The summary line carries the canonical ``runs / predictions / traces`` shape."""

    runner = CliRunner()
    # Cutoff after every seeded run so all three are deleted.
    result = runner.invoke(
        calibration_backtest,
        [
            "prune",
            "--db",
            str(seeded_db_path),
            "--before",
            "2025-01-01T00:00:00+00:00",
            "--confirm",
        ],
    )
    assert result.exit_code == 0, result.output
    # Three runs * two predictions/traces each.
    assert "Pruned 3 runs / 6 predictions / 6 traces" in result.output

    conn = duckdb.connect(database=str(seeded_db_path), read_only=True)
    try:
        for table in ("backtest_runs", "backtest_predictions", "backtest_traces"):
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            assert row is not None
            assert int(row[0]) == 0, f"{table} should be empty after prune"
    finally:
        conn.close()


def test_prune_no_matching_runs_emits_zero_summary(seeded_db_path: Path) -> None:
    """A cutoff older than every run prunes nothing and reports zeros."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "prune",
            "--db",
            str(seeded_db_path),
            "--before",
            "2020-01-01T00:00:00+00:00",
            "--confirm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Pruned 0 runs / 0 predictions / 0 traces" in result.output
