"""Tests for the threshold-measurements prune helper + CLI (T-RG-COMPAT-PRUNE-001).

Covers:
- prune_threshold_measurements: confirm guard, before-cutoff strategy,
  keep-last strategy, combined strategies, kind scoping, edge cases.
- CLI: razor-rooster report prune-measurements with --before / --keep-last /
  --confirm / --kind flags.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.report_generator.cli import report as report_cli
from razor_rooster.report_generator.engines.measurements import (
    compute_distribution,
)
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import (
    PruneConfirmationError,
    list_threshold_measurements,
    persist_threshold_measurement,
    prune_threshold_measurements,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "prune.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_report_generator_migrations(c)
    with store.connection() as c:
        yield c


def _seed(
    conn: duckdb.DuckDBPyConnection,
    *,
    report_id: str,
    measurement_kind: str,
    measured_at: datetime,
) -> None:
    persist_threshold_measurement(
        conn,
        report_id=report_id,
        measurement_kind=measurement_kind,
        measured_at=measured_at,
        distribution=compute_distribution([100.0, 200.0, 300.0], threshold=200.0),
    )


# -- guard rails -----------------------------------------------------------


def test_prune_refuses_without_confirm(conn: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(PruneConfirmationError):
        prune_threshold_measurements(conn, before=datetime(2026, 5, 14, tzinfo=UTC))


def test_prune_requires_at_least_one_strategy(conn: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ValueError, match="at least one"):
        prune_threshold_measurements(conn, confirm=True)


def test_prune_rejects_negative_keep_last(conn: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(ValueError, match="keep_last must be >= 0"):
        prune_threshold_measurements(conn, keep_last=-1, confirm=True)


# -- before-cutoff strategy ------------------------------------------------


def test_prune_before_deletes_older_rows(conn: duckdb.DuckDBPyConnection) -> None:
    base = datetime(2026, 5, 15, tzinfo=UTC)
    _seed(
        conn,
        report_id="r-old",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base - timedelta(days=10),
    )
    _seed(
        conn,
        report_id="r-mid",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base - timedelta(days=5),
    )
    _seed(
        conn,
        report_id="r-new",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base - timedelta(days=1),
    )
    # Cutoff 7 days before base; only r-old qualifies.
    deleted = prune_threshold_measurements(
        conn,
        before=base - timedelta(days=7),
        confirm=True,
    )
    assert deleted == 1
    surviving = list_threshold_measurements(conn)
    assert {r.report_id for r in surviving} == {"r-mid", "r-new"}


def test_prune_before_with_kind_scope(conn: duckdb.DuckDBPyConnection) -> None:
    base = datetime(2026, 5, 15, tzinfo=UTC)
    _seed(
        conn,
        report_id="r-cv",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base - timedelta(days=10),
    )
    _seed(
        conn,
        report_id="r-dom",
        measurement_kind="single_venue_dominance_share",
        measured_at=base - timedelta(days=10),
    )
    # Prune only cross_venue_spread_bps older than 5d; dominance row survives.
    deleted = prune_threshold_measurements(
        conn,
        before=base - timedelta(days=5),
        measurement_kind="cross_venue_spread_bps",
        confirm=True,
    )
    assert deleted == 1
    surviving = list_threshold_measurements(conn)
    assert {r.measurement_kind for r in surviving} == {"single_venue_dominance_share"}


# -- keep-last strategy ----------------------------------------------------


def test_prune_keep_last_per_kind(conn: duckdb.DuckDBPyConnection) -> None:
    base = datetime(2026, 5, 15, tzinfo=UTC)
    # 5 rows for cv, 3 for dom.
    for i in range(5):
        _seed(
            conn,
            report_id=f"cv-{i}",
            measurement_kind="cross_venue_spread_bps",
            measured_at=base + timedelta(hours=i),
        )
    for i in range(3):
        _seed(
            conn,
            report_id=f"dom-{i}",
            measurement_kind="single_venue_dominance_share",
            measured_at=base + timedelta(hours=i),
        )
    # Keep newest 2 per kind; cv loses 3, dom loses 1.
    deleted = prune_threshold_measurements(conn, keep_last=2, confirm=True)
    assert deleted == 4
    surviving = list_threshold_measurements(conn)
    cv_survivors = [
        r.report_id for r in surviving if r.measurement_kind == "cross_venue_spread_bps"
    ]
    dom_survivors = [
        r.report_id for r in surviving if r.measurement_kind == "single_venue_dominance_share"
    ]
    # The 2 newest cv rows are cv-4 and cv-3.
    assert set(cv_survivors) == {"cv-4", "cv-3"}
    # The 2 newest dom rows are dom-2 and dom-1.
    assert set(dom_survivors) == {"dom-2", "dom-1"}


def test_prune_keep_last_zero_deletes_all_rows_for_kind(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    base = datetime(2026, 5, 15, tzinfo=UTC)
    for i in range(3):
        _seed(
            conn,
            report_id=f"r{i}",
            measurement_kind="cross_venue_spread_bps",
            measured_at=base + timedelta(hours=i),
        )
    deleted = prune_threshold_measurements(
        conn,
        keep_last=0,
        measurement_kind="cross_venue_spread_bps",
        confirm=True,
    )
    assert deleted == 3
    assert list_threshold_measurements(conn) == ()


def test_prune_keep_last_more_than_available_is_noop(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """keep_last=10 with only 3 rows deletes nothing."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    for i in range(3):
        _seed(
            conn,
            report_id=f"r{i}",
            measurement_kind="cross_venue_spread_bps",
            measured_at=base + timedelta(hours=i),
        )
    deleted = prune_threshold_measurements(conn, keep_last=10, confirm=True)
    assert deleted == 0
    assert len(list_threshold_measurements(conn)) == 3


# -- combined strategies ---------------------------------------------------


def test_prune_before_and_keep_last_stack(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Both flags work together; rows die under either condition."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    # 5 rows: r0 oldest, r4 newest.
    for i in range(5):
        _seed(
            conn,
            report_id=f"r{i}",
            measurement_kind="cross_venue_spread_bps",
            measured_at=base + timedelta(hours=i),
        )
    # before: kill anything older than r2 (so r0, r1).
    # keep_last: keep newest 2 (so r3, r4 survive).
    # Combined: r0, r1, r2 die.
    deleted = prune_threshold_measurements(
        conn,
        before=base + timedelta(hours=2),
        keep_last=2,
        confirm=True,
    )
    assert deleted == 3
    survivors = [r.report_id for r in list_threshold_measurements(conn)]
    assert set(survivors) == {"r3", "r4"}


# -- empty table ------------------------------------------------------------


def test_prune_on_empty_table(conn: duckdb.DuckDBPyConnection) -> None:
    deleted = prune_threshold_measurements(
        conn,
        before=datetime(2026, 5, 15, tzinfo=UTC),
        confirm=True,
    )
    assert deleted == 0


# -- CLI -------------------------------------------------------------------


def test_cli_refuses_without_strategy(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_no_strat.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["prune-measurements", "--db", str(db_path), "--confirm"])
    assert result.exit_code != 0
    assert "refusing to prune without --before or --keep-last" in (
        result.output + (result.stderr or "")
    )


def test_cli_refuses_without_confirm(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_no_confirm.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "prune-measurements",
            "--db",
            str(db_path),
            "--before",
            "2026-05-14T00:00:00",
        ],
    )
    assert result.exit_code != 0
    assert "refusing to prune without --confirm" in (result.output + (result.stderr or ""))


def test_cli_prunes_before_cutoff(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_before.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            _seed(
                c,
                report_id="r-old",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 1, tzinfo=UTC),
            )
            _seed(
                c,
                report_id="r-new",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 14, tzinfo=UTC),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "prune-measurements",
            "--db",
            str(db_path),
            "--before",
            "2026-05-10T00:00:00",
            "--confirm",
        ],
    )
    assert result.exit_code == 0
    assert "pruned 1" in result.output


def test_cli_prunes_keep_last(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_keep_last.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            for i in range(5):
                _seed(
                    c,
                    report_id=f"r{i}",
                    measurement_kind="cross_venue_spread_bps",
                    measured_at=datetime(2026, 5, 15, i, tzinfo=UTC),
                )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "prune-measurements",
            "--db",
            str(db_path),
            "--keep-last",
            "2",
            "--confirm",
        ],
    )
    assert result.exit_code == 0
    assert "pruned 3" in result.output


def test_cli_prunes_with_kind_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_kind.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            _seed(
                c,
                report_id="cv-old",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 1, tzinfo=UTC),
            )
            _seed(
                c,
                report_id="dom-old",
                measurement_kind="single_venue_dominance_share",
                measured_at=datetime(2026, 5, 1, tzinfo=UTC),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "prune-measurements",
            "--db",
            str(db_path),
            "--before",
            "2026-05-10T00:00:00",
            "--kind",
            "cross_venue_spread_bps",
            "--confirm",
        ],
    )
    assert result.exit_code == 0
    assert "pruned 1" in result.output
    assert "for kind 'cross_venue_spread_bps'" in result.output


def test_cli_invalid_before_returns_error(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_bad_iso.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "prune-measurements",
            "--db",
            str(db_path),
            "--before",
            "not-a-date",
            "--confirm",
        ],
    )
    assert result.exit_code != 0
