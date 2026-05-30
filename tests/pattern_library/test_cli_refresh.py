"""T-PL-051 — refresh + eval CLI subcommand tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import pytest
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library.cli import pattern_library
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    Sector,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.pattern_library.registry import (
    _clear_for_tests,
    _set_discovered_for_tests,
    register,
)


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    _clear_for_tests()
    _set_discovered_for_tests(True)
    yield
    _clear_for_tests()


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "pl_cli_refresh.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_pattern_library_migrations(conn)
    s.close()
    return db_path


def _make_class(
    *,
    class_id: str = "alpha",
    n_occurrences: int = 12,
) -> EventClass:
    occurrences = [datetime(2014 + i, 6, 1, tzinfo=UTC) for i in range(n_occurrences)]
    df = pd.DataFrame({"occurrence_ts": occurrences})

    def query(_conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
        return df

    return EventClass(
        class_id=class_id,
        title=f"{class_id} title",
        description=f"{class_id} description",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=query,
        baseline_sample_size=50,
        refractory_months=3,
        base_rate_window_default=timedelta(days=365 * 30),
    )


# -- refresh subcommand --------------------------------------------------


def test_refresh_runs_against_registered_classes(store_path: Path) -> None:
    register(_make_class())
    runner = CliRunner()
    result = runner.invoke(
        pattern_library,
        ["refresh", "--db", str(store_path), "--max-workers", "1"],
    )
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "ok" in result.output


def test_refresh_with_only_class_filter(store_path: Path) -> None:
    register(_make_class(class_id="alpha"))
    register(_make_class(class_id="beta"))
    runner = CliRunner()
    result = runner.invoke(
        pattern_library,
        [
            "refresh",
            "--class",
            "alpha",
            "--db",
            str(store_path),
            "--max-workers",
            "1",
        ],
    )
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "beta" not in result.output


def test_refresh_force_bumps_version(store_path: Path) -> None:
    register(_make_class())
    runner = CliRunner()
    runner.invoke(pattern_library, ["refresh", "--db", str(store_path), "--max-workers", "1"])
    result = runner.invoke(
        pattern_library,
        ["refresh", "--db", str(store_path), "--force", "--max-workers", "1"],
    )
    assert result.exit_code == 0
    assert "code_change" in result.output


def test_refresh_with_no_classes_succeeds(store_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        pattern_library,
        ["refresh", "--db", str(store_path), "--max-workers", "1"],
    )
    assert result.exit_code == 0
    assert "classes processed: 0" in result.output


def test_refresh_with_missing_db_returns_error(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        pattern_library,
        ["refresh", "--db", str(tmp_path / "missing.duckdb")],
    )
    assert result.exit_code == 1


# -- eval subcommand -----------------------------------------------------


def test_eval_class_prints_base_rate(store_path: Path) -> None:
    register(_make_class())
    runner = CliRunner()
    result = runner.invoke(
        pattern_library,
        ["eval", "alpha", "--db", str(store_path)],
    )
    assert result.exit_code == 0
    assert "alpha" in result.output
    assert "rate_per_year" in result.output
    assert "credible_interval" in result.output


def test_eval_class_with_explicit_window(store_path: Path) -> None:
    register(_make_class())
    runner = CliRunner()
    result = runner.invoke(
        pattern_library,
        [
            "eval",
            "alpha",
            "--window-start",
            "2018-01-01T00:00:00+00:00",
            "--window-end",
            "2024-01-01T00:00:00+00:00",
            "--db",
            str(store_path),
        ],
    )
    assert result.exit_code == 0
    assert "2018-01-01" in result.output
    assert "2024-01-01" in result.output


def test_eval_class_with_partial_window_rejected(store_path: Path) -> None:
    register(_make_class())
    runner = CliRunner()
    result = runner.invoke(
        pattern_library,
        [
            "eval",
            "alpha",
            "--window-start",
            "2018-01-01T00:00:00+00:00",
            "--db",
            str(store_path),
        ],
    )
    assert result.exit_code == 2
    output = result.output + (result.stderr if result.stderr_bytes else "")
    assert "must be provided together" in output


def test_eval_unknown_class_returns_error(store_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        pattern_library,
        ["eval", "not_registered", "--db", str(store_path)],
    )
    assert result.exit_code == 1


def test_eval_with_low_sample_class_shows_warning(store_path: Path) -> None:
    register(_make_class(n_occurrences=2))
    runner = CliRunner()
    result = runner.invoke(
        pattern_library,
        ["eval", "alpha", "--db", str(store_path)],
    )
    assert result.exit_code == 0
    assert "low_sample" in result.output


# -- group help has the new commands -------------------------------------


def test_group_help_lists_refresh_and_eval() -> None:
    runner = CliRunner()
    result = runner.invoke(pattern_library, ["--help"])
    assert result.exit_code == 0
    assert "refresh" in result.output
    assert "eval" in result.output
