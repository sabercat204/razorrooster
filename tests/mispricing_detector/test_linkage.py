"""T-MD-041 — linkage pass tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.engines.linkage import run_linkage_pass
from razor_rooster.mispricing_detector.models import Comparison
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    persist_comparison,
    state_get,
    write_cycle,
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
    db_path = tmp_path / "linkage.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        run_pending_mispricing_migrations(conn)
    try:
        yield s
    finally:
        s.close()


def _seed_comparison(
    store: DuckDBStore,
    *,
    comparison_id: str,
    condition_id: str,
    polarity: str = "aligned",
    market_p: float | None = 0.10,
) -> None:
    from razor_rooster.mispricing_detector.models import ComparisonCycle

    now = datetime(2026, 5, 15, 12, tzinfo=UTC)
    with store.connection() as conn:
        write_cycle(
            conn,
            ComparisonCycle(
                cycle_id=f"cy-{comparison_id}",
                started_at=now,
                completed_at=now,
                comparisons_total=1,
                surfaced_count=0,
                suppressed_breakdown={},
                library_version_at_cycle=1,
                scan_id_consumed="scan-1",
            ),
        )
        persist_comparison(
            conn,
            Comparison(
                comparison_id=comparison_id,
                cycle_id=f"cy-{comparison_id}",
                mapping_id="m-1",
                class_id="cls",
                condition_id=condition_id,
                outcome_token_id="tok-yes",
                polarity=polarity,  # type: ignore[arg-type]
                scan_id="scan-1",
                model_probability=0.30,
                model_ci_lower=0.20,
                model_ci_upper=0.40,
                market_probability=market_p,
                market_best_bid=None,
                market_best_ask=None,
                market_last_trade_price=None,
                market_volume_24h=None,
                market_spread_bps=None,
                market_snapshot_ts=None,
                delta=None,
                log_odds_delta=None,
                ci_overlap=False,
                expected_value=None,
                confidence_weighted_score=None,
                surfaced=False,
                computed_at=now,
            ),
        )


def _seed_resolution(
    store: DuckDBStore,
    *,
    condition_id: str,
    label: str | None,
    resolution_ts: datetime,
    invalidated: bool = False,
) -> None:
    now = datetime(2026, 5, 20, tzinfo=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO polymarket_resolutions ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "condition_id, winning_outcome_token_id, winning_outcome_label, "
            "resolution_ts, resolution_source, resolution_metadata, "
            "final_yes_price, final_no_price, total_volume_at_resolution, "
            "invalidated"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 'gamma', NULL, ?, ?, ?, ?)",
            [
                "polymarket_resolutions",
                f"res-{condition_id}",
                now,
                now,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                condition_id,
                "tok-yes" if label == "Yes" else "tok-no",
                label,
                resolution_ts,
                1.0 if label == "Yes" else 0.0,
                0.0 if label == "Yes" else 1.0,
                25000.0,
                invalidated,
            ],
        )


def test_linkage_pass_creates_links(store: DuckDBStore) -> None:
    _seed_comparison(store, comparison_id="cmp-1", condition_id="0xabc")
    _seed_resolution(
        store,
        condition_id="0xabc",
        label="Yes",
        resolution_ts=datetime(2026, 6, 1, tzinfo=UTC),
    )
    report = run_linkage_pass(store, now=datetime(2026, 6, 1, 0, 1, tzinfo=UTC))
    assert report.new_resolutions_processed == 1
    assert report.new_links_written == 1
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT outcome_observed FROM comparison_resolutions WHERE comparison_id = ?",
            ["cmp-1"],
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1


def test_linkage_pass_handles_no_resolutions(store: DuckDBStore) -> None:
    """No resolutions present -> empty report, no errors."""
    report = run_linkage_pass(store, now=datetime(2026, 6, 1, tzinfo=UTC))
    assert report.new_resolutions_processed == 0
    assert report.new_links_written == 0


def test_linkage_pass_inverted_polarity_no_resolution(
    store: DuckDBStore,
) -> None:
    """Inverted mapping + NO resolution → outcome_observed = 1."""
    _seed_comparison(store, comparison_id="cmp-inv", condition_id="0xinv", polarity="inverted")
    _seed_resolution(
        store,
        condition_id="0xinv",
        label="No",
        resolution_ts=datetime(2026, 6, 1, tzinfo=UTC),
    )
    run_linkage_pass(store, now=datetime(2026, 6, 1, 0, 1, tzinfo=UTC))
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT outcome_observed FROM comparison_resolutions WHERE comparison_id = 'cmp-inv'"
        ).fetchall()
    assert rows[0][0] == 1


def test_linkage_pass_invalid_resolution_outcome_observed_zero(
    store: DuckDBStore,
) -> None:
    _seed_comparison(store, comparison_id="cmp-invalid", condition_id="0xinvalid")
    _seed_resolution(
        store,
        condition_id="0xinvalid",
        label=None,
        resolution_ts=datetime(2026, 6, 1, tzinfo=UTC),
        invalidated=True,
    )
    run_linkage_pass(store, now=datetime(2026, 6, 1, 0, 1, tzinfo=UTC))
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT resolution_outcome, outcome_observed FROM comparison_resolutions "
            "WHERE comparison_id = 'cmp-invalid'"
        ).fetchall()
    assert rows[0][0] == "invalid"
    assert rows[0][1] == 0


def test_linkage_pass_idempotent(store: DuckDBStore) -> None:
    _seed_comparison(store, comparison_id="cmp-1", condition_id="0xabc")
    _seed_resolution(
        store,
        condition_id="0xabc",
        label="Yes",
        resolution_ts=datetime(2026, 6, 1, tzinfo=UTC),
    )
    run_linkage_pass(store, now=datetime(2026, 6, 1, 0, 1, tzinfo=UTC))
    # Second run should not duplicate.
    second = run_linkage_pass(store, now=datetime(2026, 6, 1, 0, 2, tzinfo=UTC))
    assert second.new_links_written == 0
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM comparison_resolutions WHERE comparison_id = 'cmp-1'"
        ).fetchone()
    assert rows is not None and rows[0] == 1


def test_linkage_pass_resumes_from_state(store: DuckDBStore) -> None:
    _seed_comparison(store, comparison_id="cmp-1", condition_id="0xabc")
    _seed_resolution(
        store,
        condition_id="0xabc",
        label="Yes",
        resolution_ts=datetime(2026, 6, 1, tzinfo=UTC),
    )
    run_linkage_pass(store, now=datetime(2026, 6, 1, 0, 1, tzinfo=UTC))
    with store.connection() as conn:
        last_ts = state_get(conn, "last_linkage_ts")
    assert last_ts is not None
    parsed = datetime.fromisoformat(last_ts)
    assert parsed >= datetime(2026, 6, 1, tzinfo=UTC)


def test_linkage_pass_skips_resolutions_before_state(store: DuckDBStore) -> None:
    """A resolution with timestamp earlier than last_linkage_ts is not re-linked."""
    _seed_comparison(store, comparison_id="cmp-old", condition_id="0xold")
    _seed_resolution(
        store,
        condition_id="0xold",
        label="Yes",
        resolution_ts=datetime(2025, 1, 1, tzinfo=UTC),
    )
    # Pre-set state to AFTER the resolution timestamp.
    with store.connection() as conn:
        from razor_rooster.mispricing_detector.persistence.operations import state_set

        state_set(conn, "last_linkage_ts", datetime(2026, 1, 1, tzinfo=UTC).isoformat())
    report = run_linkage_pass(store, now=datetime(2026, 6, 1, tzinfo=UTC))
    assert report.new_resolutions_processed == 0


def test_linkage_pass_links_multiple_comparisons_for_same_market(
    store: DuckDBStore,
) -> None:
    """Many comparisons over time on the same market all get linked once it
    resolves."""
    _seed_comparison(store, comparison_id="cmp-1", condition_id="0xabc")
    _seed_comparison(store, comparison_id="cmp-2", condition_id="0xabc")
    _seed_resolution(
        store,
        condition_id="0xabc",
        label="Yes",
        resolution_ts=datetime(2026, 6, 1, tzinfo=UTC),
    )
    report = run_linkage_pass(store, now=datetime(2026, 6, 1, 0, 1, tzinfo=UTC))
    assert report.new_links_written == 2
