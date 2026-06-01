"""T-CB-031 — ``razor-rooster calibration-backtest run`` dispatch tests.

Exercises the run command's renderer dispatch (``--format`` ->
terminal/markdown/html/json), the bare-run defaults that read
``MIN(resolution_ts)`` from ``polymarket_resolutions`` and the
pattern-library class registry, and the failure modes for an empty
resolution corpus or a zero-mapping JOIN.

The replay pipeline itself is exercised by
``tests/calibration_backtest/test_replay*.py`` — these tests stub
:func:`run_backtest` via ``monkeypatch.setattr`` so they assert the CLI
plumbing in isolation. Stubbing also lets the tests run against an
in-memory DuckDB without seeding the full upstream schema set the real
replay loop would query.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from click.testing import CliRunner

from razor_rooster.calibration_backtest import cli as cli_module
from razor_rooster.calibration_backtest.cli import calibration_backtest
from razor_rooster.calibration_backtest.engines.replay import ReplayResult
from razor_rooster.calibration_backtest.errors import BacktestConfigError
from razor_rooster.calibration_backtest.models import (
    BacktestRun,
    BacktestStatus,
    RunParameters,
)

_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
"""Pinned wall-clock for deterministic recent-window math (must match replay)."""


# ---------------------------------------------------------------------------
# Seed helpers — the CLI opens a real DuckDB file via ``_open_store`` and
# the smoke-count + bare-run defaults issue real SQL, so we seed a small
# corpus per test rather than stub every helper.
# ---------------------------------------------------------------------------


def _seed_polymarket_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str,
    resolution_ts: datetime,
    winning_outcome_label: str | None = "yes",
    invalidated: bool = False,
) -> None:
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


def _seed_class_market_mapping(
    conn: duckdb.DuckDBPyConnection,
    *,
    mapping_id: str,
    class_id: str,
    condition_id: str,
    polarity_value: str = "aligned",
    venue: str = "polymarket",
) -> None:
    conn.execute(
        "INSERT INTO class_market_mappings ("
        "mapping_id, class_id, condition_id, mapping_type, "
        "mapping_confidence, polarity, mapped_by, mapped_at, "
        "removed_at, notes, venue"
        ") VALUES (?, ?, ?, 'direct', 'high', ?, 'op', ?, NULL, NULL, ?)",
        [
            mapping_id,
            class_id,
            condition_id,
            polarity_value,
            datetime(2025, 1, 1, tzinfo=UTC),
            venue,
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


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    """Return a pre-seeded DuckDB file path with one mapped resolution.

    The CLI's ``_open_store`` runs every upstream migration when first
    opened, so the fixture only needs to seed the rows the test cares
    about. Because ``_open_store`` returns its own connection, we open
    a separate :func:`duckdb.connect` to seed the file before the CLI
    runs against it.
    """

    path = tmp_path / "trough.duckdb"
    # ``_open_store`` requires the DB to exist for non-help commands;
    # open + close the DuckDB store once to apply every migration,
    # then seed via the same connection before the CLI runs.
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
    finally:
        conn.close()
        store.close()
    yield path


@pytest.fixture
def empty_db_path(tmp_path: Path) -> Iterator[Path]:
    """A DuckDB file with the schema applied but no rows seeded."""

    path = tmp_path / "trough_empty.duckdb"
    conn, store = cli_module._open_store(path, require_exists=False)
    try:
        # No rows seeded — ``polymarket_resolutions`` is empty.
        pass
    finally:
        conn.close()
        store.close()
    yield path


# ---------------------------------------------------------------------------
# Stub helpers for the replay pipeline
# ---------------------------------------------------------------------------


def _make_synthetic_run(params: RunParameters, *, run_id: str = "stub-run-id") -> BacktestRun:
    """Build a fully-populated :class:`BacktestRun` matching ``params``.

    The renderers consume the synthesised run for byte-for-byte
    output testing without requiring the replay loop to actually run.
    """

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
def stub_run_backtest(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``cli.run_backtest`` with a stub that records the params it saw.

    Returns a dict that callers can inspect (``recorded["params"]``,
    ``recorded["calls"]``) to assert the run command resolved its
    inputs as expected.
    """

    recorded: dict[str, Any] = {"calls": 0, "params": None}

    def _fake(
        params: RunParameters,
        *,
        conn: duckdb.DuckDBPyConnection,
        store: Any,
        persistence_conn: duckdb.DuckDBPyConnection | None = None,
    ) -> ReplayResult:
        recorded["calls"] += 1
        recorded["params"] = params
        run = _make_synthetic_run(params)
        return ReplayResult(run=run, predictions=(), traces={})

    monkeypatch.setattr(cli_module, "run_backtest", _fake)
    return recorded


# ---------------------------------------------------------------------------
# T-CB-031 tests
# ---------------------------------------------------------------------------


def test_run_terminal_format_exits_zero(db_path: Path, stub_run_backtest: dict[str, Any]) -> None:
    """``--format terminal`` exits 0 and prints the disclaimer + headers."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "run",
            "--db",
            str(db_path),
            "--since",
            "2025-01-01T00:00:00+00:00",
            "--until",
            "2025-12-31T00:00:00+00:00",
            "--class-id",
            "cls-A",
            "--format",
            "terminal",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Disclaimer:" in result.output
    assert "Run header" in result.output
    assert stub_run_backtest["calls"] == 1


def test_run_markdown_format(db_path: Path, stub_run_backtest: dict[str, Any]) -> None:
    """``--format markdown`` prints a Markdown document with table rows."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "run",
            "--db",
            str(db_path),
            "--since",
            "2025-01-01T00:00:00+00:00",
            "--until",
            "2025-12-31T00:00:00+00:00",
            "--class-id",
            "cls-A",
            "--format",
            "markdown",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "# Calibration Backtest" in result.output
    assert "| field | value |" in result.output


def test_run_html_format(db_path: Path, stub_run_backtest: dict[str, Any]) -> None:
    """``--format html`` prints a complete HTML document."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "run",
            "--db",
            str(db_path),
            "--since",
            "2025-01-01T00:00:00+00:00",
            "--until",
            "2025-12-31T00:00:00+00:00",
            "--class-id",
            "cls-A",
            "--format",
            "html",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "<!DOCTYPE html>" in result.output
    assert "</html>" in result.output


def test_run_json_format_round_trip(db_path: Path, stub_run_backtest: dict[str, Any]) -> None:
    """``--format json`` prints a JSON document carrying the disclaimer."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "run",
            "--db",
            str(db_path),
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
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_id"] == "stub-run-id"
    assert payload["status"] == "complete"
    assert "disclaimer" in payload
    assert payload["lag_days"] == 7


def test_run_bare_uses_min_resolution_ts(db_path: Path, stub_run_backtest: dict[str, Any]) -> None:
    """Bare run derives ``since_ts`` from ``MIN(resolution_ts)`` and exits 0."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        ["run", "--db", str(db_path), "--allow-recent", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    params = stub_run_backtest["params"]
    assert isinstance(params, RunParameters)
    assert params.since_ts == datetime(2025, 6, 1, tzinfo=UTC)
    assert params.lag_days == 7
    assert params.venues == ("polymarket",)
    assert "cls-A" in params.class_ids


def test_run_bare_empty_resolutions_raises_config_error(
    empty_db_path: Path, stub_run_backtest: dict[str, Any]
) -> None:
    """Bare run on an empty corpus exits 1 with an actionable hint."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        ["run", "--db", str(empty_db_path), "--format", "terminal"],
    )
    assert result.exit_code == 1
    assert "polymarket_resolutions is empty" in result.output
    assert stub_run_backtest["calls"] == 0


def test_run_zero_mapped_resolutions_exits_one(
    tmp_path: Path, stub_run_backtest: dict[str, Any]
) -> None:
    """A populated corpus with no class_market_mappings exits 1."""

    path = tmp_path / "trough_no_mapping.duckdb"
    conn, store = cli_module._open_store(path, require_exists=False)
    try:
        _seed_polymarket_resolution(
            conn,
            condition_id="cond-99",
            resolution_ts=datetime(2025, 6, 1, tzinfo=UTC),
        )
        _seed_pattern_library_class(conn, class_id="cls-A")
        # No class_market_mappings rows -> JOIN yields zero rows.
    finally:
        conn.close()
        store.close()

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "run",
            "--db",
            str(path),
            "--since",
            "2025-01-01T00:00:00+00:00",
            "--until",
            "2025-12-31T00:00:00+00:00",
            "--class-id",
            "cls-A",
            "--format",
            "terminal",
        ],
    )
    assert result.exit_code == 1
    assert "no mapped resolutions" in result.output
    assert stub_run_backtest["calls"] == 0


def test_run_class_id_filter_passed_through(
    db_path: Path, stub_run_backtest: dict[str, Any]
) -> None:
    """Repeated ``--class-id`` flags forward the full list to ``run_backtest``."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "run",
            "--db",
            str(db_path),
            "--since",
            "2025-01-01T00:00:00+00:00",
            "--until",
            "2025-12-31T00:00:00+00:00",
            "--class-id",
            "cls-A",
            "--class-id",
            "cls-B",
            "--format",
            "terminal",
        ],
    )
    # cls-B has no mapping, but cls-A does — count > 0 so the smoke
    # check passes and the stub is invoked.
    assert result.exit_code == 0, result.output
    params = stub_run_backtest["params"]
    assert params is not None
    assert tuple(params.class_ids) == ("cls-A", "cls-B")


@pytest.mark.parametrize("flag_value", ["JSON", "json", "JsOn"])
def test_run_format_case_insensitive(
    db_path: Path,
    stub_run_backtest: dict[str, Any],
    flag_value: str,
) -> None:
    """``--format`` is case-insensitive: ``JSON``/``json``/``JsOn`` all work."""

    runner = CliRunner()
    result = runner.invoke(
        calibration_backtest,
        [
            "run",
            "--db",
            str(db_path),
            "--since",
            "2025-01-01T00:00:00+00:00",
            "--until",
            "2025-12-31T00:00:00+00:00",
            "--class-id",
            "cls-A",
            "--format",
            flag_value,
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_id"] == "stub-run-id"


def test_earliest_resolution_ts_returns_min(tmp_path: Path) -> None:
    """``_earliest_resolution_ts`` returns ``MIN(resolution_ts)`` over rows."""

    path = tmp_path / "min.duckdb"
    conn, store = cli_module._open_store(path, require_exists=False)
    try:
        _seed_polymarket_resolution(
            conn,
            condition_id="cond-A",
            resolution_ts=datetime(2025, 7, 1, tzinfo=UTC),
        )
        _seed_polymarket_resolution(
            conn,
            condition_id="cond-B",
            resolution_ts=datetime(2025, 5, 1, tzinfo=UTC),
        )
        earliest = cli_module._earliest_resolution_ts(conn)
        assert earliest == datetime(2025, 5, 1, tzinfo=UTC)
    finally:
        conn.close()
        store.close()


def test_earliest_resolution_ts_raises_on_empty(empty_db_path: Path) -> None:
    """Empty ``polymarket_resolutions`` raises ``BacktestConfigError`` cleanly."""

    conn = duckdb.connect(database=str(empty_db_path), read_only=True)
    try:
        with pytest.raises(BacktestConfigError, match="polymarket_resolutions is empty"):
            cli_module._earliest_resolution_ts(conn)
    finally:
        conn.close()
