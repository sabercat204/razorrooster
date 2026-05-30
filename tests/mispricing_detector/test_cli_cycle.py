"""T-MD-050 — cycle-running CLI tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.cli import mispricing
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.pattern_library import registry
from razor_rooster.pattern_library.models.event_class import (
    EventClass,
    Sector,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    registry._clear_for_tests()
    registry._set_discovered_for_tests(True)
    yield
    registry._clear_for_tests()


def _occurrences(_conn: object) -> pd.DataFrame:
    return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})


def _make_class(class_id: str = "cli_cls") -> EventClass:
    return EventClass(
        class_id=class_id,
        title=f"Test class {class_id}",
        description="Synthetic class for CLI cycle tests",
        domain_sector=Sector.PUBLIC_HEALTH,
        occurrence_query=_occurrences,
    )


def _seed_db(tmp_path: Path, *, with_scan: bool = True, with_market: bool = True) -> Path:
    db_path = tmp_path / "trough.duckdb"
    store = DuckDBStore(db_path)
    now = datetime(2026, 5, 15, 12, tzinfo=UTC)
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    completed = started + timedelta(minutes=2)
    snapshot_ts = datetime(2026, 5, 15, 11, tzinfo=UTC)
    with store.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        run_pending_mispricing_migrations(conn)
        if with_scan:
            conn.execute(
                "INSERT INTO scan_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["scan-001", started, completed, 1, 1, 1, 0, 0, 0, 0, None, None],
            )
            conn.execute(
                "INSERT INTO scan_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    "scan-001",
                    "cli_cls",
                    1,
                    1,
                    started,
                    started,
                    completed,
                    0.05,
                    0.025,
                    0.075,
                    0.30,
                    0.20,
                    0.40,
                    1.5,
                    False,
                    None,
                    0.8,
                    False,
                    False,
                    False,
                    False,
                    False,
                    None,
                    None,
                ],
            )
            conn.execute(
                "INSERT INTO scan_traces VALUES (?, ?, ?)",
                [
                    "scan-001",
                    "cli_cls",
                    json.dumps({"class_id": "cli_cls", "warnings": [], "precursors": []}),
                ],
            )
        if with_market:
            conn.execute(
                "INSERT INTO polymarket_markets ("
                "source_id, source_record_id, source_publication_ts, fetch_ts, "
                "connector_version, source_payload_json, superseded_at, "
                "condition_id, slug, question, description, category, subcategory, "
                "tags, event_id, market_type, outcome_tokens, end_date, active, "
                "closed, resolved, volume_lifetime, created_at_polymarket, "
                "last_updated_polymarket, removed_at"
                ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, "
                "?, ?, NULL, TRUE, FALSE, FALSE, NULL, NULL, NULL, NULL)",
                [
                    "polymarket",
                    "market-0xabc",
                    now,
                    now,
                    "test@1",
                    json.dumps({"raw": "synthetic"}),
                    "0xabc",
                    "slug",
                    "Will WHO PHEIC happen in 2026?",
                    "binary",
                    json.dumps(
                        [{"id": "tok-yes", "outcome": "Yes"}, {"id": "tok-no", "outcome": "No"}]
                    ),
                ],
            )
            conn.execute(
                "INSERT INTO polymarket_sector_mapping VALUES (?, ?, NULL, 'inferred', ?, 'auto')",
                ["0xabc", "public_health", now],
            )
            conn.execute(
                "INSERT INTO polymarket_price_snapshots ("
                "source_id, source_record_id, source_publication_ts, fetch_ts, "
                "connector_version, source_payload_json, superseded_at, "
                "condition_id, outcome_token_id, snapshot_ts, mid_price, "
                "best_bid, best_ask, last_trade_price, last_trade_ts, "
                "volume_24h, liquidity_warning, spread_bps, snapshot_source"
                ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, ?, 'test')",
                [
                    "polymarket",
                    "snap-0xabc",
                    snapshot_ts,
                    now,
                    "test@1",
                    json.dumps({"raw": "synthetic"}),
                    "0xabc",
                    "tok-yes",
                    snapshot_ts,
                    0.10,
                    0.09,
                    0.11,
                    0.10,
                    snapshot_ts,
                    20000.0,
                    200,
                ],
            )
    store.close()
    return db_path


def test_run_command_completes(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path)
    cls = _make_class()
    registry.register(cls)
    runner = CliRunner()
    # Register operator mapping so the cycle has something to evaluate.
    map_result = runner.invoke(
        mispricing,
        ["map", "cli_cls", "0xabc", "--db", str(db_path)],
    )
    assert map_result.exit_code == 0
    result = runner.invoke(mispricing, ["run", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "cycle_id" in result.output
    assert "cli_cls" in result.output


def test_run_command_no_scan_exits_1(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path, with_scan=False)
    runner = CliRunner()
    result = runner.invoke(mispricing, ["run", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "no signal_scanner" in result.output


def test_show_command(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path)
    cls = _make_class()
    registry.register(cls)
    runner = CliRunner()
    runner.invoke(mispricing, ["map", "cli_cls", "0xabc", "--db", str(db_path)])
    run_result = runner.invoke(mispricing, ["run", "--db", str(db_path)])
    assert run_result.exit_code == 0
    # Find a comparison_id in the output line.
    list_result = runner.invoke(mispricing, ["list-comparisons", "--db", str(db_path)])
    assert list_result.exit_code == 0
    # Pull the first comparison_id from the listing.
    lines = [
        line
        for line in list_result.output.splitlines()
        if line and not line.startswith(("comparison_id", "-"))
    ]
    assert lines, list_result.output
    comparison_id = lines[0].split()[0]
    show_result = runner.invoke(mispricing, ["show", comparison_id, "--db", str(db_path)])
    assert show_result.exit_code == 0, show_result.output
    assert "Possible reasons the model may be right" in show_result.output
    assert "Possible reasons the market may be right" in show_result.output


def test_show_unknown_comparison_id(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(mispricing, ["show", "no-such-id", "--db", str(db_path)])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_list_comparisons_empty(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(mispricing, ["list-comparisons", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "no comparisons" in result.output


def test_relink_command_runs(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path)
    runner = CliRunner()
    result = runner.invoke(mispricing, ["relink", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "resolutions processed" in result.output
