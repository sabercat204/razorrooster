"""T-CB-034 — end-to-end framing audit for the calibration-backtest CLI.

These tests exercise every operator-facing rendered surface
(``run``/``list``/``show``/``compare``/``prune`` x terminal/markdown/
html/json) through :func:`razor_rooster.calibration_backtest.frame.check_cli_framing`
to guarantee no rendered output ever leaks an imperative phrase from
``config/forbidden_phrases.yaml``.

Three additional contracts are pinned alongside the bulk audit:

* The terminal/markdown/html surfaces embed the canonical disclaimer
  string verbatim (REQ-CB-CLI-002).
* The JSON surface carries the disclaimer as a top-level
  ``disclaimer`` field on the ``run``/``show`` payloads
  (REQ-CB-CLI-003).
* The framing catalog is loaded from the absolute repo-root path at
  module import time, so importing
  :mod:`razor_rooster.calibration_backtest.frame` from a non-repo CWD
  still yields the full ~42-phrase YAML catalog rather than the
  10-phrase upstream fallback.

The renderers are stubbed for ``run`` via ``monkeypatch.setattr`` so
the test can drive the full ``--format`` matrix without standing up
the upstream replay pipeline; ``list``/``show``/``compare``/``prune``
seed real ``backtest_runs``/``backtest_predictions``/
``backtest_traces`` rows so their renderers exercise the same SQL paths
production hits.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from click.testing import CliRunner

from razor_rooster.calibration_backtest import cli as cli_module
from razor_rooster.calibration_backtest import frame as frame_module
from razor_rooster.calibration_backtest.cli import calibration_backtest
from razor_rooster.calibration_backtest.engines.replay import ReplayResult
from razor_rooster.calibration_backtest.frame import (
    DISCLAIMER,
    check_cli_framing,
)
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
    BacktestTrace,
    CompressionAlgorithm,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
    RunParameters,
)
from razor_rooster.calibration_backtest.persistence import operations as persistence_ops

_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
"""Pinned wall-clock for deterministic test output."""

_SINCE = datetime(2024, 1, 1, tzinfo=UTC)
_UNTIL = datetime(2024, 6, 1, tzinfo=UTC)
_PRED_TS = datetime(2024, 1, 8, tzinfo=UTC)
_RES_TS = datetime(2024, 1, 15, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Seed helpers — kept local so the framing audit does not depend on test
# fixtures defined in sibling files.
# ---------------------------------------------------------------------------


def _seed_polymarket_resolution(
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


def _seed_class_market_mapping(
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
        ") VALUES (?, ?, ?, 'direct', 'high', 'aligned', 'op', ?, "
        "NULL, NULL, 'polymarket')",
        [
            mapping_id,
            class_id,
            condition_id,
            datetime(2025, 1, 1, tzinfo=UTC),
        ],
    )


def _seed_pattern_library_class(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str = "cls-A",
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO pl_event_classes ("
        "class_id, title, description, domain_sector, secondary_sectors, "
        "definition_version, outcome_type, registered_at, "
        "last_evaluated_at, library_version_at_last_eval, removed_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        [
            class_id,
            "Test class",
            "A test event class",
            "public_health",
            json.dumps([]),
            1,
            "binary",
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2025, 1, 1, tzinfo=UTC),
            1,
        ],
    )


def _make_run(
    run_id: str,
    *,
    started_at: datetime,
    overall_brier: float | None = 0.18,
) -> BacktestRun:
    summary_json: dict[str, Any] = {
        "fallback_polarity_count": 0,
        "fallback_polarity_rate": 0.0,
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
        library_version=1,
        system_revision="deadbeefcafef00d",
        started_at=started_at,
        completed_at=started_at,
        status=BacktestStatus.COMPLETE,
        error_summary=None,
        predictions_total=2,
        predictions_scored=2,
        predictions_skipped=0,
        overall_brier=overall_brier,
        summary_json=summary_json,
        bin_count_global=10,
        bin_count_per_sector={},
        fallback_polarity_count=0,
        allow_recent=False,
        disclaimer_version="v1",
    )


def _make_prediction(
    run_id: str,
    prediction_id: str,
    *,
    brier_contribution: float = 0.20,
) -> BacktestPrediction:
    return BacktestPrediction(
        run_id=run_id,
        prediction_id=prediction_id,
        class_id="cls-A",
        condition_id=f"cond-{prediction_id}",
        venue="polymarket",
        sector="public_health",
        prediction_ts=_PRED_TS,
        resolution_ts=_RES_TS,
        model_p=0.4,
        observed=1.0,
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


def _seed_run_with_predictions(
    conn: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    started_at: datetime,
    brier_contributions: tuple[float, ...] = (0.10, 0.20),
) -> None:
    overall_brier = sum(brier_contributions) / len(brier_contributions)
    persistence_ops.insert_run(
        conn,
        _make_run(run_id, started_at=started_at, overall_brier=overall_brier),
    )
    for idx, contrib in enumerate(brier_contributions, start=1):
        prediction_id = f"pred-{run_id}-{idx:03d}"
        persistence_ops.insert_prediction(
            conn,
            _make_prediction(run_id, prediction_id, brier_contribution=contrib),
        )
        persistence_ops.insert_trace(conn, _make_trace(run_id, prediction_id))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_db_path(tmp_path: Path) -> Iterator[Path]:
    """A DuckDB file with one polymarket resolution + two seeded runs."""

    path = tmp_path / "trough_framing.duckdb"
    conn, store = cli_module._open_store(path, require_exists=False)
    try:
        _seed_polymarket_resolution(
            conn,
            condition_id="cond-1",
            resolution_ts=datetime(2025, 6, 1, tzinfo=UTC),
        )
        _seed_class_market_mapping(
            conn,
            mapping_id="m-1",
            class_id="cls-A",
            condition_id="cond-1",
        )
        _seed_pattern_library_class(conn, class_id="cls-A")
        _seed_run_with_predictions(
            conn,
            "run-aaaaaaaaaaaa",
            started_at=datetime(2024, 1, 10, 12, 0, tzinfo=UTC),
            brier_contributions=(0.10, 0.20),
        )
        _seed_run_with_predictions(
            conn,
            "run-bbbbbbbbbbbb",
            started_at=datetime(2024, 2, 10, 12, 0, tzinfo=UTC),
            brier_contributions=(0.30, 0.40),
        )
    finally:
        conn.close()
        store.close()
    yield path


def _make_synthetic_run(params: RunParameters, *, run_id: str = "stub-run-id") -> BacktestRun:
    return BacktestRun(
        run_id=run_id,
        since_ts=params.since_ts,
        until_ts=params.until_ts,
        lag_days=params.lag_days,
        class_ids=params.class_ids,
        sectors=params.sectors,
        venues=params.venues,
        library_version=1,
        system_revision="abcdef0123456789",
        started_at=_NOW - timedelta(minutes=1),
        completed_at=_NOW,
        status=BacktestStatus.COMPLETE,
        error_summary=None,
        predictions_total=2,
        predictions_scored=2,
        predictions_skipped=0,
        overall_brier=0.18,
        summary_json={
            "fallback_polarity_count": 0,
            "fallback_polarity_rate": 0.0,
            "overall_brier": 0.18,
            "per_class_brier": {"cls-A": 0.18},
            "per_sector_brier": {"public_health": 0.18},
            "reliability_diagrams": {},
            "zero_resolutions_classes": [],
            "zero_resolutions_sectors": [],
        },
        bin_count_global=10,
        bin_count_per_sector={},
        fallback_polarity_count=0,
        allow_recent=params.allow_recent,
        disclaimer_version="v1",
    )


@pytest.fixture
def stub_run_backtest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``cli.run_backtest`` so the run command renders without replay."""

    def _fake(
        params: RunParameters,
        *,
        conn: duckdb.DuckDBPyConnection,
        store: Any,
        persistence_conn: duckdb.DuckDBPyConnection | None = None,
    ) -> ReplayResult:
        run = _make_synthetic_run(params)
        return ReplayResult(run=run, predictions=(), traces={})

    monkeypatch.setattr(cli_module, "run_backtest", _fake)


# ---------------------------------------------------------------------------
# T-CB-034 — every rendered output must pass the framing linter.
# ---------------------------------------------------------------------------


_FORMATS: tuple[str, ...] = ("terminal", "markdown", "html", "json")


def _invoke(args: list[str]) -> str:
    """Run ``calibration-backtest`` with ``args``; assert exit 0; return output."""

    runner = CliRunner()
    result = runner.invoke(calibration_backtest, args)
    assert result.exit_code == 0, (
        f"args={args!r} exited {result.exit_code} with output:\n{result.output}"
    )
    return result.output


def test_run_renders_pass_linter(
    seeded_db_path: Path,
    stub_run_backtest: None,
) -> None:
    """Every ``--format`` of ``run`` produces output the linter accepts."""

    for fmt in _FORMATS:
        output = _invoke(
            [
                "run",
                "--db",
                str(seeded_db_path),
                "--since",
                "2025-01-01T00:00:00+00:00",
                "--until",
                "2025-12-31T00:00:00+00:00",
                "--class-id",
                "cls-A",
                "--format",
                fmt,
            ],
        )
        # JSON is bypassed by the production renderer; checking it here
        # is a strict super-set of the live linter contract.
        check_cli_framing(output)


def test_list_renders_pass_linter(seeded_db_path: Path) -> None:
    """Every ``--format`` of ``list`` produces output the linter accepts."""

    for fmt in _FORMATS:
        output = _invoke(["list", "--db", str(seeded_db_path), "--format", fmt])
        check_cli_framing(output)


def test_show_renders_pass_linter(seeded_db_path: Path) -> None:
    """Every ``--format`` of ``show`` produces output the linter accepts."""

    for fmt in _FORMATS:
        output = _invoke(
            [
                "show",
                "run-aaaaaaaaaaaa",
                "--db",
                str(seeded_db_path),
                "--format",
                fmt,
            ],
        )
        check_cli_framing(output)


def test_compare_renders_pass_linter(seeded_db_path: Path) -> None:
    """Every ``--format`` of ``compare`` produces output the linter accepts."""

    for fmt in _FORMATS:
        output = _invoke(
            [
                "compare",
                "run-aaaaaaaaaaaa",
                "run-bbbbbbbbbbbb",
                "--db",
                str(seeded_db_path),
                "--format",
                fmt,
            ],
        )
        check_cli_framing(output)


def test_prune_summary_passes_linter(seeded_db_path: Path) -> None:
    """The ``prune`` summary line passes the framing linter.

    ``prune`` does not expose a ``--format`` switch; the single
    deterministic summary string must still be free of imperative
    phrases. Using a far-future cutoff ensures the seeded rows are
    deleted so the summary covers the non-empty branch.
    """

    output = _invoke(
        [
            "prune",
            "--db",
            str(seeded_db_path),
            "--before",
            "2099-01-01T00:00:00+00:00",
            "--confirm",
        ],
    )
    check_cli_framing(output)


# ---------------------------------------------------------------------------
# Disclaimer presence checks (REQ-CB-CLI-002, REQ-CB-CLI-003).
# ---------------------------------------------------------------------------


_DISCLAIMER_FRAGMENT = "Paper-analysis remains the v1 contract"
"""Stable substring of :data:`DISCLAIMER` used to assert verbatim embedding.

Chosen short enough to survive the terminal renderer's 72-character
line wrap (the longer fragments split across a newline). Hard-coded
rather than computed from ``DISCLAIMER`` so a renderer that silently
mangles the text (HTML-escapes punctuation, drops a clause, etc.)
still trips the assertion.
"""


def test_disclaimer_in_terminal_markdown_html(
    seeded_db_path: Path,
    stub_run_backtest: None,
) -> None:
    """Terminal, Markdown, and HTML ``run`` outputs embed the disclaimer text."""

    assert _DISCLAIMER_FRAGMENT in DISCLAIMER, "DISCLAIMER fragment drift; update the test constant"
    for fmt in ("terminal", "markdown", "html"):
        output = _invoke(
            [
                "run",
                "--db",
                str(seeded_db_path),
                "--since",
                "2025-01-01T00:00:00+00:00",
                "--until",
                "2025-12-31T00:00:00+00:00",
                "--class-id",
                "cls-A",
                "--format",
                fmt,
            ],
        )
        assert _DISCLAIMER_FRAGMENT in output, (
            f"--format {fmt!r} omitted the disclaimer fragment; output={output!r}"
        )


def test_disclaimer_field_in_json(
    seeded_db_path: Path,
    stub_run_backtest: None,
) -> None:
    """The JSON ``run`` output carries the disclaimer as a top-level field."""

    output = _invoke(
        [
            "run",
            "--db",
            str(seeded_db_path),
            "--since",
            "2025-01-01T00:00:00+00:00",
            "--until",
            "2025-12-31T00:00:00+00:00",
            "--class-id",
            "cls-A",
            "--format",
            "json",
        ],
    )
    payload = json.loads(output)
    assert "disclaimer" in payload, "JSON payload missing top-level disclaimer field"
    assert payload["disclaimer"] == DISCLAIMER, (
        "JSON disclaimer must equal the canonical DISCLAIMER constant verbatim"
    )


# ---------------------------------------------------------------------------
# CWD-independence — frame.py must absolute-resolve the catalog path.
# ---------------------------------------------------------------------------


def test_cwd_independence(tmp_path: Path) -> None:
    """Reimporting frame from a non-repo CWD must keep the full YAML catalog.

    ``position_engine.frame.linter.DEFAULT_CATALOG_PATH`` is
    CWD-relative; if :mod:`razor_rooster.calibration_backtest.frame`
    relied on it, importing from any other directory would silently
    fall back to the 10-phrase upstream default. The module instead
    resolves an absolute path against ``parents[3]`` so the catalog
    keeps its full ~42-phrase coverage everywhere it is imported.
    """

    original_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        reloaded = importlib.reload(frame_module)
        assert len(reloaded._LINTER_CATALOG.phrases) > 10, (
            "frame catalog under-loaded from non-repo CWD; "
            f"phrase count={len(reloaded._LINTER_CATALOG.phrases)}"
        )
    finally:
        os.chdir(original_cwd)
        importlib.reload(frame_module)


# ---------------------------------------------------------------------------
# --help smoke tests via CliRunner (NOT bash — pytest preserves coverage).
# ---------------------------------------------------------------------------


def test_top_level_help_exits_zero() -> None:
    """``razor-rooster --help`` exits 0 and lists ``calibration-backtest``."""

    from razor_rooster.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0, result.output
    assert "calibration-backtest" in result.output


def test_calibration_backtest_help_lists_all_subcommands() -> None:
    """The subgroup help banner lists all five subcommands."""

    from razor_rooster.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["calibration-backtest", "--help"])
    assert result.exit_code == 0, result.output
    for subcommand in ("run", "list", "show", "compare", "prune"):
        assert subcommand in result.output, f"--help banner missing subcommand {subcommand!r}"


def test_calibration_backtest_run_help_lists_all_flags() -> None:
    """``run --help`` enumerates every flag the operator may pass."""

    from razor_rooster.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["calibration-backtest", "run", "--help"])
    assert result.exit_code == 0, result.output
    for flag in (
        "--since",
        "--until",
        "--lag-days",
        "--class-id",
        "--sector",
        "--venue",
        "--bin-count",
        "--bin-count-per-sector",
        "--allow-recent",
        "--format",
        "--db",
    ):
        assert flag in result.output, f"missing flag {flag!r} in --help output"
