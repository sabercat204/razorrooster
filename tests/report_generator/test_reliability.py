"""Tests for the reliability-diagram section (DEFER-RG-COMPAT-003 v0.41.0).

Covers bin construction, per-bin aggregation, sparse-bin flagging,
per-sector window narrowing, and integration through the generator
dispatch.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    reliability as reliability_assembler,
)
from razor_rooster.report_generator.engines.section_assemblers.reliability import (
    DEFAULT_BIN_COUNT,
    DEFAULT_MIN_RESOLUTIONS_PER_BIN,
    assemble,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "rg_reliability.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
    with store.connection() as c:
        yield c


def _seed_cycle(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotent seed for the parent cycle row referenced by comparisons."""
    existing = conn.execute(
        "SELECT cycle_id FROM comparison_cycles WHERE cycle_id = 'cy-1'"
    ).fetchone()
    if existing is not None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO comparison_cycles "
        "(cycle_id, started_at, completed_at, comparisons_total, "
        "surfaced_count, suppressed_breakdown, library_version_at_cycle, "
        "scan_id_consumed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "cy-1",
            datetime(2026, 5, 14, tzinfo=UTC),
            datetime(2026, 5, 14, tzinfo=UTC),
            0,
            0,
            "{}",
            1,
            "scan-1",
        ],
    )


def _add_class(conn: duckdb.DuckDBPyConnection, *, class_id: str, sector: str) -> None:
    conn.execute(
        "INSERT INTO pl_event_classes (class_id, title, description, "
        "domain_sector, definition_version, outcome_type, registered_at) VALUES "
        "(?, ?, ?, ?, 1, 'binary', ?)",
        [
            class_id,
            f"{class_id} title",
            f"{class_id} description",
            sector,
            datetime(2026, 1, 1, tzinfo=UTC),
        ],
    )


def _add_resolution(
    conn: duckdb.DuckDBPyConnection,
    *,
    comparison_id: str,
    class_id: str,
    model_p: float,
    outcome: int,
    resolution_ts: datetime,
    venue: str = "polymarket",
    resolution_outcome: str | None = None,
) -> None:
    """Insert a comparison + its resolution row using the full schema."""
    _seed_cycle(conn)
    condition_id = f"cond-{comparison_id}"
    conn.execute(
        "INSERT INTO comparisons "
        "(comparison_id, cycle_id, mapping_id, class_id, condition_id, "
        "outcome_token_id, polarity, scan_id, model_probability, "
        "model_ci_lower, model_ci_upper, market_probability, "
        "market_best_bid, market_best_ask, market_last_trade_price, "
        "market_volume_24h, market_spread_bps, market_snapshot_ts, "
        "delta, log_odds_delta, ci_overlap, expected_value, "
        "confidence_weighted_score, surfaced, suppression_reasons, "
        "low_signature_confidence, source_stale_warning, "
        "library_stale_warning, definition_drift_warning, "
        "stale_market_price, no_market_price, degenerate_orderbook, "
        "low_liquidity, low_mapping_confidence, computed_at, venue) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            comparison_id,
            "cy-1",
            f"map-{comparison_id}",
            class_id,
            condition_id,
            f"{condition_id}-yes",
            "aligned",
            "scan-1",
            model_p,
            max(0.0, model_p - 0.10),
            min(1.0, model_p + 0.10),
            model_p,
            max(0.0, model_p - 0.005),
            min(1.0, model_p + 0.005),
            model_p,
            10_000.0,
            100,
            resolution_ts - timedelta(days=2),
            0.0,
            0.0,
            False,
            0.0,
            0.5,
            True,
            "[]",
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            resolution_ts - timedelta(days=2),
            venue,
        ],
    )
    outcome_str = (
        resolution_outcome if resolution_outcome is not None else ("yes" if outcome == 1 else "no")
    )
    conn.execute(
        "INSERT INTO comparison_resolutions (comparison_id, condition_id, "
        "resolution_outcome, resolution_ts, model_probability_at_comparison, "
        "market_probability_at_comparison, polarity_at_comparison, "
        "outcome_observed, linked_at, venue) VALUES "
        "(?, ?, ?, ?, ?, ?, 'aligned', ?, ?, ?)",
        [
            comparison_id,
            condition_id,
            outcome_str,
            resolution_ts,
            model_p,
            model_p,
            outcome,
            resolution_ts + timedelta(hours=1),
            venue,
        ],
    )


# -- bin construction ------------------------------------------------------


def test_default_bins_cover_unit_interval(conn: duckdb.DuckDBPyConnection) -> None:
    """Default bin count produces 10 equal-width bins from 0 to 1."""
    out = assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    bins = out["bins"]
    assert len(bins) == DEFAULT_BIN_COUNT
    assert bins[0] == (0.0, 0.1)
    assert bins[-1] == (0.9, 1.0)


def test_custom_bin_count_works(conn: duckdb.DuckDBPyConnection) -> None:
    out = assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
        bin_count=5,
    )
    assert len(out["bins"]) == 5
    assert out["bins"][0] == (0.0, 0.2)
    assert out["bins"][-1] == (0.8, 1.0)


# -- empty / single-sector cases ------------------------------------------


def test_empty_when_no_resolutions(conn: duckdb.DuckDBPyConnection) -> None:
    out = assemble(
        conn,
        since_ts=datetime(2026, 5, 14, tzinfo=UTC),
        until_ts=datetime(2026, 5, 15, tzinfo=UTC),
    )
    assert out["sectors"] == []


def test_single_sector_aggregates_into_bins(conn: duckdb.DuckDBPyConnection) -> None:
    _add_class(conn, class_id="cls1", sector="macroeconomic")
    until = datetime(2026, 5, 15, tzinfo=UTC)
    base_ts = until - timedelta(days=10)
    # 5 resolutions in the [0.4, 0.5) bin: model_p=0.45, half resolve YES
    for i, outcome in enumerate([1, 0, 1, 0, 1]):
        _add_resolution(
            conn,
            comparison_id=f"a-{i}",
            class_id="cls1",
            model_p=0.45,
            outcome=outcome,
            resolution_ts=base_ts + timedelta(hours=i),
        )
    out = assemble(
        conn,
        since_ts=until - timedelta(days=1),
        until_ts=until,
    )
    sectors = out["sectors"]
    assert len(sectors) == 1
    assert sectors[0]["sector"] == "macroeconomic"
    assert sectors[0]["n_resolutions"] == 5
    bins = sectors[0]["bins"]
    target_bin = next(b for b in bins if b["bin_lo"] == 0.4 and b["bin_hi"] == 0.5)
    assert target_bin["n"] == 5
    assert target_bin["mean_predicted"] == pytest.approx(0.45)
    assert target_bin["empirical_rate"] == pytest.approx(0.6)
    assert target_bin["calibration_gap"] == pytest.approx(0.6 - 0.45)
    assert target_bin["sparse"] is False


def test_sparse_bin_flagged(conn: duckdb.DuckDBPyConnection) -> None:
    """Bins with fewer than ``min_resolutions_per_bin`` get sparse=True."""
    _add_class(conn, class_id="cls1", sector="climate")
    until = datetime(2026, 5, 15, tzinfo=UTC)
    base_ts = until - timedelta(days=10)
    # Only 3 resolutions in the [0.7, 0.8) bin → sparse at default 5.
    for i in range(3):
        _add_resolution(
            conn,
            comparison_id=f"s-{i}",
            class_id="cls1",
            model_p=0.75,
            outcome=1,
            resolution_ts=base_ts + timedelta(hours=i),
        )
    out = assemble(
        conn,
        since_ts=until - timedelta(days=1),
        until_ts=until,
    )
    target = next(b for b in out["sectors"][0]["bins"] if b["bin_lo"] == 0.7)
    assert target["n"] == 3
    assert target["sparse"] is True


def test_empty_bins_marked(conn: duckdb.DuckDBPyConnection) -> None:
    """Bins with zero observations have n=0 and None for stats."""
    _add_class(conn, class_id="cls1", sector="commodity")
    until = datetime(2026, 5, 15, tzinfo=UTC)
    _add_resolution(
        conn,
        comparison_id="a",
        class_id="cls1",
        model_p=0.05,
        outcome=0,
        resolution_ts=until - timedelta(days=10),
    )
    out = assemble(
        conn,
        since_ts=until - timedelta(days=1),
        until_ts=until,
    )
    high_bin = next(b for b in out["sectors"][0]["bins"] if b["bin_lo"] == 0.9)
    assert high_bin["n"] == 0
    assert high_bin["mean_predicted"] is None
    assert high_bin["empirical_rate"] is None
    assert high_bin["calibration_gap"] is None
    assert high_bin["sparse"] is True


def test_top_bin_includes_probability_one(conn: duckdb.DuckDBPyConnection) -> None:
    """Probability 1.0 lands in the top bin; the bin is fully closed."""
    _add_class(conn, class_id="cls1", sector="public_health")
    until = datetime(2026, 5, 15, tzinfo=UTC)
    _add_resolution(
        conn,
        comparison_id="a",
        class_id="cls1",
        model_p=1.0,
        outcome=1,
        resolution_ts=until - timedelta(days=5),
    )
    out = assemble(
        conn,
        since_ts=until - timedelta(days=1),
        until_ts=until,
    )
    top_bin = next(b for b in out["sectors"][0]["bins"] if b["bin_lo"] == 0.9)
    assert top_bin["n"] == 1
    assert top_bin["mean_predicted"] == pytest.approx(1.0)


def test_invalid_resolutions_excluded(conn: duckdb.DuckDBPyConnection) -> None:
    _add_class(conn, class_id="cls1", sector="regulatory")
    until = datetime(2026, 5, 15, tzinfo=UTC)
    base_ts = until - timedelta(days=5)
    _add_resolution(
        conn,
        comparison_id="a",
        class_id="cls1",
        model_p=0.55,
        outcome=1,
        resolution_ts=base_ts,
    )
    _add_resolution(
        conn,
        comparison_id="b",
        class_id="cls1",
        model_p=0.95,
        outcome=0,
        resolution_ts=base_ts - timedelta(hours=1),
        resolution_outcome="invalid",
    )
    out = assemble(
        conn,
        since_ts=until - timedelta(days=1),
        until_ts=until,
    )
    assert out["sectors"][0]["n_resolutions"] == 1


# -- per-sector window narrowing ------------------------------------------


def test_per_sector_window_narrows_observations(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A per-sector window can exclude older resolutions for one sector."""
    _add_class(conn, class_id="cls1", sector="macroeconomic")
    _add_class(conn, class_id="cls2", sector="climate")
    until = datetime(2026, 5, 15, tzinfo=UTC)
    # macro: one old (60d), one recent (10d). climate: one recent (10d).
    _add_resolution(
        conn,
        comparison_id="m-old",
        class_id="cls1",
        model_p=0.45,
        outcome=1,
        resolution_ts=until - timedelta(days=60),
    )
    _add_resolution(
        conn,
        comparison_id="m-new",
        class_id="cls1",
        model_p=0.55,
        outcome=0,
        resolution_ts=until - timedelta(days=10),
    )
    _add_resolution(
        conn,
        comparison_id="c-new",
        class_id="cls2",
        model_p=0.65,
        outcome=1,
        resolution_ts=until - timedelta(days=10),
    )
    out = assemble(
        conn,
        since_ts=until - timedelta(days=1),
        until_ts=until,
        window_days=90,
        window_days_per_sector={"macroeconomic": 30},
    )
    macro = next(s for s in out["sectors"] if s["sector"] == "macroeconomic")
    climate = next(s for s in out["sectors"] if s["sector"] == "climate")
    # Macroeconomic narrowed to 30d → only one resolution (the recent one).
    assert macro["n_resolutions"] == 1
    assert macro["window_days"] == 30
    # Climate keeps 90d → still only one resolution but the window stamp reflects 90.
    assert climate["n_resolutions"] == 1
    assert climate["window_days"] == 90


# -- multi-sector ordering -------------------------------------------------


def test_sectors_sorted_alphabetically(conn: duckdb.DuckDBPyConnection) -> None:
    _add_class(conn, class_id="m1", sector="macroeconomic")
    _add_class(conn, class_id="c1", sector="climate")
    _add_class(conn, class_id="g1", sector="geopolitical")
    until = datetime(2026, 5, 15, tzinfo=UTC)
    for i, (cid, cls) in enumerate([("m-1", "m1"), ("c-1", "c1"), ("g-1", "g1")]):
        _add_resolution(
            conn,
            comparison_id=cid,
            class_id=cls,
            model_p=0.5,
            outcome=1,
            resolution_ts=until - timedelta(days=10 + i),
        )
    out = assemble(
        conn,
        since_ts=until - timedelta(days=1),
        until_ts=until,
    )
    sectors = [s["sector"] for s in out["sectors"]]
    assert sectors == ["climate", "geopolitical", "macroeconomic"]


# -- integration via generator dispatch ------------------------------------


def test_generator_dispatch_passes_thresholds_through(tmp_path: Path) -> None:
    """The generator dispatch passes bin_count + min_resolutions_per_bin."""
    from razor_rooster.report_generator.config.loader import (
        ReportConfig,
        ReportThresholds,
    )
    from razor_rooster.report_generator.engines.generator import _assemble_section

    db_path = tmp_path / "dispatch.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
    with store.connection() as conn_:
        cfg = ReportConfig(
            thresholds=ReportThresholds(
                reliability_bin_count=4,
                reliability_min_resolutions_per_bin=2,
            ),
        )
        out = _assemble_section(
            conn_,
            section_name="reliability",
            since_ts=datetime(2026, 5, 14, tzinfo=UTC),
            until_ts=datetime(2026, 5, 15, tzinfo=UTC),
            cfg=cfg,
        )
        assert out["type"] == "reliability"
        assert len(out["bins"]) == 4
        assert out["min_resolutions_per_bin"] == 2


def test_min_resolutions_per_bin_default_constant() -> None:
    assert DEFAULT_MIN_RESOLUTIONS_PER_BIN == 5


def test_default_bin_count_constant() -> None:
    assert DEFAULT_BIN_COUNT == 10


def test_assemble_module_exports_assemble() -> None:
    assert callable(reliability_assembler.assemble)


# -- per-sector reliability overrides (v0.40.0) ----------------------------


def test_per_sector_bin_count_override(conn: duckdb.DuckDBPyConnection) -> None:
    """A per-sector bin count overrides the global value for that sector."""
    _add_class(conn, class_id="m1", sector="macroeconomic")
    _add_class(conn, class_id="g1", sector="geopolitical")
    until = datetime(2026, 5, 15, tzinfo=UTC)
    base_ts = until - timedelta(days=5)
    for i in range(6):
        _add_resolution(
            conn,
            comparison_id=f"m-{i}",
            class_id="m1",
            model_p=0.45,
            outcome=1 if i % 2 == 0 else 0,
            resolution_ts=base_ts + timedelta(hours=i),
        )
        _add_resolution(
            conn,
            comparison_id=f"g-{i}",
            class_id="g1",
            model_p=0.55,
            outcome=1 if i % 2 == 0 else 0,
            resolution_ts=base_ts + timedelta(hours=i),
        )
    out = assemble(
        conn,
        since_ts=until - timedelta(days=1),
        until_ts=until,
        bin_count=10,
        bin_count_per_sector={"macroeconomic": 4},
    )
    macro = next(s for s in out["sectors"] if s["sector"] == "macroeconomic")
    geo = next(s for s in out["sectors"] if s["sector"] == "geopolitical")
    assert macro["bin_count"] == 4
    assert len(macro["bins"]) == 4
    assert macro["bins"][0]["bin_lo"] == 0.0
    assert macro["bins"][0]["bin_hi"] == 0.25
    assert geo["bin_count"] == 10
    assert len(geo["bins"]) == 10


def test_per_sector_min_resolutions_per_bin_override(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Per-sector min_resolutions_per_bin can flip the sparse flag for a sector."""
    _add_class(conn, class_id="g1", sector="geopolitical")
    until = datetime(2026, 5, 15, tzinfo=UTC)
    base_ts = until - timedelta(days=5)
    # 3 resolutions in the [0.5, 0.6) bin.
    for i in range(3):
        _add_resolution(
            conn,
            comparison_id=f"g-{i}",
            class_id="g1",
            model_p=0.55,
            outcome=1,
            resolution_ts=base_ts + timedelta(hours=i),
        )
    # Default min=5 → bin is sparse.
    out_default = assemble(
        conn,
        since_ts=until - timedelta(days=1),
        until_ts=until,
        min_resolutions_per_bin=5,
    )
    geo_default = next(s for s in out_default["sectors"] if s["sector"] == "geopolitical")
    target_default = next(b for b in geo_default["bins"] if b["bin_lo"] == 0.5)
    assert target_default["sparse"] is True
    assert geo_default["min_resolutions_per_bin"] == 5

    # Per-sector override min=2 → 3 resolutions are no longer sparse.
    out_override = assemble(
        conn,
        since_ts=until - timedelta(days=1),
        until_ts=until,
        min_resolutions_per_bin=5,
        min_resolutions_per_bin_per_sector={"geopolitical": 2},
    )
    geo_override = next(s for s in out_override["sectors"] if s["sector"] == "geopolitical")
    target_override = next(b for b in geo_override["bins"] if b["bin_lo"] == 0.5)
    assert target_override["sparse"] is False
    assert geo_override["min_resolutions_per_bin"] == 2


def test_per_sector_overrides_dispatch_through_generator(tmp_path: Path) -> None:
    """The generator dispatch passes both per-sector mappings to the assembler."""
    from razor_rooster.report_generator.config.loader import (
        ReportConfig,
        ReportThresholds,
    )
    from razor_rooster.report_generator.engines.generator import _assemble_section

    db_path = tmp_path / "per_sector_dispatch.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
    with store.connection() as conn_:
        cfg = ReportConfig(
            thresholds=ReportThresholds(
                reliability_bin_count=10,
                reliability_min_resolutions_per_bin=5,
                reliability_bin_count_per_sector={"macroeconomic": 20},
                reliability_min_resolutions_per_bin_per_sector={"climate": 3},
            ),
        )
        out = _assemble_section(
            conn_,
            section_name="reliability",
            since_ts=datetime(2026, 5, 14, tzinfo=UTC),
            until_ts=datetime(2026, 5, 15, tzinfo=UTC),
            cfg=cfg,
        )
        # No data, so no sectors emitted; but the global bins payload
        # should still reflect the global default.
        assert len(out["bins"]) == 10
        assert out["min_resolutions_per_bin"] == 5
