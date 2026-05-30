"""T-KSI-060 — Kalshi CLI subcommand acceptance tests.

Focuses on:
- Subcommand list exposure (`kalshi --help`).
- Watched-markets management against a YAML fixture.
- Sector mapping CLI (`map`, `needs-review`, `mapping-stats`).
- Status printing.
- Gate-bypass invariant: when the eligibility gate fails, no
  network-touching command reaches API code paths.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.kalshi_connector.cli import kalshi
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)
from razor_rooster.kalshi_connector.persistence.source import (
    register_kalshi_sources,
)

# -- fixtures -------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def kalshi_yaml(tmp_path: Path) -> Path:
    """A minimal valid kalshi.yaml file."""
    path = tmp_path / "kalshi.yaml"
    payload: dict[str, Any] = {
        "version": 1,
        "base_url": "https://external-api.kalshi.com/trade-api/v2",
        "tier": "Basic",
        "sync": {
            "prices": {
                "default_cadence": "every_30min",
                "minimum_interval_seconds": 60,
                "watched_markets": [],
            }
        },
        "tos_url": "https://kalshi.com/docs/kalshi-terms-of-service",
    }
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "kalshi_cli.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_kalshi_migrations(conn)
        register_kalshi_sources(conn)
    yield s
    s.close()


# -- subcommand list ------------------------------------------------------


def test_help_lists_all_subcommands(runner: CliRunner) -> None:
    result = runner.invoke(kalshi, ["--help"])
    assert result.exit_code == 0
    expected_subcommands = [
        "ack-tos",
        "backfill-settlements",
        "fetch-orderbook",
        "list-watched",
        "map",
        "mapping-stats",
        "needs-review",
        "snapshot-prices",
        "status",
        "sync",
        "unwatch",
        "version",
        "watch",
    ]
    for name in expected_subcommands:
        assert name in result.output, f"missing subcommand {name!r}"


def test_version_subcommand(runner: CliRunner) -> None:
    result = runner.invoke(kalshi, ["version"])
    assert result.exit_code == 0
    assert "8001" in result.output


# -- watched-markets management ------------------------------------------


def test_watch_adds_ticker_to_yaml(runner: CliRunner, kalshi_yaml: Path) -> None:
    result = runner.invoke(kalshi, ["watch", "KX-CPI", "--config", str(kalshi_yaml)])
    assert result.exit_code == 0
    assert "watching: KX-CPI" in result.output

    data = yaml.safe_load(kalshi_yaml.read_text(encoding="utf-8"))
    assert data["sync"]["prices"]["watched_markets"] == ["KX-CPI"]


def test_watch_dedupes(runner: CliRunner, kalshi_yaml: Path) -> None:
    """Watching an already-listed ticker is a no-op announcement."""
    runner.invoke(kalshi, ["watch", "KX-CPI", "--config", str(kalshi_yaml)])
    result = runner.invoke(kalshi, ["watch", "KX-CPI", "--config", str(kalshi_yaml)])
    assert result.exit_code == 0
    assert "already watched: KX-CPI" in result.output

    data = yaml.safe_load(kalshi_yaml.read_text(encoding="utf-8"))
    assert data["sync"]["prices"]["watched_markets"] == ["KX-CPI"]


def test_unwatch_removes_ticker(runner: CliRunner, kalshi_yaml: Path) -> None:
    runner.invoke(kalshi, ["watch", "KX-CPI", "--config", str(kalshi_yaml)])
    result = runner.invoke(kalshi, ["unwatch", "KX-CPI", "--config", str(kalshi_yaml)])
    assert result.exit_code == 0
    assert "unwatched: KX-CPI" in result.output

    data = yaml.safe_load(kalshi_yaml.read_text(encoding="utf-8"))
    assert data["sync"]["prices"]["watched_markets"] == []


def test_unwatch_unknown_ticker_errors(runner: CliRunner, kalshi_yaml: Path) -> None:
    result = runner.invoke(kalshi, ["unwatch", "KX-UNKNOWN", "--config", str(kalshi_yaml)])
    assert result.exit_code != 0
    assert "not in watched_markets" in result.output


def test_list_watched_empty(runner: CliRunner, kalshi_yaml: Path) -> None:
    result = runner.invoke(kalshi, ["list-watched", "--config", str(kalshi_yaml)])
    assert result.exit_code == 0
    assert "(no watched markets)" in result.output


def test_list_watched_after_add(runner: CliRunner, kalshi_yaml: Path) -> None:
    runner.invoke(kalshi, ["watch", "KX-A", "--config", str(kalshi_yaml)])
    runner.invoke(kalshi, ["watch", "KX-B", "--config", str(kalshi_yaml)])
    result = runner.invoke(kalshi, ["list-watched", "--config", str(kalshi_yaml)])
    assert result.exit_code == 0
    assert "KX-A" in result.output
    assert "KX-B" in result.output


def test_watch_missing_config_errors(runner: CliRunner, tmp_path: Path) -> None:
    """Missing config file → exit 3 with actionable error."""
    nonexistent = tmp_path / "missing.yaml"
    result = runner.invoke(kalshi, ["watch", "KX-CPI", "--config", str(nonexistent)])
    assert result.exit_code != 0
    assert "not found" in result.output


# -- sector mapping CLI -------------------------------------------------


def test_map_writes_manual_override(runner: CliRunner, store: DuckDBStore) -> None:
    db_path = store.path
    result = runner.invoke(
        kalshi,
        ["map", "KX-CPI", "macroeconomic", "--db", str(db_path)],
    )
    assert result.exit_code == 0, result.output
    assert "mapped: KX-CPI" in result.output
    with store.connection() as conn:
        row = conn.execute(
            "SELECT razor_sector, confidence FROM kalshi_sector_mapping WHERE ticker = 'KX-CPI'"
        ).fetchone()
    assert row is not None
    assert row[0] == "macroeconomic"
    assert row[1] == "manual"


def test_map_accepts_out_of_scope(runner: CliRunner, store: DuckDBStore) -> None:
    db_path = store.path
    result = runner.invoke(
        kalshi,
        ["map", "KX-NFL", "out_of_scope", "--db", str(db_path)],
    )
    assert result.exit_code == 0
    with store.connection() as conn:
        row = conn.execute(
            "SELECT razor_sector FROM kalshi_sector_mapping WHERE ticker = 'KX-NFL'"
        ).fetchone()
    assert row is not None
    assert row[0] == "out_of_scope"


def test_map_accepts_none_for_explicit_null(runner: CliRunner, store: DuckDBStore) -> None:
    db_path = store.path
    result = runner.invoke(kalshi, ["map", "KX-NULL", "none", "--db", str(db_path)])
    assert result.exit_code == 0
    with store.connection() as conn:
        row = conn.execute(
            "SELECT razor_sector, confidence FROM kalshi_sector_mapping WHERE ticker = 'KX-NULL'"
        ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] == "manual"


def test_map_rejects_unknown_sector(runner: CliRunner, store: DuckDBStore) -> None:
    db_path = store.path
    result = runner.invoke(kalshi, ["map", "KX-FOO", "weather", "--db", str(db_path)])
    assert result.exit_code != 0
    assert "unknown razor_sector" in result.output


def test_needs_review_empty(runner: CliRunner, store: DuckDBStore) -> None:
    db_path = store.path
    result = runner.invoke(kalshi, ["needs-review", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "(no Kalshi tickers awaiting review)" in result.output


def test_mapping_stats_runs_on_empty(runner: CliRunner, store: DuckDBStore) -> None:
    db_path = store.path
    result = runner.invoke(kalshi, ["mapping-stats", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "By sector:" in result.output
    assert "By confidence:" in result.output


# -- status -------------------------------------------------------------


def test_status_prints_kalshi_source_state(runner: CliRunner, store: DuckDBStore) -> None:
    db_path = store.path
    result = runner.invoke(kalshi, ["status", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "Kalshi source state:" in result.output
    assert "kalshi" in result.output
    assert "Cutoff snapshot:" in result.output


# -- gate-bypass invariant --------------------------------------------


def test_sync_refuses_when_eligibility_unconfigured(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    store: DuckDBStore,
    kalshi_yaml: Path,
) -> None:
    """When the eligibility gate refuses, sync exits non-zero without
    making any API calls (proven by the absence of an httpx client
    construction error). The gate runs before client construction.
    """
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    db_path = store.path
    result = runner.invoke(
        kalshi,
        [
            "sync",
            "--db",
            str(db_path),
            "--config",
            str(kalshi_yaml),
        ],
    )
    assert result.exit_code != 0
    assert "kalshi eligibility gate refused" in result.output


def test_snapshot_prices_refuses_when_eligibility_unconfigured(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    store: DuckDBStore,
    kalshi_yaml: Path,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    db_path = store.path
    result = runner.invoke(
        kalshi,
        [
            "snapshot-prices",
            "--db",
            str(db_path),
            "--config",
            str(kalshi_yaml),
        ],
    )
    assert result.exit_code != 0
    assert "kalshi eligibility gate refused" in result.output


def test_backfill_settlements_refuses_when_eligibility_unconfigured(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    store: DuckDBStore,
    kalshi_yaml: Path,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    db_path = store.path
    result = runner.invoke(
        kalshi,
        [
            "backfill-settlements",
            "--db",
            str(db_path),
            "--config",
            str(kalshi_yaml),
        ],
    )
    assert result.exit_code != 0
    assert "kalshi eligibility gate refused" in result.output


def test_fetch_orderbook_refuses_when_eligibility_unconfigured(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    store: DuckDBStore,
    kalshi_yaml: Path,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    db_path = store.path
    result = runner.invoke(
        kalshi,
        [
            "fetch-orderbook",
            "KX-CPI",
            "--db",
            str(db_path),
            "--config",
            str(kalshi_yaml),
        ],
    )
    assert result.exit_code != 0
    assert "kalshi eligibility gate refused" in result.output
