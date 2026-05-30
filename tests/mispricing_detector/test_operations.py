"""T-MD-011 — persistence helpers acceptance tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.models import (
    Comparison,
    ComparisonCycle,
    ComparisonResolution,
    ComparisonTrace,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    MappingExistsError,
    complete_cycle,
    get_comparison,
    get_mapping,
    persist_comparison,
    persist_trace,
    query_comparisons,
    query_comparisons_for_market,
    query_cycle,
    query_existing_resolution_links,
    query_mappings,
    query_trace,
    register_mapping,
    remove_mapping,
    state_get,
    state_set,
    write_cycle,
    write_resolution_link,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "md_ops.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        yield c
    store.close()


# -- mapping operations -----------------------------------------------------


def test_register_mapping_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    m = register_mapping(
        conn,
        class_id="cls_a",
        condition_id="0xabc",
        mapping_type="direct",
        notes="operator note",
    )
    assert m.mapping_id
    assert m.mapping_confidence == "exact"
    assert m.mapped_by == "operator"
    fetched = get_mapping(conn, mapping_id=m.mapping_id)
    assert fetched is not None
    assert fetched.class_id == "cls_a"


def test_register_mapping_rejects_duplicate(conn: duckdb.DuckDBPyConnection) -> None:
    register_mapping(conn, class_id="cls_a", condition_id="0xabc", mapping_type="direct")
    with pytest.raises(MappingExistsError):
        register_mapping(conn, class_id="cls_a", condition_id="0xabc", mapping_type="direct")


def test_register_mapping_allows_inverted_polarity_after_aligned(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Same (class, condition) but different polarity is fine."""
    register_mapping(conn, class_id="cls_a", condition_id="0xabc", mapping_type="direct")
    register_mapping(
        conn,
        class_id="cls_a",
        condition_id="0xabc",
        mapping_type="proxy",
        polarity="inverted",
    )
    rows = query_mappings(conn, class_id="cls_a")
    assert len(rows) == 2


def test_remove_mapping_soft_deletes(conn: duckdb.DuckDBPyConnection) -> None:
    m = register_mapping(conn, class_id="cls_a", condition_id="0xabc", mapping_type="direct")
    assert remove_mapping(conn, mapping_id=m.mapping_id) is True
    fetched = get_mapping(conn, mapping_id=m.mapping_id)
    assert fetched is not None
    assert fetched.removed_at is not None


def test_query_mappings_default_excludes_removed(conn: duckdb.DuckDBPyConnection) -> None:
    m = register_mapping(conn, class_id="cls_a", condition_id="0xabc", mapping_type="direct")
    remove_mapping(conn, mapping_id=m.mapping_id)
    active = query_mappings(conn, class_id="cls_a")
    assert active == ()
    all_rows = query_mappings(conn, class_id="cls_a", include_removed=True)
    assert len(all_rows) == 1


def test_remove_then_register_same_pair(conn: duckdb.DuckDBPyConnection) -> None:
    """After removal, a fresh mapping for the same pair is allowed."""
    m1 = register_mapping(conn, class_id="cls_a", condition_id="0xabc", mapping_type="direct")
    remove_mapping(conn, mapping_id=m1.mapping_id)
    m2 = register_mapping(conn, class_id="cls_a", condition_id="0xabc", mapping_type="proxy")
    assert m2.mapping_id != m1.mapping_id


def test_query_mappings_filters(conn: duckdb.DuckDBPyConnection) -> None:
    register_mapping(conn, class_id="cls_a", condition_id="0xabc", mapping_type="direct")
    register_mapping(
        conn,
        class_id="cls_b",
        condition_id="0xdef",
        mapping_type="proxy",
        mapping_confidence="inferred",
    )
    by_class = query_mappings(conn, class_id="cls_a")
    by_market = query_mappings(conn, condition_id="0xdef")
    by_conf = query_mappings(conn, confidence="inferred")
    assert len(by_class) == 1
    assert len(by_market) == 1
    assert len(by_conf) == 1
    assert by_conf[0].class_id == "cls_b"


# -- cycle / comparison operations ------------------------------------------


def _cycle(cycle_id: str = "cy-1") -> ComparisonCycle:
    return ComparisonCycle(
        cycle_id=cycle_id,
        started_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
        completed_at=None,
        comparisons_total=0,
        surfaced_count=0,
        suppressed_breakdown={},
        library_version_at_cycle=1,
        scan_id_consumed="scan-1",
    )


def _comparison(
    comparison_id: str = "cmp-1",
    *,
    cycle_id: str = "cy-1",
    surfaced: bool = False,
    posterior: float = 0.20,
    market: float | None = 0.10,
) -> Comparison:
    return Comparison(
        comparison_id=comparison_id,
        cycle_id=cycle_id,
        mapping_id="map-1",
        class_id="cls_a",
        condition_id="0xabc",
        outcome_token_id="token-yes",
        polarity="aligned",
        scan_id="scan-1",
        model_probability=posterior,
        model_ci_lower=posterior * 0.7,
        model_ci_upper=posterior * 1.3,
        market_probability=market,
        market_best_bid=(market - 0.005) if market else None,
        market_best_ask=(market + 0.005) if market else None,
        market_last_trade_price=market,
        market_volume_24h=15000.0,
        market_spread_bps=100,
        market_snapshot_ts=datetime(2026, 5, 15, 11, tzinfo=UTC),
        delta=(posterior - market) if market else None,
        log_odds_delta=0.85 if market else None,
        ci_overlap=False,
        expected_value=0.05 if market else None,
        confidence_weighted_score=0.85 if surfaced else None,
        surfaced=surfaced,
        computed_at=datetime(2026, 5, 15, 12, 1, tzinfo=UTC),
    )


def test_write_then_query_cycle(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    fetched = query_cycle(conn, cycle_id="cy-1")
    assert fetched is not None
    assert fetched.cycle_id == "cy-1"
    assert fetched.comparisons_total == 0


def test_complete_cycle_updates_aggregates(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    complete_cycle(
        conn,
        cycle_id="cy-1",
        completed_at=datetime(2026, 5, 15, 12, 5, tzinfo=UTC),
        comparisons_total=8,
        surfaced_count=2,
        suppressed_breakdown={"ci_overlap": 4, "low_liquidity": 2},
    )
    fetched = query_cycle(conn, cycle_id="cy-1")
    assert fetched is not None
    assert fetched.comparisons_total == 8
    assert fetched.surfaced_count == 2
    assert dict(fetched.suppressed_breakdown) == {"ci_overlap": 4, "low_liquidity": 2}


def test_persist_comparison_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_comparison(conn, _comparison(comparison_id="cmp-1", surfaced=True))
    persist_comparison(conn, _comparison(comparison_id="cmp-2", surfaced=False))
    fetched = query_comparisons(conn, cycle_id="cy-1")
    assert len(fetched) == 2
    surfaced = next(c for c in fetched if c.comparison_id == "cmp-1")
    assert surfaced.surfaced is True


def test_persist_comparison_idempotent(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_comparison(conn, _comparison(posterior=0.10))
    persist_comparison(conn, _comparison(posterior=0.30))
    fetched = get_comparison(conn, comparison_id="cmp-1")
    assert fetched is not None
    assert fetched.model_probability == pytest.approx(0.30)
    rows = conn.execute("SELECT COUNT(*) FROM comparisons WHERE comparison_id = 'cmp-1'").fetchone()
    assert rows is not None and rows[0] == 1


def test_query_comparisons_surfaced_only(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_comparison(conn, _comparison(comparison_id="cmp-1", surfaced=True))
    persist_comparison(conn, _comparison(comparison_id="cmp-2", surfaced=False))
    surfaced_only = query_comparisons(conn, surfaced_only=True)
    assert len(surfaced_only) == 1
    assert surfaced_only[0].comparison_id == "cmp-1"


def test_persist_trace_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_comparison(conn, _comparison())
    persist_trace(
        conn,
        ComparisonTrace(
            comparison_id="cmp-1",
            payload={"case_for_model": ["a"], "case_for_market": ["b"]},
        ),
    )
    fetched = query_trace(conn, comparison_id="cmp-1")
    assert fetched is not None
    assert fetched.payload["case_for_model"] == ["a"]
    assert fetched.payload["case_for_market"] == ["b"]


def test_write_resolution_link_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_comparison(conn, _comparison())
    write_resolution_link(
        conn,
        ComparisonResolution(
            comparison_id="cmp-1",
            condition_id="0xabc",
            resolution_outcome="yes",
            resolution_ts=datetime(2026, 6, 1, tzinfo=UTC),
            model_probability_at_comparison=0.20,
            market_probability_at_comparison=0.10,
            polarity_at_comparison="aligned",
            outcome_observed=1,
            linked_at=datetime(2026, 6, 1, 0, 1, tzinfo=UTC),
        ),
    )
    links = query_existing_resolution_links(conn, condition_id="0xabc")
    assert links == {"cmp-1"}


def test_query_comparisons_for_market(conn: duckdb.DuckDBPyConnection) -> None:
    write_cycle(conn, _cycle())
    persist_comparison(conn, _comparison(comparison_id="cmp-1"))
    persist_comparison(
        conn,
        Comparison(
            comparison_id="cmp-2",
            cycle_id="cy-1",
            mapping_id="map-2",
            class_id="cls_b",
            condition_id="0xdef",
            outcome_token_id="token-yes",
            polarity="aligned",
            scan_id="scan-1",
            model_probability=0.30,
            model_ci_lower=0.20,
            model_ci_upper=0.40,
            market_probability=0.45,
            market_best_bid=0.44,
            market_best_ask=0.46,
            market_last_trade_price=0.45,
            market_volume_24h=20000.0,
            market_spread_bps=200,
            market_snapshot_ts=datetime(2026, 5, 15, 11, tzinfo=UTC),
            delta=-0.15,
            log_odds_delta=-0.6,
            ci_overlap=False,
            expected_value=None,
            confidence_weighted_score=None,
            surfaced=False,
        ),
    )
    for_abc = query_comparisons_for_market(conn, condition_id="0xabc")
    assert len(for_abc) == 1
    assert for_abc[0].class_id == "cls_a"


def test_state_kv_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    state_set(conn, "last_linkage_ts", "2026-05-15T08:00:00+00:00")
    assert state_get(conn, "last_linkage_ts") == "2026-05-15T08:00:00+00:00"
    # Idempotent on update.
    state_set(conn, "last_linkage_ts", "2026-05-15T12:00:00+00:00")
    assert state_get(conn, "last_linkage_ts") == "2026-05-15T12:00:00+00:00"


def test_query_comparisons_since_filter(conn: duckdb.DuckDBPyConnection) -> None:
    from dataclasses import replace

    write_cycle(conn, _cycle())
    old = _comparison(comparison_id="cmp-old")
    new = replace(
        _comparison(comparison_id="cmp-new"),
        computed_at=datetime(2026, 5, 16, tzinfo=UTC),
    )
    persist_comparison(conn, old)
    persist_comparison(conn, new)
    after = query_comparisons(conn, since=datetime(2026, 5, 16, tzinfo=UTC) - timedelta(hours=1))
    assert len(after) == 1
    assert after[0].comparison_id == "cmp-new"
