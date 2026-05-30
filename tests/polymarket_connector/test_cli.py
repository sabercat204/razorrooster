"""T-PMC-060 — polymarket CLI subcommand tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.polymarket_connector.cli import polymarket
from razor_rooster.polymarket_connector.client.rate_limit import (
    reset_shared_bucket,
)
from razor_rooster.polymarket_connector.gates.tos import (
    hash_tos_text,
    record_acknowledgement,
)
from razor_rooster.polymarket_connector.mapping.sector_overrides import (
    get_mapping,
    set_override,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.source import (
    register_polymarket_sources,
)


@pytest.fixture(autouse=True)
def _isolated_bucket() -> Iterator[None]:
    reset_shared_bucket()
    yield
    reset_shared_bucket()


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "polymarket_cli.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        register_polymarket_sources(conn)
    s.close()
    return db_path


@pytest.fixture
def restricted_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "restricted.yaml"
    p.write_text("version: 1\nrestricted:\n  - US\n", encoding="utf-8")
    return p


@pytest.fixture
def polymarket_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "polymarket.yaml"
    p.write_text(
        "version: 1\n"
        "sync:\n"
        "  prices:\n"
        "    watched_markets: []\n"
        "sector_mapping:\n"
        "  heuristic_version: 1\n"
        '  keywords_file: "config/sector_keywords.yaml"\n',
        encoding="utf-8",
    )
    return p


@pytest.fixture
def permitted_jurisdiction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPERATOR_JURISDICTION", "DE")


def _seed_tos_ack(store_path: Path, hash_value: str) -> None:
    s = DuckDBStore(store_path)
    try:
        with s.connection() as conn:
            record_acknowledgement(conn, tos_version_hash=hash_value)
    finally:
        s.close()


def _seed_tos_history(
    store_path: Path, hash_value: str, *, url: str = "https://polymarket.com/tos"
) -> None:
    """Seed polymarket_tos_version_history so the gate's fallback path passes."""
    s = DuckDBStore(store_path)
    try:
        when = datetime.now(tz=UTC)
        with s.connection() as conn:
            conn.execute(
                "INSERT INTO polymarket_tos_version_history "
                "(tos_version_hash, tos_url, first_seen_at, last_seen_at, notes) "
                "VALUES (?, ?, ?, ?, NULL)",
                [hash_value, url, when, when],
            )
    finally:
        s.close()


# -------------------------------------------------------------------------
# group + status (no gates)
# -------------------------------------------------------------------------
def test_polymarket_group_help_lists_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(polymarket, ["--help"])
    assert result.exit_code == 0
    for cmd in (
        "ack-tos",
        "status",
        "sync",
        "snapshot",
        "backfill-resolutions",
        "watch",
        "unwatch",
        "list-watched",
        "fetch-orderbook",
        "map",
        "needs-review",
        "mapping-stats",
    ):
        assert cmd in result.output


def test_status_with_no_db_returns_error(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(polymarket, ["status", "--db", str(tmp_path / "nope.duckdb")])
    assert result.exit_code == 1
    assert "DuckDB store not found" in (
        result.output + (result.stderr if result.stderr_bytes else "")
    )


def test_status_empty_database_lists_no_polymarket_sources(tmp_path: Path) -> None:
    """A fresh DuckDB without polymarket migrations applied → no rows."""
    db_path = tmp_path / "empty.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
    s.close()

    runner = CliRunner()
    result = runner.invoke(polymarket, ["status", "--db", str(db_path)])
    assert result.exit_code == 0
    # The CLI applies polymarket migrations + registers sources lazily, so
    # the polymarket rows ARE present after the first invocation.
    assert "polymarket" in result.output


def test_status_after_ack_shows_hash(store_path: Path) -> None:
    expected_hash = hash_tos_text("Polymarket Terms of Service v1\n")
    _seed_tos_ack(store_path, expected_hash)
    runner = CliRunner()
    result = runner.invoke(polymarket, ["status", "--db", str(store_path)])
    assert result.exit_code == 0
    assert expected_hash[:12] in result.output


# -------------------------------------------------------------------------
# ack-tos
# -------------------------------------------------------------------------
def test_ack_tos_geo_refusal(monkeypatch: pytest.MonkeyPatch, store_path: Path) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    runner = CliRunner()
    result = runner.invoke(polymarket, ["ack-tos", "--db", str(store_path)])
    assert result.exit_code == 2
    assert "geo gate refused" in (result.output + (result.stderr if result.stderr_bytes else ""))


def test_ack_tos_with_yes_flag_records(
    monkeypatch: pytest.MonkeyPatch,
    store_path: Path,
    permitted_jurisdiction: None,
) -> None:
    """With --yes and a mocked fetch_current_tos_hash, the ack lands."""

    def fake_fetch(*, url: str = "", timeout_seconds: float = 10.0, client: Any = None) -> str:
        del url, timeout_seconds, client
        return "deadbeef" * 8  # synthetic 64-char hash

    monkeypatch.setattr(
        "razor_rooster.polymarket_connector.cli.fetch_current_tos_hash",
        fake_fetch,
    )
    runner = CliRunner()
    result = runner.invoke(polymarket, ["ack-tos", "--db", str(store_path), "--yes"])
    assert result.exit_code == 0
    assert "Recorded acknowledgement" in result.output

    # Verify it landed in the sources table.
    s = DuckDBStore(store_path)
    try:
        with s.connection() as conn:
            row = conn.execute(
                "SELECT license_terms_hash FROM sources WHERE source_id = 'polymarket'"
            ).fetchone()
    finally:
        s.close()
    assert row is not None
    assert row[0] == "deadbeef" * 8


def test_ack_tos_missing_db_errors(
    monkeypatch: pytest.MonkeyPatch,
    permitted_jurisdiction: None,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "razor_rooster.polymarket_connector.cli.fetch_current_tos_hash",
        lambda **_kw: "x" * 64,
    )
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        ["ack-tos", "--db", str(tmp_path / "nope.duckdb"), "--yes"],
    )
    assert result.exit_code == 1


# -------------------------------------------------------------------------
# watched-markets management (no gates needed)
# -------------------------------------------------------------------------
def test_watch_adds_market(polymarket_yaml: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        ["watch", "0xabc", "--config", str(polymarket_yaml)],
    )
    assert result.exit_code == 0
    assert "Added" in result.output

    import yaml

    data = yaml.safe_load(polymarket_yaml.read_text())
    assert data["sync"]["prices"]["watched_markets"] == ["0xabc"]


def test_watch_idempotent_for_known_market(polymarket_yaml: Path) -> None:
    runner = CliRunner()
    runner.invoke(polymarket, ["watch", "0xabc", "--config", str(polymarket_yaml)])
    result = runner.invoke(
        polymarket,
        ["watch", "0xabc", "--config", str(polymarket_yaml)],
    )
    assert result.exit_code == 0
    assert "already on the watch list" in result.output


def test_unwatch_removes_market(polymarket_yaml: Path) -> None:
    runner = CliRunner()
    runner.invoke(polymarket, ["watch", "0xabc", "--config", str(polymarket_yaml)])
    result = runner.invoke(
        polymarket,
        ["unwatch", "0xabc", "--config", str(polymarket_yaml)],
    )
    assert result.exit_code == 0
    assert "Removed" in result.output

    import yaml

    data = yaml.safe_load(polymarket_yaml.read_text())
    assert data["sync"]["prices"]["watched_markets"] == []


def test_unwatch_unknown_market_no_op(polymarket_yaml: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        ["unwatch", "0xnotwatched", "--config", str(polymarket_yaml)],
    )
    assert result.exit_code == 0
    assert "not on the watch list" in result.output


def test_list_watched_empty(polymarket_yaml: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        ["list-watched", "--config", str(polymarket_yaml)],
    )
    assert result.exit_code == 0
    assert "no watched markets" in result.output


def test_list_watched_after_adds(polymarket_yaml: Path) -> None:
    runner = CliRunner()
    runner.invoke(polymarket, ["watch", "0xabc", "--config", str(polymarket_yaml)])
    runner.invoke(polymarket, ["watch", "0xdef", "--config", str(polymarket_yaml)])
    result = runner.invoke(
        polymarket,
        ["list-watched", "--config", str(polymarket_yaml)],
    )
    assert result.exit_code == 0
    assert "0xabc" in result.output
    assert "0xdef" in result.output


# -------------------------------------------------------------------------
# map / needs-review / mapping-stats (no gates)
# -------------------------------------------------------------------------
def test_map_records_manual_override(store_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        ["map", "0xabc", "regulatory", "--db", str(store_path)],
    )
    assert result.exit_code == 0
    assert "manual override" in result.output

    s = DuckDBStore(store_path)
    try:
        with s.connection() as conn:
            row = get_mapping(conn, "0xabc")
    finally:
        s.close()
    assert row is not None
    assert row.razor_sector == "regulatory"
    assert row.confidence == "manual"


def test_map_with_secondary_persisted(store_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        [
            "map",
            "0xabc",
            "geopolitical",
            "--secondary",
            "regulatory",
            "--secondary",
            "commodity",
            "--db",
            str(store_path),
        ],
    )
    assert result.exit_code == 0

    s = DuckDBStore(store_path)
    try:
        with s.connection() as conn:
            row = get_mapping(conn, "0xabc")
    finally:
        s.close()
    assert row is not None
    assert row.secondary_sectors == ("regulatory", "commodity")


def test_map_none_records_explicit_no_sector(store_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        ["map", "0xabc", "none", "--db", str(store_path)],
    )
    assert result.exit_code == 0
    s = DuckDBStore(store_path)
    try:
        with s.connection() as conn:
            row = get_mapping(conn, "0xabc")
    finally:
        s.close()
    assert row is not None
    assert row.razor_sector is None
    assert row.confidence == "manual"


def test_needs_review_empty(store_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        ["needs-review", "--db", str(store_path)],
    )
    assert result.exit_code == 0
    assert "no markets pending review" in result.output


def test_needs_review_lists_inferred_nulls(store_path: Path) -> None:
    """An inferred-null mapping should appear in needs-review."""
    from razor_rooster.polymarket_connector.mapping.sector_heuristic import (
        SectorMapping,
    )
    from razor_rooster.polymarket_connector.mapping.sector_overrides import (
        upsert_inferred_mapping,
    )

    s = DuckDBStore(store_path)
    try:
        with s.connection() as conn:
            upsert_inferred_mapping(
                conn,
                condition_id="0xneedsreview",
                mapping=SectorMapping(
                    razor_sector=None,
                    secondary_sectors=(),
                    confidence="inferred",
                    scores={},
                ),
            )
    finally:
        s.close()

    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        ["needs-review", "--db", str(store_path)],
    )
    assert result.exit_code == 0
    assert "0xneedsreview" in result.output


def test_mapping_stats_empty(store_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        ["mapping-stats", "--db", str(store_path)],
    )
    assert result.exit_code == 0
    assert "By sector:" in result.output
    assert "By confidence:" in result.output


def test_mapping_stats_with_data(store_path: Path) -> None:
    s = DuckDBStore(store_path)
    try:
        with s.connection() as conn:
            set_override(conn, condition_id="0x1", razor_sector="climate")
            set_override(conn, condition_id="0x2", razor_sector="climate")
            set_override(conn, condition_id="0x3", razor_sector="commodity")
    finally:
        s.close()

    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        ["mapping-stats", "--db", str(store_path)],
    )
    assert result.exit_code == 0
    assert "climate" in result.output
    assert "commodity" in result.output
    assert "manual" in result.output


# -------------------------------------------------------------------------
# Gate refusals on gated subcommands
# -------------------------------------------------------------------------
def test_sync_geo_refusal(
    monkeypatch: pytest.MonkeyPatch,
    store_path: Path,
    polymarket_yaml: Path,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        [
            "sync",
            "--db",
            str(store_path),
            "--config",
            str(polymarket_yaml),
        ],
    )
    assert result.exit_code == 2
    assert "geo gate refused" in (result.output + (result.stderr if result.stderr_bytes else ""))


def test_sync_tos_refusal(
    monkeypatch: pytest.MonkeyPatch,
    store_path: Path,
    polymarket_yaml: Path,
    permitted_jurisdiction: None,
) -> None:
    """No prior ack → ToS gate refuses."""

    def fake_fetch(*, url: str = "", timeout_seconds: float = 10.0, client: Any = None) -> str:
        del url, timeout_seconds, client
        return "abc" * 22

    monkeypatch.setattr(
        "razor_rooster.polymarket_connector.gates.tos.fetch_current_tos_hash",
        fake_fetch,
    )
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        [
            "sync",
            "--db",
            str(store_path),
            "--config",
            str(polymarket_yaml),
        ],
    )
    assert result.exit_code == 3
    assert "ToS gate refused" in (result.output + (result.stderr if result.stderr_bytes else ""))


def test_snapshot_geo_refusal(
    monkeypatch: pytest.MonkeyPatch,
    store_path: Path,
    polymarket_yaml: Path,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        [
            "snapshot",
            "--db",
            str(store_path),
            "--config",
            str(polymarket_yaml),
        ],
    )
    assert result.exit_code == 2


def test_backfill_resolutions_geo_refusal(
    monkeypatch: pytest.MonkeyPatch,
    store_path: Path,
    polymarket_yaml: Path,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        [
            "backfill-resolutions",
            "--db",
            str(store_path),
            "--config",
            str(polymarket_yaml),
        ],
    )
    assert result.exit_code == 2


def test_fetch_orderbook_geo_refusal(
    monkeypatch: pytest.MonkeyPatch,
    store_path: Path,
    polymarket_yaml: Path,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        polymarket,
        [
            "fetch-orderbook",
            "0xabc",
            "--token-id",
            "0xabc-yes",
            "--db",
            str(store_path),
            "--config",
            str(polymarket_yaml),
        ],
    )
    assert result.exit_code == 2
