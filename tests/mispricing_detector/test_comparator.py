"""T-MD-034 / T-MD-040 — comparator + cycle orchestrator tests."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.engines.comparator import (
    NoScanAvailableError,
    compute_comparison,
    run_cycle,
)
from razor_rooster.mispricing_detector.models import ClassMarketMapping
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    query_comparisons,
    query_cycle,
    query_trace,
    register_mapping,
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


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "md_compare.duckdb"
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


def _occurrences(_conn: object) -> pd.DataFrame:
    return pd.DataFrame({"occurrence_ts": pd.to_datetime([], utc=True)})


def _make_class(class_id: str = "test_cls", *, sector: Sector = Sector.PUBLIC_HEALTH) -> EventClass:
    return EventClass(
        class_id=class_id,
        title=f"Test class {class_id} title",
        description=f"Test class {class_id} description",
        domain_sector=sector,
        occurrence_query=_occurrences,
    )


def _seed_signal_scanner(
    store: DuckDBStore,
    *,
    class_id: str = "test_cls",
    posterior: float = 0.30,
    posterior_ci_lower: float = 0.20,
    posterior_ci_upper: float = 0.40,
    signature_confidence: float = 0.8,
    library_stale_warning: bool = False,
    embedded_warnings: tuple[str, ...] = (),
) -> str:
    """Insert a scan_summary, scan_record, and scan_trace for the class."""
    scan_id = "scan-test-001"
    started = datetime(2026, 5, 15, 8, tzinfo=UTC)
    completed = started + timedelta(minutes=2)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO scan_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                scan_id,
                started,
                completed,
                1,
                1,
                1,
                0,
                0,
                0,
                0,
                None,
                None,
            ],
        )
        conn.execute(
            "INSERT INTO scan_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                scan_id,
                class_id,
                1,
                1,
                started,
                started,
                completed,
                0.05,
                0.025,
                0.075,
                posterior,
                posterior_ci_lower,
                posterior_ci_upper,
                1.0,
                False,
                None,
                signature_confidence,
                signature_confidence < 0.3,
                False,
                library_stale_warning,
                False,
                False,
                None,
                None,
            ],
        )
        trace_payload = {
            "class_id": class_id,
            "warnings": list(embedded_warnings),
            "precursors": [
                {
                    "variable_id": "v1",
                    "title": "First precursor",
                    "fired": True,
                    "hit_rate": 0.7,
                    "false_positive_rate": 0.2,
                    "likelihood_ratio_applied": 3.5,
                }
            ],
        }
        conn.execute(
            "INSERT INTO scan_traces VALUES (?, ?, ?)",
            [scan_id, class_id, json.dumps(trace_payload)],
        )
    return scan_id


def _seed_polymarket(
    store: DuckDBStore,
    *,
    condition_id: str = "0xabc",
    market_type: str = "binary",
    yes_token_id: str = "tok-yes",
    bid: float | None = 0.09,
    ask: float | None = 0.11,
    last_trade: float | None = 0.10,
    volume_24h: float | None = 20000.0,
    spread_bps: int | None = 200,
    snapshot_ts: datetime | None = None,
    sector: str = "public_health",
    question: str = "Will WHO declare PHEIC in 2026?",
) -> None:
    snapshot_ts = snapshot_ts or datetime(2026, 5, 15, 11, tzinfo=UTC)
    now = datetime(2026, 5, 15, tzinfo=UTC)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO polymarket_markets ("
            "source_id, source_record_id, source_publication_ts, fetch_ts, "
            "connector_version, source_payload_json, superseded_at, "
            "condition_id, slug, question, description, category, subcategory, "
            "tags, event_id, market_type, outcome_tokens, end_date, active, "
            "closed, resolved, volume_lifetime, created_at_polymarket, "
            "last_updated_polymarket, removed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, NULL, "
            "NULL, ?, ?, NULL, TRUE, FALSE, FALSE, NULL, NULL, NULL, NULL)",
            [
                "polymarket",
                f"market-{condition_id}",
                now,
                now,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                condition_id,
                f"slug-{condition_id}",
                question,
                market_type,
                json.dumps(
                    [
                        {"id": yes_token_id, "outcome": "Yes"},
                        {"id": "tok-no", "outcome": "No"},
                    ]
                ),
            ],
        )
        conn.execute(
            "INSERT INTO polymarket_sector_mapping ("
            "condition_id, razor_sector, secondary_sectors, confidence, "
            "mapped_at, mapped_by"
            ") VALUES (?, ?, NULL, 'inferred', ?, 'auto')",
            [condition_id, sector, now],
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
                f"snap-{condition_id}",
                snapshot_ts,
                now,
                "test@1",
                json.dumps({"raw": "synthetic"}),
                condition_id,
                yes_token_id,
                snapshot_ts,
                ((bid or 0.0) + (ask or 0.0)) / 2.0
                if bid is not None and ask is not None
                else last_trade,
                bid,
                ask,
                last_trade,
                snapshot_ts,
                volume_24h,
                spread_bps,
            ],
        )


def test_compute_comparison_basic_surfaces_strong_disagreement(
    store: DuckDBStore,
) -> None:
    cls = _make_class()
    registry.register(cls)
    scan_id = _seed_signal_scanner(store, posterior=0.30)
    _seed_polymarket(store, bid=0.09, ask=0.11, last_trade=0.10)
    mapping = ClassMarketMapping(
        mapping_id="m-1",
        class_id="test_cls",
        condition_id="0xabc",
        mapping_type="direct",
        mapping_confidence="exact",
        polarity="aligned",
    )
    comparison, trace = compute_comparison(
        store=store,
        cycle_id="cy-1",
        mapping=mapping,
        scan_id=scan_id,
        library_version=1,
        sector="public_health",
        liquidity_floor=10000.0,
        now=datetime(2026, 5, 15, 12, tzinfo=UTC),
    )
    assert comparison.error is None
    assert comparison.market_probability == pytest.approx(0.10)
    assert comparison.model_probability == pytest.approx(0.30)
    assert comparison.delta == pytest.approx(0.20)
    assert comparison.surfaced is True
    # Trace has both case sections present.
    assert "case_for_model" in trace.payload
    assert "case_for_market" in trace.payload


def test_compute_comparison_inverted_polarity_flips_market(
    store: DuckDBStore,
) -> None:
    cls = _make_class()
    registry.register(cls)
    scan_id = _seed_signal_scanner(store, posterior=0.30)
    _seed_polymarket(store, bid=0.09, ask=0.11, last_trade=0.10)
    mapping = ClassMarketMapping(
        mapping_id="m-1",
        class_id="test_cls",
        condition_id="0xabc",
        mapping_type="direct",
        mapping_confidence="exact",
        polarity="inverted",
    )
    comparison, _ = compute_comparison(
        store=store,
        cycle_id="cy-1",
        mapping=mapping,
        scan_id=scan_id,
        library_version=1,
        sector="public_health",
        now=datetime(2026, 5, 15, 12, tzinfo=UTC),
    )
    # Aligned market_p would be 0.10; inverted -> 0.90.
    assert comparison.market_probability == pytest.approx(0.90)
    # Delta = model - inverted_market = 0.30 - 0.90 = -0.60.
    assert comparison.delta == pytest.approx(-0.60)


def test_compute_comparison_low_mapping_confidence_suppressed(
    store: DuckDBStore,
) -> None:
    cls = _make_class()
    registry.register(cls)
    scan_id = _seed_signal_scanner(store, posterior=0.30)
    _seed_polymarket(store)
    mapping = ClassMarketMapping(
        mapping_id="m-low",
        class_id="test_cls",
        condition_id="0xabc",
        mapping_type="proxy",
        mapping_confidence="low",
        polarity="aligned",
    )
    comparison, _ = compute_comparison(
        store=store,
        cycle_id="cy-1",
        mapping=mapping,
        scan_id=scan_id,
        library_version=1,
        sector="public_health",
        now=datetime(2026, 5, 15, 12, tzinfo=UTC),
    )
    assert comparison.surfaced is False
    assert "low_mapping_confidence" in comparison.suppression_reasons


def test_compute_comparison_stale_market_price_suppressed(
    store: DuckDBStore,
) -> None:
    cls = _make_class()
    registry.register(cls)
    scan_id = _seed_signal_scanner(store, posterior=0.30)
    # Snapshot 2 days old.
    old_ts = datetime(2026, 5, 13, tzinfo=UTC)
    _seed_polymarket(store, snapshot_ts=old_ts)
    mapping = ClassMarketMapping(
        mapping_id="m-1",
        class_id="test_cls",
        condition_id="0xabc",
        mapping_type="direct",
        mapping_confidence="exact",
        polarity="aligned",
    )
    comparison, _ = compute_comparison(
        store=store,
        cycle_id="cy-1",
        mapping=mapping,
        scan_id=scan_id,
        library_version=1,
        sector="public_health",
        now=datetime(2026, 5, 15, 12, tzinfo=UTC),
    )
    assert comparison.stale_market_price is True
    assert comparison.surfaced is False
    assert "stale_market_price" in comparison.suppression_reasons


def test_compute_comparison_no_market_price_suppressed(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    scan_id = _seed_signal_scanner(store, posterior=0.30)
    _seed_polymarket(store, bid=None, ask=None, last_trade=None, volume_24h=None, spread_bps=None)
    mapping = ClassMarketMapping(
        mapping_id="m-1",
        class_id="test_cls",
        condition_id="0xabc",
        mapping_type="direct",
        mapping_confidence="exact",
        polarity="aligned",
    )
    comparison, _ = compute_comparison(
        store=store,
        cycle_id="cy-1",
        mapping=mapping,
        scan_id=scan_id,
        library_version=1,
        sector="public_health",
        now=datetime(2026, 5, 15, 12, tzinfo=UTC),
    )
    assert comparison.no_market_price is True
    assert comparison.surfaced is False


def test_compute_comparison_low_liquidity_suppressed(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    scan_id = _seed_signal_scanner(store, posterior=0.30)
    _seed_polymarket(store, volume_24h=500.0)
    mapping = ClassMarketMapping(
        mapping_id="m-1",
        class_id="test_cls",
        condition_id="0xabc",
        mapping_type="direct",
        mapping_confidence="exact",
        polarity="aligned",
    )
    comparison, _ = compute_comparison(
        store=store,
        cycle_id="cy-1",
        mapping=mapping,
        scan_id=scan_id,
        library_version=1,
        sector="public_health",
        liquidity_floor=10000.0,
        now=datetime(2026, 5, 15, 12, tzinfo=UTC),
    )
    assert comparison.low_liquidity is True
    assert comparison.surfaced is False
    assert "low_liquidity" in comparison.suppression_reasons


def test_compute_comparison_failure_isolation(store: DuckDBStore) -> None:
    """If the market is missing, the comparison gets an error rather than throw."""
    cls = _make_class()
    registry.register(cls)
    scan_id = _seed_signal_scanner(store, posterior=0.30)
    # No polymarket_markets row inserted.
    mapping = ClassMarketMapping(
        mapping_id="m-1",
        class_id="test_cls",
        condition_id="0xnope",
        mapping_type="direct",
        mapping_confidence="exact",
        polarity="aligned",
    )
    comparison, _ = compute_comparison(
        store=store,
        cycle_id="cy-1",
        mapping=mapping,
        scan_id=scan_id,
        library_version=1,
        sector="public_health",
        now=datetime(2026, 5, 15, 12, tzinfo=UTC),
    )
    assert comparison.error is not None
    assert "MissingMarketError" in comparison.error
    assert comparison.surfaced is False


def test_run_cycle_no_scan_raises(store: DuckDBStore) -> None:
    with pytest.raises(NoScanAvailableError):
        run_cycle(store, now=datetime(2026, 5, 15, 12, tzinfo=UTC))


def test_run_cycle_persists_comparisons(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    _seed_signal_scanner(store, posterior=0.30)
    _seed_polymarket(store)
    with store.connection() as conn:
        register_mapping(
            conn,
            class_id="test_cls",
            condition_id="0xabc",
            mapping_type="direct",
        )
    report = run_cycle(store, liquidity_floor=10000.0, now=datetime(2026, 5, 15, 12, tzinfo=UTC))
    assert report.completed_at is not None
    assert len(report.comparisons) == 1
    assert report.surfaced == 1
    with store.connection() as conn:
        persisted = query_comparisons(conn, cycle_id=report.cycle_id)
        cycle = query_cycle(conn, cycle_id=report.cycle_id)
        trace = query_trace(conn, comparison_id=persisted[0].comparison_id)
    assert len(persisted) == 1
    assert cycle is not None
    assert cycle.comparisons_total == 1
    assert cycle.surfaced_count == 1
    assert trace is not None


def test_run_cycle_filters_by_class(store: DuckDBStore) -> None:
    cls_a = _make_class("cls_a")
    cls_b = _make_class("cls_b", sector=Sector.GEOPOLITICAL)
    registry.register(cls_a)
    registry.register(cls_b)
    _seed_signal_scanner(store, class_id="cls_a")
    # Add a second scan record for cls_b within the same scan_id.
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO scan_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "scan-test-001",
                "cls_b",
                1,
                1,
                datetime(2026, 5, 15, 8, tzinfo=UTC),
                datetime(2026, 5, 15, 8, tzinfo=UTC),
                datetime(2026, 5, 15, 8, 2, tzinfo=UTC),
                0.05,
                0.025,
                0.075,
                0.20,
                0.10,
                0.30,
                0.5,
                False,
                None,
                0.7,
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
                "scan-test-001",
                "cls_b",
                json.dumps({"class_id": "cls_b", "warnings": [], "precursors": []}),
            ],
        )
    _seed_polymarket(store, condition_id="0xabc", question="Will WHO PHEIC?")
    _seed_polymarket(
        store,
        condition_id="0xdef",
        question="Will conflict escalate?",
        sector="geopolitical",
    )
    with store.connection() as conn:
        register_mapping(conn, class_id="cls_a", condition_id="0xabc", mapping_type="direct")
        register_mapping(conn, class_id="cls_b", condition_id="0xdef", mapping_type="direct")
    report = run_cycle(
        store,
        class_id_filter="cls_a",
        now=datetime(2026, 5, 15, 12, tzinfo=UTC),
    )
    assert {c.class_id for c in report.comparisons} == {"cls_a"}


def test_run_cycle_records_suppression_breakdown(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    _seed_signal_scanner(store, posterior=0.07)  # similar to market price
    _seed_polymarket(store, bid=0.05, ask=0.10)  # CI overlap likely
    with store.connection() as conn:
        register_mapping(
            conn,
            class_id="test_cls",
            condition_id="0xabc",
            mapping_type="direct",
        )
    report = run_cycle(store, liquidity_floor=10000.0, now=datetime(2026, 5, 15, 12, tzinfo=UTC))
    # Either ci_overlap or delta_below_threshold should appear.
    assert report.suppressed_breakdown
    assert sum(report.suppressed_breakdown.values()) >= 1


def test_run_cycle_skips_multi_outcome_market(store: DuckDBStore) -> None:
    cls = _make_class()
    registry.register(cls)
    _seed_signal_scanner(store, posterior=0.30)
    _seed_polymarket(store, market_type="multi", question="Multi outcome?")
    with store.connection() as conn:
        register_mapping(
            conn,
            class_id="test_cls",
            condition_id="0xabc",
            mapping_type="direct",
        )
    report = run_cycle(store, now=datetime(2026, 5, 15, 12, tzinfo=UTC))
    # Multi-outcome markets are skipped (no comparison row).
    assert len(report.comparisons) == 0
    assert report.suppressed_breakdown.get("multi_outcome_market_skipped", 0) == 1
