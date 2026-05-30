"""Tests for per-cycle threshold-distribution measurements (T-RG-COMPAT-MEAS-001).

Covers:
- compute_distribution: empty input, single value, percentile correctness, threshold counting.
- cross_venue_spread_observations: extraction from cross_venue content dicts.
- Persistence: round-trip insert + upsert + query helpers.
- Generator integration: a full generate() cycle with cross_venue items
  records a measurement row.
- CLI: razor-rooster report measurements subcommand prints expected text.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.report_generator.cli import report as report_cli
from razor_rooster.report_generator.engines.measurements import (
    DEFAULT_PERCENTILES,
    compute_distribution,
    cross_venue_spread_observations,
)
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import (
    list_threshold_measurements,
    persist_threshold_measurement,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "measurements.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_report_generator_migrations(c)
    with store.connection() as c:
        yield c


# -- compute_distribution --------------------------------------------------


def test_empty_distribution_emits_zero_n_and_none_stats() -> None:
    dist = compute_distribution([], threshold=500.0)
    assert dist["n"] == 0
    assert dist["n_above_threshold"] == 0
    assert dist["configured_threshold"] == 500.0
    assert dist["min"] is None
    assert dist["max"] is None
    assert dist["mean"] is None
    assert dist["stddev"] is None
    # Percentile keys are present but values are None.
    for p in DEFAULT_PERCENTILES:
        key = f"{p:.2f}"
        assert key in dist["percentiles"]
        assert dist["percentiles"][key] is None


def test_single_value_distribution() -> None:
    dist = compute_distribution([700.0], threshold=500.0)
    assert dist["n"] == 1
    assert dist["n_above_threshold"] == 1
    assert dist["min"] == 700.0
    assert dist["max"] == 700.0
    assert dist["mean"] == 700.0
    assert dist["stddev"] == 0.0
    assert dist["percentiles"]["0.50"] == 700.0


def test_strict_greater_than_for_threshold_counting() -> None:
    """A value exactly at the threshold is NOT counted as above (strict >)."""
    dist = compute_distribution([500.0, 501.0, 250.0], threshold=500.0)
    assert dist["n"] == 3
    # 500.0 is not > 500.0; only 501.0 counts.
    assert dist["n_above_threshold"] == 1


def test_percentile_correctness_simple_series() -> None:
    """Median of [10, 20, 30, 40, 50] is 30; 90th is 46."""
    dist = compute_distribution([10.0, 20.0, 30.0, 40.0, 50.0], threshold=0.0)
    pct = dist["percentiles"]
    assert pct["0.50"] == 30.0
    # Linear interpolation: rank = 0.9 * 4 = 3.6 → between values[3]=40 and values[4]=50
    # → 40 + 0.6 * (50 - 40) = 46.
    assert pct["0.90"] == pytest.approx(46.0)
    # 10th percentile: rank = 0.1 * 4 = 0.4 → 10 + 0.4 * (20 - 10) = 14.
    assert pct["0.10"] == pytest.approx(14.0)


def test_distribution_handles_unsorted_input() -> None:
    """Input order must not affect the result."""
    a = compute_distribution([300.0, 100.0, 500.0, 200.0, 400.0], threshold=250.0)
    b = compute_distribution([100.0, 200.0, 300.0, 400.0, 500.0], threshold=250.0)
    assert a == b


def test_distribution_mean_and_stddev() -> None:
    dist = compute_distribution([10.0, 20.0, 30.0], threshold=0.0)
    assert dist["mean"] == pytest.approx(20.0)
    # Population stddev: sqrt(((10-20)^2 + 0 + (30-20)^2) / 3) = sqrt(200/3) ≈ 8.16
    assert dist["stddev"] == pytest.approx(8.165, rel=1e-3)


# -- cross_venue_spread_observations ---------------------------------------


def test_extracts_spreads_from_cross_venue_content() -> None:
    content = {
        "type": "cross_venue",
        "items": [
            {"class_id": "a", "spread_bps": 700},
            {"class_id": "b", "spread_bps": 1200},
            {"class_id": "c", "spread_bps": 300},
        ],
    }
    out = cross_venue_spread_observations(content)
    assert out == [700.0, 1200.0, 300.0]


def test_handles_empty_items_gracefully() -> None:
    assert cross_venue_spread_observations({"type": "cross_venue", "items": []}) == []
    assert cross_venue_spread_observations({"type": "cross_venue"}) == []


def test_skips_malformed_items() -> None:
    content = {
        "type": "cross_venue",
        "items": [
            {"spread_bps": 500},
            {"spread_bps": None},  # null spread → skipped
            {"spread_bps": "not a number"},  # non-coercible → skipped
            "a string instead of a dict",  # wrong shape → skipped
            {"spread_bps": 1500},
        ],
    }
    out = cross_venue_spread_observations(content)
    assert out == [500.0, 1500.0]


def test_non_mapping_content_returns_empty() -> None:
    assert cross_venue_spread_observations(None) == []  # type: ignore[arg-type]
    assert cross_venue_spread_observations([]) == []  # type: ignore[arg-type]


# -- persistence: round-trip + upsert --------------------------------------


def test_persist_and_query_round_trip(conn: duckdb.DuckDBPyConnection) -> None:
    measured_at = datetime(2026, 5, 15, 12, tzinfo=UTC)
    distribution = compute_distribution([300.0, 700.0, 1100.0], threshold=500.0)
    persist_threshold_measurement(
        conn,
        report_id="r1",
        measurement_kind="cross_venue_spread_bps",
        measured_at=measured_at,
        distribution=distribution,
    )
    out = list_threshold_measurements(conn)
    assert len(out) == 1
    record = out[0]
    assert record.report_id == "r1"
    assert record.measurement_kind == "cross_venue_spread_bps"
    assert record.measured_at == measured_at
    assert record.n_observations == 3
    assert record.n_above_threshold == 2
    assert record.configured_threshold == 500.0
    assert record.distribution["percentiles"]["0.50"] == 700.0


def test_upsert_replaces_existing_row(conn: duckdb.DuckDBPyConnection) -> None:
    measured_at_1 = datetime(2026, 5, 15, 12, tzinfo=UTC)
    measured_at_2 = datetime(2026, 5, 15, 14, tzinfo=UTC)
    persist_threshold_measurement(
        conn,
        report_id="r1",
        measurement_kind="cross_venue_spread_bps",
        measured_at=measured_at_1,
        distribution=compute_distribution([100.0], threshold=500.0),
    )
    persist_threshold_measurement(
        conn,
        report_id="r1",
        measurement_kind="cross_venue_spread_bps",
        measured_at=measured_at_2,
        distribution=compute_distribution([100.0, 600.0], threshold=500.0),
    )
    rows = list_threshold_measurements(conn)
    assert len(rows) == 1
    assert rows[0].measured_at == measured_at_2
    assert rows[0].n_observations == 2
    assert rows[0].n_above_threshold == 1


def test_filter_by_measurement_kind(conn: duckdb.DuckDBPyConnection) -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    persist_threshold_measurement(
        conn,
        report_id="r1",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base,
        distribution=compute_distribution([500.0], threshold=500.0),
    )
    persist_threshold_measurement(
        conn,
        report_id="r1",
        measurement_kind="hypothetical_other",
        measured_at=base,
        distribution=compute_distribution([1.0], threshold=0.5),
    )
    cv_rows = list_threshold_measurements(conn, measurement_kind="cross_venue_spread_bps")
    assert len(cv_rows) == 1
    assert cv_rows[0].measurement_kind == "cross_venue_spread_bps"


def test_filter_by_since(conn: duckdb.DuckDBPyConnection) -> None:
    old = datetime(2026, 5, 1, 12, tzinfo=UTC)
    new = datetime(2026, 5, 15, 12, tzinfo=UTC)
    persist_threshold_measurement(
        conn,
        report_id="r-old",
        measurement_kind="cross_venue_spread_bps",
        measured_at=old,
        distribution=compute_distribution([100.0], threshold=500.0),
    )
    persist_threshold_measurement(
        conn,
        report_id="r-new",
        measurement_kind="cross_venue_spread_bps",
        measured_at=new,
        distribution=compute_distribution([700.0], threshold=500.0),
    )
    cutoff = datetime(2026, 5, 10, tzinfo=UTC)
    rows = list_threshold_measurements(conn, since=cutoff)
    assert len(rows) == 1
    assert rows[0].report_id == "r-new"


def test_results_ordered_newest_first(conn: duckdb.DuckDBPyConnection) -> None:
    base = datetime(2026, 5, 15, 12, tzinfo=UTC)
    for i, hours in enumerate([0, 1, 2]):
        persist_threshold_measurement(
            conn,
            report_id=f"r{i}",
            measurement_kind="cross_venue_spread_bps",
            measured_at=base + timedelta(hours=hours),
            distribution=compute_distribution([float(i * 100)], threshold=500.0),
        )
    rows = list_threshold_measurements(conn)
    assert [r.report_id for r in rows] == ["r2", "r1", "r0"]


# -- generator integration -------------------------------------------------


def test_generator_persists_measurement_when_cross_venue_renders(
    tmp_path: Path,
) -> None:
    """A full generate() with cross_venue items records the spread distribution."""
    from razor_rooster.data_ingest.persistence.migrations import (
        run_pending_migrations as run_pending_data_ingest_migrations,
    )
    from razor_rooster.mispricing_detector.persistence.migrations import (
        run_pending_mispricing_migrations,
    )
    from razor_rooster.pattern_library.persistence.migrations import (
        run_pending_pattern_library_migrations,
    )
    from razor_rooster.report_generator.config.loader import (
        ReportConfig,
        ReportThresholds,
    )
    from razor_rooster.report_generator.engines.generator import generate
    from razor_rooster.signal_scanner.persistence.migrations import (
        run_pending_signal_scanner_migrations,
    )

    db_path = tmp_path / "gen.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_report_generator_migrations(c)
        # Seed two venue prices for one class so cross_venue produces an item.
        c.execute(
            "INSERT INTO pl_event_classes (class_id, title, description, "
            "domain_sector, definition_version, outcome_type, registered_at) VALUES "
            "(?, ?, ?, ?, 1, 'binary', ?)",
            ["cls1", "cls1 title", "cls1 desc", "macroeconomic", datetime(2026, 1, 1, tzinfo=UTC)],
        )
        c.execute(
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
        cmp_cols = (
            "comparison_id, cycle_id, mapping_id, class_id, condition_id, "
            "outcome_token_id, polarity, scan_id, model_probability, "
            "model_ci_lower, model_ci_upper, market_probability, "
            "market_best_bid, market_best_ask, market_last_trade_price, "
            "market_volume_24h, market_spread_bps, market_snapshot_ts, "
            "delta, log_odds_delta, ci_overlap, expected_value, "
            "confidence_weighted_score, surfaced, suppression_reasons, "
            "low_signature_confidence, source_stale_warning, "
            "library_stale_warning, definition_drift_warning, "
            "stale_market_price, no_market_price, degenerate_orderbook, "
            "low_liquidity, low_mapping_confidence, computed_at, venue"
        )
        cmp_placeholders = ", ".join(["?"] * len(cmp_cols.split(",")))
        for cmp_id, venue, market_p in [("a", "polymarket", 0.30), ("b", "kalshi", 0.45)]:
            c.execute(
                f"INSERT INTO comparisons ({cmp_cols}) VALUES ({cmp_placeholders})",
                [
                    cmp_id,
                    "cy-1",
                    f"map-{cmp_id}",
                    "cls1",
                    f"cond-{cmp_id}",
                    f"cond-{cmp_id}-yes",
                    "aligned",
                    "scan-1",
                    0.50,
                    0.40,
                    0.60,
                    market_p,
                    market_p - 0.005,
                    market_p + 0.005,
                    market_p,
                    10_000.0,
                    50,
                    datetime(2026, 5, 14, 12, tzinfo=UTC),
                    0.0,
                    0.0,
                    False,
                    0.0,
                    0.5,
                    False,
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
                    datetime(2026, 5, 14, 14, tzinfo=UTC),
                    venue,
                ],
            )
    cfg = ReportConfig(
        enabled_sections=("cross_venue",),
        thresholds=ReportThresholds(cross_venue_spread_bps=500),
    )
    result = generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        config=cfg,
        quiet=True,
        now=datetime(2026, 5, 15, 14, tzinfo=UTC),
    )
    # The cross_venue section ran; its item has a 1500-bps spread → above threshold.
    with store.connection() as c:
        rows = list_threshold_measurements(c, measurement_kind="cross_venue_spread_bps")
    assert len(rows) == 1
    assert rows[0].report_id == result.report_id
    assert rows[0].measurement_kind == "cross_venue_spread_bps"
    assert rows[0].n_observations == 1
    assert rows[0].n_above_threshold == 1
    assert rows[0].configured_threshold == 500.0


def test_generator_persists_zero_count_when_cross_venue_empty(tmp_path: Path) -> None:
    """When the cross_venue section runs but produces no items, the measurement is n=0."""
    from razor_rooster.data_ingest.persistence.migrations import (
        run_pending_migrations as run_pending_data_ingest_migrations,
    )
    from razor_rooster.mispricing_detector.persistence.migrations import (
        run_pending_mispricing_migrations,
    )
    from razor_rooster.pattern_library.persistence.migrations import (
        run_pending_pattern_library_migrations,
    )
    from razor_rooster.report_generator.engines.generator import generate
    from razor_rooster.signal_scanner.persistence.migrations import (
        run_pending_signal_scanner_migrations,
    )

    db_path = tmp_path / "empty.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_report_generator_migrations(c)
    generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        quiet=True,
        now=datetime(2026, 5, 15, 14, tzinfo=UTC),
    )
    with store.connection() as c:
        rows = list_threshold_measurements(c, measurement_kind="cross_venue_spread_bps")
    assert len(rows) == 1
    assert rows[0].n_observations == 0
    assert rows[0].n_above_threshold == 0


# -- CLI: razor-rooster report measurements --------------------------------


def test_cli_measurements_lists_recorded_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r-cli",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([200.0, 600.0, 1100.0], threshold=500.0),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["measurements", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "r-cli" in result.output
    assert "cross_venue_spread_bps" in result.output
    assert "n=3" in result.output
    assert "above_threshold=2" in result.output


def test_cli_measurements_json_flag_emits_parsable_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_json.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r-cli",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([700.0], threshold=500.0),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["measurements", "--db", str(db_path), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output.strip())
    assert payload["report_id"] == "r-cli"
    assert payload["distribution"]["n"] == 1


def test_cli_measurements_empty_message(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_empty.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["measurements", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "No 'cross_venue_spread_bps' measurements yet." in result.output


# -- single_venue_dominance_observations (v0.41.0) -------------------------


def test_dominance_extraction_single_class() -> None:
    from razor_rooster.report_generator.engines.measurements import (
        single_venue_dominance_observations,
    )

    content = {
        "type": "surfaced",
        "comparisons": [
            {
                "class_id": "cls1",
                "venue_shares": {"polymarket": 0.85, "kalshi": 0.15},
            },
        ],
    }
    out = single_venue_dominance_observations(content)
    assert out == [0.85]


def test_dominance_extraction_skips_single_venue_classes() -> None:
    from razor_rooster.report_generator.engines.measurements import (
        single_venue_dominance_observations,
    )

    content = {
        "type": "surfaced",
        "comparisons": [
            # Single-venue class — no dominance is meaningful.
            {"class_id": "cls1", "venue_shares": {"polymarket": 1.0}},
            # Multi-venue class — should be included.
            {
                "class_id": "cls2",
                "venue_shares": {"polymarket": 0.6, "kalshi": 0.4},
            },
            # No venue_shares → skipped.
            {"class_id": "cls3"},
        ],
    }
    out = single_venue_dominance_observations(content)
    assert out == [0.6]


def test_dominance_extraction_dedups_per_class() -> None:
    """Two surfaced comparisons for the same class produce one observation (max)."""
    from razor_rooster.report_generator.engines.measurements import (
        single_venue_dominance_observations,
    )

    content = {
        "type": "surfaced",
        "comparisons": [
            {
                "class_id": "cls1",
                "venue_shares": {"polymarket": 0.70, "kalshi": 0.30},
            },
            {
                "class_id": "cls1",
                "venue_shares": {"polymarket": 0.85, "kalshi": 0.15},
            },
        ],
    }
    out = single_venue_dominance_observations(content)
    # Same class twice; we keep the higher share.
    assert out == [0.85]


def test_dominance_extraction_handles_malformed_input() -> None:
    from razor_rooster.report_generator.engines.measurements import (
        single_venue_dominance_observations,
    )

    assert single_venue_dominance_observations({}) == []
    assert single_venue_dominance_observations({"comparisons": []}) == []
    assert single_venue_dominance_observations({"comparisons": "not a list"}) == []
    # Non-coercible share value → skipped.
    content = {
        "comparisons": [
            {"class_id": "cls1", "venue_shares": {"polymarket": "bad"}},
        ],
    }
    assert single_venue_dominance_observations(content) == []


# -- brier_per_sector_observations (v0.41.0) -------------------------------


def test_brier_extraction_one_per_sector() -> None:
    from razor_rooster.report_generator.engines.measurements import (
        brier_per_sector_observations,
    )

    content = {
        "type": "calibration",
        "sector_brier_scores": [
            {"sector": "macroeconomic", "brier_score": 0.18},
            {"sector": "geopolitical", "brier_score": 0.32},
            {"sector": "climate", "brier_score": 0.12},
        ],
    }
    out = brier_per_sector_observations(content)
    assert sorted(out) == [0.12, 0.18, 0.32]


def test_brier_extraction_handles_missing_or_malformed() -> None:
    from razor_rooster.report_generator.engines.measurements import (
        brier_per_sector_observations,
    )

    assert brier_per_sector_observations({}) == []
    assert brier_per_sector_observations({"sector_brier_scores": []}) == []
    assert brier_per_sector_observations({"sector_brier_scores": "not a list"}) == []
    # Mixed entries — one valid, others skipped.
    content = {
        "sector_brier_scores": [
            {"sector": "macroeconomic"},  # missing brier_score → skipped
            {"sector": "regulatory", "brier_score": 0.20},
            "not a dict",  # wrong shape → skipped
        ],
    }
    assert brier_per_sector_observations(content) == [0.20]


# -- generator integration: all three kinds persisted on a real cycle -------


def test_generator_persists_all_three_kinds(tmp_path: Path) -> None:
    """A full cycle records cross_venue, dominance, and brier rows."""
    from razor_rooster.data_ingest.persistence.migrations import (
        run_pending_migrations as run_pending_data_ingest_migrations,
    )
    from razor_rooster.mispricing_detector.persistence.migrations import (
        run_pending_mispricing_migrations,
    )
    from razor_rooster.pattern_library.persistence.migrations import (
        run_pending_pattern_library_migrations,
    )
    from razor_rooster.report_generator.config.loader import (
        ReportConfig,
        ReportThresholds,
    )
    from razor_rooster.report_generator.engines.generator import generate
    from razor_rooster.report_generator.engines.measurements import (
        SHIPPED_MEASUREMENT_KINDS,
    )
    from razor_rooster.signal_scanner.persistence.migrations import (
        run_pending_signal_scanner_migrations,
    )

    db_path = tmp_path / "all_kinds.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_data_ingest_migrations(c)
        run_pending_pattern_library_migrations(c)
        run_pending_signal_scanner_migrations(c)
        run_pending_mispricing_migrations(c)
        run_pending_report_generator_migrations(c)
    cfg = ReportConfig(
        enabled_sections=("surfaced", "cross_venue", "calibration"),
        thresholds=ReportThresholds(),
    )
    generate(
        store,
        since=datetime(2026, 5, 14, tzinfo=UTC),
        config=cfg,
        quiet=True,
        now=datetime(2026, 5, 15, 14, tzinfo=UTC),
    )
    with store.connection() as c:
        rows = list_threshold_measurements(c)
    kinds_recorded = {row.measurement_kind for row in rows}
    # All three shipped kinds should have been recorded, even on
    # an empty corpus (each emits an n=0 measurement row).
    assert kinds_recorded == set(SHIPPED_MEASUREMENT_KINDS)
    for row in rows:
        if row.measurement_kind == "single_venue_dominance_share":
            assert row.configured_threshold == 0.80
        elif row.measurement_kind == "brier_per_sector":
            assert row.configured_threshold == 0.25
        elif row.measurement_kind == "cross_venue_spread_bps":
            assert row.configured_threshold == 500.0


# -- CLI inspection across kinds -------------------------------------------


def test_cli_supports_kind_filter_for_new_kinds(tmp_path: Path) -> None:
    """The --kind flag works for single_venue_dominance_share."""
    db_path = tmp_path / "cli_dominance.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r-dom",
                measurement_kind="single_venue_dominance_share",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([0.55, 0.85, 0.92], threshold=0.80),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "measurements",
            "--db",
            str(db_path),
            "--kind",
            "single_venue_dominance_share",
        ],
    )
    assert result.exit_code == 0
    assert "r-dom" in result.output
    assert "single_venue_dominance_share" in result.output
    # Two of three values are above 0.80 (strict >).
    assert "above_threshold=2" in result.output


def test_cli_lists_brier_per_sector_measurements(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_brier.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r-brier",
                measurement_kind="brier_per_sector",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([0.18, 0.22, 0.31], threshold=0.25),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "measurements",
            "--db",
            str(db_path),
            "--kind",
            "brier_per_sector",
        ],
    )
    assert result.exit_code == 0
    assert "brier_per_sector" in result.output
    # 0.31 > 0.25 → 1 of 3 is above.
    assert "above_threshold=1" in result.output


# -- threshold_percentile_rank (v0.41.0) -----------------------------------


def test_percentile_rank_threshold_at_median() -> None:
    from razor_rooster.report_generator.engines.measurements import (
        threshold_percentile_rank,
    )

    distribution = compute_distribution([10.0, 20.0, 30.0, 40.0, 50.0], threshold=30.0)
    rank = threshold_percentile_rank(distribution)
    # threshold == p50 == 30.0 → rank should be 0.50.
    assert rank == 0.50


def test_percentile_rank_threshold_below_bottom_returns_zero() -> None:
    from razor_rooster.report_generator.engines.measurements import (
        threshold_percentile_rank,
    )

    distribution = compute_distribution([100.0, 200.0, 300.0], threshold=50.0)
    rank = threshold_percentile_rank(distribution)
    assert rank == 0.0


def test_percentile_rank_threshold_above_top_returns_one() -> None:
    from razor_rooster.report_generator.engines.measurements import (
        threshold_percentile_rank,
    )

    distribution = compute_distribution([10.0, 20.0, 30.0], threshold=100.0)
    rank = threshold_percentile_rank(distribution)
    assert rank == 1.0


def test_percentile_rank_uses_configured_threshold_by_default() -> None:
    """Omitting the threshold arg falls back to configured_threshold."""
    from razor_rooster.report_generator.engines.measurements import (
        threshold_percentile_rank,
    )

    distribution = compute_distribution([10.0, 20.0, 30.0, 40.0], threshold=25.0)
    # configured_threshold is recorded as 25.0 in the payload.
    rank = threshold_percentile_rank(distribution)
    # 25.0 is between p50 (25.0 due to interpolation) and p75 (32.5).
    # The function returns the highest q whose percentile value is <= 25.0.
    assert rank is not None
    assert 0.40 <= rank <= 0.75


def test_percentile_rank_returns_none_for_empty_distribution() -> None:
    from razor_rooster.report_generator.engines.measurements import (
        threshold_percentile_rank,
    )

    distribution = compute_distribution([], threshold=500.0)
    assert threshold_percentile_rank(distribution) is None


def test_percentile_rank_returns_none_for_missing_percentiles() -> None:
    from razor_rooster.report_generator.engines.measurements import (
        threshold_percentile_rank,
    )

    distribution = {"n": 5, "configured_threshold": 1.0, "percentiles": None}
    assert threshold_percentile_rank(distribution) is None


def test_percentile_rank_explicit_threshold_arg() -> None:
    from razor_rooster.report_generator.engines.measurements import (
        threshold_percentile_rank,
    )

    distribution = compute_distribution([10.0, 20.0, 30.0, 40.0, 50.0], threshold=0.0)
    # Explicit override of the threshold argument.
    rank = threshold_percentile_rank(distribution, threshold=40.0)
    assert rank == 0.75


# -- CLI: explain-thresholds (v0.41.0) ------------------------------------


def test_cli_explain_thresholds_default_lists_all_kinds(tmp_path: Path) -> None:
    """Default invocation prints a section per shipped kind."""
    db_path = tmp_path / "explain_default.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r-cv",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([200.0, 600.0, 1100.0], threshold=500.0),
            )
            persist_threshold_measurement(
                c,
                report_id="r-dom",
                measurement_kind="single_venue_dominance_share",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([0.55, 0.85, 0.92], threshold=0.80),
            )
            persist_threshold_measurement(
                c,
                report_id="r-brier",
                measurement_kind="brier_per_sector",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([0.18, 0.22, 0.31], threshold=0.25),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["explain-thresholds", "--db", str(db_path)])
    assert result.exit_code == 0
    # All three shipped kinds appear.
    assert "cross_venue_spread_bps" in result.output
    assert "single_venue_dominance_share" in result.output
    assert "brier_per_sector" in result.output
    # Percentile-rank language.
    assert "percentile rank" in result.output
    assert "configured threshold" in result.output


def test_cli_explain_thresholds_per_kind_filter(tmp_path: Path) -> None:
    """--kind narrows the output to a single kind."""
    db_path = tmp_path / "explain_kind.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r-cv",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([200.0, 600.0, 1100.0], threshold=500.0),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "explain-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
        ],
    )
    assert result.exit_code == 0
    assert "cross_venue_spread_bps" in result.output
    assert "single_venue_dominance_share" not in result.output


def test_cli_explain_thresholds_handles_no_data(tmp_path: Path) -> None:
    """When a kind has no measurements yet, the output is descriptive, not an error."""
    db_path = tmp_path / "explain_empty.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["explain-thresholds", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "no 'cross_venue_spread_bps' measurements yet" in result.output


def test_cli_explain_thresholds_uses_descriptive_language_only(tmp_path: Path) -> None:
    """The explain output must not contain forbidden imperative phrases."""
    from razor_rooster.position_engine.frame.linter import check_text

    db_path = tmp_path / "explain_lint.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r-cv",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([200.0, 600.0, 1100.0], threshold=500.0),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["explain-thresholds", "--db", str(db_path)])
    assert result.exit_code == 0
    # The shared imperative-language linter must not raise on this output.
    check_text(result.output)
