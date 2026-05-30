"""T-KSI-061 — cross-venue comparator wiring acceptance tests.

Verifies that:
- The comparator's market reader branches on ``mapping.venue``.
- A class mapped against both Polymarket and Kalshi produces two
  distinct comparison rows (one per venue) when the comparator runs.
- Existing Polymarket mappings keep working (default `venue='polymarket'`).
- ``register_operator_mapping`` accepts a ``venue`` kwarg.
- ``mispricing map --venue kalshi`` refuses non-binary Kalshi tickers.
- ``mispricing map --venue kalshi`` refuses unknown tickers.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)
from razor_rooster.mispricing_detector.cli import mispricing
from razor_rooster.mispricing_detector.engines.comparator import (
    _read_market_context,
    compute_comparison,
)
from razor_rooster.mispricing_detector.mapping.operator_overrides import (
    register_operator_mapping,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
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


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "ksi_061.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        run_pending_mispricing_migrations(conn)
        run_pending_kalshi_migrations(conn)
    yield s
    s.close()


# -- helpers ----------------------------------------------------------


def _seed_polymarket_market(
    store: DuckDBStore,
    *,
    condition_id: str = "0xabc",
    yes_token: str = "tok-yes",
    snapshot_ts: datetime | None = None,
) -> None:
    """Seed a polymarket market with one YES-token price snapshot."""
    snapshot_ts = snapshot_ts or datetime(2026, 5, 16, 12, tzinfo=UTC)
    outcome_tokens = json.dumps(
        [
            {"id": yes_token, "outcome": "yes"},
            {"id": "tok-no", "outcome": "no"},
        ]
    )
    fetch_ts = snapshot_ts
    with store.connection() as conn:
        # Markets row.
        conn.execute(
            "INSERT INTO polymarket_markets ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "condition_id, slug, question, market_type, outcome_tokens, "
            "active, closed, resolved"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "polymarket",
                condition_id,
                fetch_ts,
                fetch_ts,
                "polymarket@0.1.0",
                "{}",
                condition_id,
                "test-slug",
                "Test question?",
                "binary",
                outcome_tokens,
                True,
                False,
                False,
            ],
        )
        # Price snapshot row.
        conn.execute(
            "INSERT INTO polymarket_price_snapshots ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "condition_id, outcome_token_id, snapshot_ts, mid_price, "
            "best_bid, best_ask, last_trade_price, last_trade_ts, "
            "volume_24h, liquidity_warning, spread_bps, snapshot_source"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "polymarket",
                f"{condition_id}:{yes_token}:{snapshot_ts.isoformat()}",
                snapshot_ts,
                fetch_ts,
                "polymarket@0.1.0",
                "{}",
                condition_id,
                yes_token,
                snapshot_ts,
                0.42,
                0.41,
                0.43,
                0.42,
                snapshot_ts,
                10_000.0,
                False,
                50,
                "rest",
            ],
        )


def _seed_kalshi_market(
    store: DuckDBStore,
    *,
    ticker: str = "KX-ABC",
    market_type: str = "binary",
    snapshot_ts: datetime | None = None,
    yes_bid: float | None = 0.30,
    yes_ask: float | None = 0.32,
) -> None:
    """Seed a kalshi market + price snapshot."""
    snapshot_ts = snapshot_ts or datetime(2026, 5, 16, 12, tzinfo=UTC)
    fetch_ts = snapshot_ts
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO kalshi_markets ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "ticker, event_ticker, series_ticker, title, market_type, status"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
            [
                "kalshi",
                ticker,
                fetch_ts,
                fetch_ts,
                "kalshi@0.1.0",
                "{}",
                ticker,
                "EVT-1",
                "INX",
                "Test market",
                market_type,
                "open",
            ],
        )
        if market_type == "binary":
            mid_price = None
            spread_bps = None
            if yes_bid is not None and yes_ask is not None:
                mid_price = (yes_bid + yes_ask) / 2.0
                if mid_price > 0:
                    spread_bps = round((yes_ask - yes_bid) / mid_price * 10_000)
            conn.execute(
                "INSERT INTO kalshi_price_snapshots ("
                "source_id, source_record_id, source_publication_ts, fetch_ts, "
                "connector_version, source_payload_json, superseded_at, "
                "ticker, snapshot_ts, yes_bid_dollars, yes_ask_dollars, "
                "mid_price_dollars, last_trade_price_dollars, last_trade_ts, "
                "volume_24h, volume_total, open_interest, liquidity, "
                "liquidity_warning, spread_bps, snapshot_source"
                ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    "kalshi",
                    f"{ticker}:{snapshot_ts.isoformat()}",
                    snapshot_ts,
                    fetch_ts,
                    "kalshi@0.1.0",
                    "{}",
                    ticker,
                    snapshot_ts,
                    yes_bid,
                    yes_ask,
                    mid_price,
                    yes_bid,
                    snapshot_ts,
                    5_000.0,
                    5_000.0,
                    None,
                    None,
                    False,
                    spread_bps,
                    "rest",
                ],
            )


def _seed_scan_record(store: DuckDBStore, *, class_id: str, scan_id: str) -> None:
    """Seed a signal_scanner scan record + summary the comparator can read."""
    started = datetime(2026, 5, 16, 11, tzinfo=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO scan_summaries ("
            "scan_id, scan_started_at, scan_completed_at, "
            "pattern_library_version, classes_total, classes_succeeded, "
            "classes_failed, classes_skipped, candidates_count, "
            "library_stale_warning"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [scan_id, started, started, 1, 1, 1, 0, 0, 1, False],
        )
        conn.execute(
            "INSERT INTO scan_records ("
            "scan_id, class_id, class_definition_version, "
            "pattern_library_version, data_as_of, scan_started_at, "
            "scan_completed_at, base_rate, base_rate_ci_lower, "
            "base_rate_ci_upper, posterior, posterior_ci_lower, "
            "posterior_ci_upper, log_odds_shift, is_candidate, "
            "candidate_direction, signature_confidence, "
            "low_signature_confidence, source_stale_warning, "
            "library_stale_warning, definition_drift_warning, "
            "no_update_applied, no_update_reason, error"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                scan_id,
                class_id,
                1,
                1,
                started,
                started,
                started,
                0.20,
                0.10,
                0.30,
                0.65,
                0.55,
                0.75,
                1.5,
                True,
                "above",
                0.85,
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
            "INSERT INTO scan_traces (scan_id, class_id, trace_json) VALUES (?, ?, ?)",
            [scan_id, class_id, json.dumps({"precursors": []})],
        )


# -- comparator branching tests --------------------------------------


def test_market_context_polymarket_branch(store: DuckDBStore) -> None:
    _seed_polymarket_market(store, condition_id="0xabc")
    ctx = _read_market_context(store, condition_id="0xabc", polarity="aligned", venue="polymarket")
    assert ctx.is_binary is True
    assert ctx.outcome_token_id == "tok-yes"
    assert ctx.snapshot.best_bid == pytest.approx(0.41)
    assert ctx.snapshot.best_ask == pytest.approx(0.43)


def test_market_context_kalshi_branch(store: DuckDBStore) -> None:
    _seed_kalshi_market(store, ticker="KX-CPI")
    ctx = _read_market_context(store, condition_id="KX-CPI", polarity="aligned", venue="kalshi")
    assert ctx.is_binary is True
    # Kalshi: outcome_token_id mirrors the ticker (no separate token concept).
    assert ctx.outcome_token_id == "KX-CPI"
    assert ctx.snapshot.best_bid == pytest.approx(0.30)
    assert ctx.snapshot.best_ask == pytest.approx(0.32)


def test_market_context_kalshi_non_binary_passes_through(
    store: DuckDBStore,
) -> None:
    """Non-binary Kalshi markets read but ``is_binary=False``.

    The comparator's per-mapping path uses this flag to short-circuit.
    """
    _seed_kalshi_market(store, ticker="KX-SCAL", market_type="scalar")
    ctx = _read_market_context(store, condition_id="KX-SCAL", polarity="aligned", venue="kalshi")
    assert ctx.is_binary is False


# -- register_operator_mapping --------------------------------------


def test_register_operator_mapping_accepts_kalshi_venue(
    store: DuckDBStore,
) -> None:
    with store.connection() as conn:
        m = register_operator_mapping(
            conn,
            class_id="cls",
            condition_id="KX-CPI",
            venue="kalshi",
        )
    assert m.venue == "kalshi"
    assert m.mapping_confidence == "exact"


def test_register_operator_mapping_default_venue_polymarket(
    store: DuckDBStore,
) -> None:
    with store.connection() as conn:
        m = register_operator_mapping(
            conn,
            class_id="cls",
            condition_id="0xabc",
        )
    assert m.venue == "polymarket"


def test_same_class_can_be_mapped_to_both_venues(store: DuckDBStore) -> None:
    """The cross-venue contract: one class → two mappings, one per venue."""
    with store.connection() as conn:
        a = register_operator_mapping(
            conn,
            class_id="cpi_above_target",
            condition_id="0xabc",
            venue="polymarket",
        )
        b = register_operator_mapping(
            conn,
            class_id="cpi_above_target",
            condition_id="KX-CPI",
            venue="kalshi",
        )
    assert a.venue == "polymarket"
    assert b.venue == "kalshi"
    assert a.mapping_id != b.mapping_id


# -- compute_comparison cross-venue --------------------------------


def test_comparator_produces_distinct_rows_per_venue(store: DuckDBStore) -> None:
    """Two mappings against the same class — one Polymarket, one Kalshi —
    produce two distinct comparison rows with the right venue value.
    """
    _seed_polymarket_market(store, condition_id="0xabc")
    _seed_kalshi_market(store, ticker="KX-CPI")
    _seed_scan_record(store, class_id="cpi", scan_id="scan-1")

    with store.connection() as conn:
        poly = register_operator_mapping(
            conn,
            class_id="cpi",
            condition_id="0xabc",
            venue="polymarket",
        )
        kalshi = register_operator_mapping(
            conn,
            class_id="cpi",
            condition_id="KX-CPI",
            venue="kalshi",
        )

    cycle_id = "cy-test"
    poly_cmp, _ = compute_comparison(
        store=store,
        cycle_id=cycle_id,
        mapping=poly,
        scan_id="scan-1",
        library_version=1,
    )
    kalshi_cmp, _ = compute_comparison(
        store=store,
        cycle_id=cycle_id,
        mapping=kalshi,
        scan_id="scan-1",
        library_version=1,
    )

    assert poly_cmp.venue == "polymarket"
    assert poly_cmp.condition_id == "0xabc"
    assert poly_cmp.market_best_bid == pytest.approx(0.41)

    assert kalshi_cmp.venue == "kalshi"
    assert kalshi_cmp.condition_id == "KX-CPI"
    assert kalshi_cmp.market_best_bid == pytest.approx(0.30)
    # The two market-implied probabilities differ → two distinct deltas.
    assert poly_cmp.delta != kalshi_cmp.delta


# -- CLI: mispricing map --venue ----------------------------------


def test_cli_map_kalshi_refuses_unknown_ticker(store: DuckDBStore, tmp_path: Path) -> None:
    """`mispricing map --venue kalshi` exits non-zero for unknown tickers."""
    runner = CliRunner()
    db_path = tmp_path / "ksi_061.duckdb"
    result = runner.invoke(
        mispricing,
        [
            "map",
            "cls",
            "KX-DOES-NOT-EXIST",
            "--venue",
            "kalshi",
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code != 0
    assert "not present in kalshi_markets" in result.output


def test_cli_map_kalshi_refuses_non_binary(store: DuckDBStore, tmp_path: Path) -> None:
    """`mispricing map --venue kalshi` refuses scalar markets per OQ-KSI-003."""
    _seed_kalshi_market(store, ticker="KX-SCAL", market_type="scalar")
    runner = CliRunner()
    db_path = store.path
    result = runner.invoke(
        mispricing,
        [
            "map",
            "cls",
            "KX-SCAL",
            "--venue",
            "kalshi",
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code != 0
    assert "non-binary" in result.output
    assert "v1.2" in result.output


def test_cli_map_polymarket_default_unchanged(store: DuckDBStore, tmp_path: Path) -> None:
    """Existing Polymarket mappings work without --venue."""
    _seed_polymarket_market(store, condition_id="0xabc")
    runner = CliRunner()
    db_path = store.path
    result = runner.invoke(
        mispricing,
        [
            "map",
            "cls",
            "0xabc",
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code == 0
    assert "venue:             polymarket" in result.output


def test_cli_map_kalshi_binary_succeeds(store: DuckDBStore) -> None:
    _seed_kalshi_market(store, ticker="KX-CPI")
    runner = CliRunner()
    db_path = store.path
    result = runner.invoke(
        mispricing,
        [
            "map",
            "cls",
            "KX-CPI",
            "--venue",
            "kalshi",
            "--db",
            str(db_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "venue:             kalshi" in result.output
