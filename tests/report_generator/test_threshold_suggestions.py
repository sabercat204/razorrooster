"""Tests for the threshold-suggestion engine (T-RG-COMPAT-SUGG-001 v0.41.0).

Covers:
- suggest_thresholds: empty input, single-cycle, multi-cycle averaging,
  target-percentile interpolation, target-out-of-range clamping.
- CLI: razor-rooster report suggest-thresholds prints expected output,
  honors --kind / --lookback-cycles / --target-pct flags, and never
  contains forbidden imperative phrases.
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
from razor_rooster.report_generator.engines.suggestions import (
    DEFAULT_LOOKBACK_CYCLES,
    DEFAULT_TARGET_PERCENTILES,
    SuggestedThreshold,
    ThresholdSuggestionReport,
    suggest_thresholds,
)
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import (
    persist_threshold_measurement,
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = tmp_path / "suggestions.duckdb"
    store = DuckDBStore(db_path)
    with store.connection() as c:
        run_pending_report_generator_migrations(c)
    with store.connection() as c:
        yield c


def _seed_cycle(
    conn: duckdb.DuckDBPyConnection,
    *,
    report_id: str,
    measurement_kind: str,
    measured_at: datetime,
    values: list[float],
    threshold: float,
) -> None:
    persist_threshold_measurement(
        conn,
        report_id=report_id,
        measurement_kind=measurement_kind,
        measured_at=measured_at,
        distribution=compute_distribution(values, threshold=threshold),
    )


# -- defaults --------------------------------------------------------------


def test_defaults_match_documented_constants() -> None:
    assert DEFAULT_LOOKBACK_CYCLES == 30
    assert DEFAULT_TARGET_PERCENTILES == (0.50, 0.70, 0.90)


def test_empty_table_returns_empty_report(conn: duckdb.DuckDBPyConnection) -> None:
    report_obj = suggest_thresholds(
        conn,
        measurement_kind="cross_venue_spread_bps",
    )
    assert isinstance(report_obj, ThresholdSuggestionReport)
    assert report_obj.cycles_inspected == 0
    assert report_obj.cycles_with_data == 0
    assert report_obj.current_threshold is None
    assert report_obj.suggestions == ()


def test_zero_observations_only_returns_empty_suggestions(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Cycles with n=0 don't contribute to averages; suggestions are empty."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    for i in range(3):
        _seed_cycle(
            conn,
            report_id=f"r{i}",
            measurement_kind="cross_venue_spread_bps",
            measured_at=base + timedelta(hours=i),
            values=[],  # zero observations
            threshold=500.0,
        )
    report_obj = suggest_thresholds(conn, measurement_kind="cross_venue_spread_bps")
    assert report_obj.cycles_inspected == 3
    assert report_obj.cycles_with_data == 0
    assert report_obj.suggestions == ()


# -- single-cycle behavior -------------------------------------------------


def test_single_cycle_uses_recorded_percentiles(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """One cycle's percentiles round-trip into suggestions."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    _seed_cycle(
        conn,
        report_id="r1",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base,
        values=[100.0, 200.0, 300.0, 400.0, 500.0],
        threshold=500.0,
    )
    report_obj = suggest_thresholds(conn, measurement_kind="cross_venue_spread_bps")
    assert report_obj.cycles_inspected == 1
    assert report_obj.cycles_with_data == 1
    assert report_obj.current_threshold == 500.0
    targets = {s.target_percentile for s in report_obj.suggestions}
    assert targets == set(DEFAULT_TARGET_PERCENTILES)
    # p50 of [100,200,300,400,500] = 300; p90 = 460.
    p50 = next(s for s in report_obj.suggestions if s.target_percentile == 0.50)
    p90 = next(s for s in report_obj.suggestions if s.target_percentile == 0.90)
    assert p50.suggested_value == 300.0
    assert p90.suggested_value == pytest.approx(460.0)


# -- multi-cycle averaging -------------------------------------------------


def test_multi_cycle_averages_percentile_cuts(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Two cycles' percentiles average per cut."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    _seed_cycle(
        conn,
        report_id="r1",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base,
        values=[100.0, 200.0, 300.0, 400.0, 500.0],
        threshold=500.0,
    )
    _seed_cycle(
        conn,
        report_id="r2",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base + timedelta(hours=1),
        values=[200.0, 300.0, 400.0, 500.0, 600.0],
        threshold=500.0,
    )
    report_obj = suggest_thresholds(conn, measurement_kind="cross_venue_spread_bps")
    assert report_obj.cycles_inspected == 2
    assert report_obj.cycles_with_data == 2
    p50 = next(s for s in report_obj.suggestions if s.target_percentile == 0.50)
    # Cycle 1's p50 = 300; cycle 2's p50 = 400; average = 350.
    assert p50.suggested_value == pytest.approx(350.0)


# -- target percentile interpolation ---------------------------------------


def test_custom_target_percentiles_emit_correct_count(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    base = datetime(2026, 5, 15, tzinfo=UTC)
    _seed_cycle(
        conn,
        report_id="r1",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base,
        values=[100.0, 200.0, 300.0, 400.0, 500.0],
        threshold=500.0,
    )
    report_obj = suggest_thresholds(
        conn,
        measurement_kind="cross_venue_spread_bps",
        target_percentiles=(0.25, 0.60, 0.95),
    )
    targets = sorted(s.target_percentile for s in report_obj.suggestions)
    assert targets == [0.25, 0.60, 0.95]


def test_target_above_recorded_clamps_to_top(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Target == 1.0 clamps to the highest recorded percentile value."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    _seed_cycle(
        conn,
        report_id="r1",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base,
        values=[100.0, 200.0, 300.0, 400.0, 500.0],
        threshold=500.0,
    )
    report_obj = suggest_thresholds(
        conn,
        measurement_kind="cross_venue_spread_bps",
        target_percentiles=(1.0,),
    )
    s = report_obj.suggestions[0]
    # The highest recorded percentile is p99; for [100..500] the
    # interpolated p99 = 100 + 0.99 * 4 * 100 = 496.
    assert s.suggested_value == pytest.approx(496.0)


def test_target_below_recorded_clamps_to_bottom(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    base = datetime(2026, 5, 15, tzinfo=UTC)
    _seed_cycle(
        conn,
        report_id="r1",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base,
        values=[100.0, 200.0, 300.0, 400.0, 500.0],
        threshold=500.0,
    )
    report_obj = suggest_thresholds(
        conn,
        measurement_kind="cross_venue_spread_bps",
        target_percentiles=(0.0,),
    )
    s = report_obj.suggestions[0]
    # The lowest recorded percentile is p10 = 140.
    assert s.suggested_value == pytest.approx(140.0)


# -- lookback windowing ----------------------------------------------------


def test_lookback_window_limits_rows_inspected(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """--lookback-cycles N reads only the N most recent rows."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    # Seed 5 cycles. The two oldest have a much higher distribution
    # than the three newest. With lookback=3, only the newest three
    # contribute to the averages.
    for i, factor in enumerate([10.0, 10.0, 1.0, 1.0, 1.0]):
        _seed_cycle(
            conn,
            report_id=f"r{i}",
            measurement_kind="cross_venue_spread_bps",
            measured_at=base + timedelta(hours=i),
            values=[100.0 * factor, 200.0 * factor, 300.0 * factor],
            threshold=500.0,
        )
    full = suggest_thresholds(
        conn,
        measurement_kind="cross_venue_spread_bps",
        lookback_cycles=5,
    )
    short = suggest_thresholds(
        conn,
        measurement_kind="cross_venue_spread_bps",
        lookback_cycles=3,
    )
    # The short window should have a smaller p50 than the full window
    # because it excludes the two old big-distribution cycles.
    full_p50 = next(s for s in full.suggestions if s.target_percentile == 0.50)
    short_p50 = next(s for s in short.suggestions if s.target_percentile == 0.50)
    assert short_p50.suggested_value < full_p50.suggested_value


# -- mixed empty-and-data cycles -------------------------------------------


def test_mixed_empty_and_data_cycles(conn: duckdb.DuckDBPyConnection) -> None:
    """Empty cycles are inspected but not counted in cycles_with_data."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    # Two empty + one with data.
    _seed_cycle(
        conn,
        report_id="r-empty-1",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base,
        values=[],
        threshold=500.0,
    )
    _seed_cycle(
        conn,
        report_id="r-data",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base + timedelta(hours=1),
        values=[100.0, 200.0, 300.0],
        threshold=500.0,
    )
    _seed_cycle(
        conn,
        report_id="r-empty-2",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base + timedelta(hours=2),
        values=[],
        threshold=500.0,
    )
    report_obj = suggest_thresholds(conn, measurement_kind="cross_venue_spread_bps")
    assert report_obj.cycles_inspected == 3
    assert report_obj.cycles_with_data == 1


# -- CLI tests -------------------------------------------------------------


def test_cli_suggest_thresholds_default_lists_all_kinds(tmp_path: Path) -> None:
    """Default invocation prints sections for every shipped kind."""
    db_path = tmp_path / "cli_suggest.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([200.0, 600.0, 1100.0], threshold=500.0),
            )
            persist_threshold_measurement(
                c,
                report_id="r2",
                measurement_kind="single_venue_dominance_share",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([0.55, 0.85, 0.92], threshold=0.80),
            )
            persist_threshold_measurement(
                c,
                report_id="r3",
                measurement_kind="brier_per_sector",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([0.18, 0.22, 0.31], threshold=0.25),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["suggest-thresholds", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "cross_venue_spread_bps" in result.output
    assert "single_venue_dominance_share" in result.output
    assert "brier_per_sector" in result.output
    # Default percentile targets should appear.
    assert "p50:" in result.output
    assert "p70:" in result.output
    assert "p90:" in result.output


def test_cli_suggest_thresholds_kind_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_suggest_kind.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
                measurement_kind="brier_per_sector",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([0.10, 0.20, 0.30, 0.40], threshold=0.25),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "brier_per_sector",
        ],
    )
    assert result.exit_code == 0
    assert "brier_per_sector" in result.output
    assert "cross_venue_spread_bps" not in result.output


def test_cli_suggest_thresholds_custom_targets(tmp_path: Path) -> None:
    """--target-pct repeated values produce one suggestion line each."""
    db_path = tmp_path / "cli_suggest_targets.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
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
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--target-pct",
            "0.40",
            "--target-pct",
            "0.85",
        ],
    )
    assert result.exit_code == 0
    assert "p40:" in result.output
    assert "p85:" in result.output
    # Default targets should NOT appear when custom ones are passed.
    assert "p50:" not in result.output


def test_cli_suggest_thresholds_invalid_target_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_suggest_bad.duckdb"
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
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--target-pct",
            "1.5",
        ],
    )
    assert result.exit_code != 0


def test_cli_suggest_thresholds_no_data_message(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_suggest_empty.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["suggest-thresholds", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "not enough data to suggest thresholds yet" in result.output


def test_cli_suggest_thresholds_uses_descriptive_language_only(tmp_path: Path) -> None:
    """The output never trips the imperative-language linter."""
    from razor_rooster.position_engine.frame.linter import check_text

    db_path = tmp_path / "cli_suggest_lint.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 12, tzinfo=UTC),
                distribution=compute_distribution([200.0, 600.0, 1100.0], threshold=500.0),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(report_cli, ["suggest-thresholds", "--db", str(db_path)])
    assert result.exit_code == 0
    check_text(result.output)


def test_suggested_threshold_dataclass_is_frozen() -> None:
    s = SuggestedThreshold(
        target_percentile=0.50,
        suggested_value=100.0,
        cycles=5,
        cycles_with_data=5,
    )
    with pytest.raises(AttributeError):
        s.target_percentile = 0.99  # type: ignore[misc]


# -- write path: apply_threshold_suggestion (v0.42.0) ----------------------


def _seed_config_yaml(path: Path, payload: dict[str, object]) -> Path:
    import yaml as _yaml

    cfg_path = path / "report.yaml"
    cfg_path.write_text(_yaml.safe_dump(payload), encoding="utf-8")
    return cfg_path


def test_apply_writes_value_and_creates_backup(tmp_path: Path) -> None:
    """Successful apply updates the YAML and writes a timestamped backup."""
    import yaml as _yaml

    from razor_rooster.report_generator.engines.suggestions import (
        apply_threshold_suggestion,
    )

    cfg_path = _seed_config_yaml(
        tmp_path,
        {
            "enabled_sections": ["surfaced", "cross_venue"],
            "thresholds": {"cross_venue_spread_bps": 500},
        },
    )
    fixed_now = datetime(2026, 5, 16, 14, 30, 0, tzinfo=UTC)
    result = apply_threshold_suggestion(
        config_path=cfg_path,
        measurement_kind="cross_venue_spread_bps",
        new_value=750.0,
        target_percentile=0.70,
        now=fixed_now,
    )
    assert result.knob == "cross_venue_spread_bps"
    assert result.previous_value == 500.0
    assert result.new_value == 750.0
    assert result.backup_path.exists()
    assert "20260516T143000" in result.backup_path.name
    assert result.backup_path.name.endswith("Z")

    # YAML round-trip: new value lands in the right knob.
    written = _yaml.safe_load(cfg_path.read_text())
    assert written["thresholds"]["cross_venue_spread_bps"] == 750
    # Other top-level keys preserved.
    assert written["enabled_sections"] == ["surfaced", "cross_venue"]


def test_apply_coerces_integer_valued_knob_to_int(tmp_path: Path) -> None:
    """cross_venue_spread_bps is integer-typed → suggested float gets rounded."""
    import yaml as _yaml

    from razor_rooster.report_generator.engines.suggestions import (
        apply_threshold_suggestion,
    )

    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    apply_threshold_suggestion(
        config_path=cfg_path,
        measurement_kind="cross_venue_spread_bps",
        new_value=746.7,
    )
    written = _yaml.safe_load(cfg_path.read_text())
    assert written["thresholds"]["cross_venue_spread_bps"] == 747
    # Type is int, not float (YAML serialization preserves it).
    raw_text = cfg_path.read_text()
    # Sanity: the integer doesn't have a decimal point in the rendered YAML.
    assert "cross_venue_spread_bps: 747" in raw_text


def test_apply_writes_float_for_dominance_pct(tmp_path: Path) -> None:
    import yaml as _yaml

    from razor_rooster.report_generator.engines.suggestions import (
        apply_threshold_suggestion,
    )

    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"single_venue_dominance_pct": 0.80}},
    )
    apply_threshold_suggestion(
        config_path=cfg_path,
        measurement_kind="single_venue_dominance_share",
        new_value=0.65,
        target_percentile=0.50,
    )
    written = _yaml.safe_load(cfg_path.read_text())
    assert written["thresholds"]["single_venue_dominance_pct"] == 0.65


def test_apply_writes_brier_miscalibration(tmp_path: Path) -> None:
    import yaml as _yaml

    from razor_rooster.report_generator.engines.suggestions import (
        apply_threshold_suggestion,
    )

    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"brier_miscalibration": 0.25}},
    )
    apply_threshold_suggestion(
        config_path=cfg_path,
        measurement_kind="brier_per_sector",
        new_value=0.18,
        target_percentile=0.50,
    )
    written = _yaml.safe_load(cfg_path.read_text())
    assert written["thresholds"]["brier_miscalibration"] == 0.18


def test_apply_creates_thresholds_block_if_missing(tmp_path: Path) -> None:
    """A config without a ``thresholds:`` block gets one created."""
    import yaml as _yaml

    from razor_rooster.report_generator.engines.suggestions import (
        apply_threshold_suggestion,
    )

    cfg_path = _seed_config_yaml(
        tmp_path,
        {"enabled_sections": ["cross_venue"]},
    )
    apply_threshold_suggestion(
        config_path=cfg_path,
        measurement_kind="cross_venue_spread_bps",
        new_value=600.0,
    )
    written = _yaml.safe_load(cfg_path.read_text())
    assert written["thresholds"]["cross_venue_spread_bps"] == 600


def test_apply_refuses_unknown_kind(tmp_path: Path) -> None:
    from razor_rooster.report_generator.engines.suggestions import (
        ApplyError,
        apply_threshold_suggestion,
    )

    cfg_path = _seed_config_yaml(tmp_path, {"thresholds": {}})
    with pytest.raises(ApplyError, match="not writable"):
        apply_threshold_suggestion(
            config_path=cfg_path,
            measurement_kind="hypothetical_other_kind",
            new_value=1.0,
        )


def test_apply_refuses_dominance_target_at_or_above_one(tmp_path: Path) -> None:
    """target_pct >= 1.0 for dominance would silence the warning."""
    from razor_rooster.report_generator.engines.suggestions import (
        ApplyError,
        apply_threshold_suggestion,
    )

    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"single_venue_dominance_pct": 0.80}},
    )
    with pytest.raises(ApplyError, match="silence the dominance warning"):
        apply_threshold_suggestion(
            config_path=cfg_path,
            measurement_kind="single_venue_dominance_share",
            new_value=1.0,
            target_percentile=1.0,
        )


def test_apply_refuses_missing_config(tmp_path: Path) -> None:
    from razor_rooster.report_generator.engines.suggestions import (
        ApplyError,
        apply_threshold_suggestion,
    )

    cfg_path = tmp_path / "does-not-exist.yaml"
    with pytest.raises(ApplyError, match="config file not found"):
        apply_threshold_suggestion(
            config_path=cfg_path,
            measurement_kind="cross_venue_spread_bps",
            new_value=600.0,
        )


def test_apply_records_previous_none_when_knob_missing(tmp_path: Path) -> None:
    """If the targeted knob isn't in the YAML, previous_value is None."""
    from razor_rooster.report_generator.engines.suggestions import (
        apply_threshold_suggestion,
    )

    cfg_path = _seed_config_yaml(tmp_path, {"thresholds": {}})
    result = apply_threshold_suggestion(
        config_path=cfg_path,
        measurement_kind="cross_venue_spread_bps",
        new_value=600.0,
    )
    assert result.previous_value is None
    assert result.new_value == 600.0


def test_apply_backup_preserves_original_bytes(tmp_path: Path) -> None:
    """The backup file is a byte-perfect copy of the pre-write config."""
    from razor_rooster.report_generator.engines.suggestions import (
        apply_threshold_suggestion,
    )

    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    original = cfg_path.read_text(encoding="utf-8")
    result = apply_threshold_suggestion(
        config_path=cfg_path,
        measurement_kind="cross_venue_spread_bps",
        new_value=750.0,
    )
    assert result.backup_path.read_text(encoding="utf-8") == original
    # And the live config is now different.
    assert cfg_path.read_text(encoding="utf-8") != original


# -- CLI: --apply path -----------------------------------------------------


def test_cli_apply_requires_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "apply.duckdb"
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
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--apply",
            "--target-pct",
            "0.70",
        ],
    )
    assert result.exit_code != 0
    assert "--apply requires --kind" in result.output


def test_cli_apply_requires_exactly_one_target_pct(tmp_path: Path) -> None:
    db_path = tmp_path / "apply2.duckdb"
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
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--apply",
            "--target-pct",
            "0.50",
            "--target-pct",
            "0.70",
        ],
    )
    assert result.exit_code != 0
    assert "exactly one --target-pct" in result.output


def test_cli_apply_writes_with_yes_flag(tmp_path: Path) -> None:
    """End-to-end: CLI applies a suggested value with --yes (no prompt)."""
    import yaml as _yaml

    db_path = tmp_path / "apply3.duckdb"
    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
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
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--target-pct",
            "0.70",
            "--apply",
            "--yes",
            "--config",
            str(cfg_path),
        ],
    )
    assert result.exit_code == 0
    assert "Applied." in result.output
    assert "Backup saved to" in result.output
    written = _yaml.safe_load(cfg_path.read_text())
    # Suggested value at p70 should be different from the original 500.
    assert written["thresholds"]["cross_venue_spread_bps"] != 500


def test_cli_apply_prompt_negative_response_skips(tmp_path: Path) -> None:
    """Without --yes, answering "n" to the prompt skips the write."""
    import yaml as _yaml

    db_path = tmp_path / "apply4.duckdb"
    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
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
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--target-pct",
            "0.70",
            "--apply",
            "--config",
            str(cfg_path),
        ],
        input="n\n",
    )
    assert result.exit_code == 0
    assert "Skipped (no change applied)." in result.output
    written = _yaml.safe_load(cfg_path.read_text())
    # Original value preserved.
    assert written["thresholds"]["cross_venue_spread_bps"] == 500


def test_cli_apply_refuses_dominance_at_one(tmp_path: Path) -> None:
    """The CLI surfaces ApplyError when the engine refuses the write."""
    db_path = tmp_path / "apply5.duckdb"
    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"single_venue_dominance_pct": 0.80}},
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
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
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "single_venue_dominance_share",
            "--target-pct",
            "1.0",
            "--apply",
            "--yes",
            "--config",
            str(cfg_path),
        ],
    )
    # CLI exits non-zero with the refusal message.
    assert result.exit_code != 0
    assert "Refused" in (result.output + (result.stderr or ""))


def test_cli_apply_uses_descriptive_language_only(tmp_path: Path) -> None:
    """The --apply confirmation output passes the imperative-language linter."""
    from razor_rooster.position_engine.frame.linter import check_text

    db_path = tmp_path / "apply_lint.duckdb"
    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
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
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--target-pct",
            "0.70",
            "--apply",
            "--yes",
            "--config",
            str(cfg_path),
        ],
    )
    assert result.exit_code == 0
    check_text(result.output)


# -- stability metric (T-RG-COMPAT-SUGG-003 v0.42.0) -----------------------


def test_stability_default_threshold_documented() -> None:
    from razor_rooster.report_generator.engines.suggestions import (
        DEFAULT_STABILITY_CV_THRESHOLD,
    )

    assert DEFAULT_STABILITY_CV_THRESHOLD == 0.5


def test_stability_returns_none_with_one_cycle(conn: duckdb.DuckDBPyConnection) -> None:
    """Need at least two cycles to compute variation."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    _seed_cycle(
        conn,
        report_id="r1",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base,
        values=[100.0, 200.0, 300.0],
        threshold=200.0,
    )
    report_obj = suggest_thresholds(conn, measurement_kind="cross_venue_spread_bps")
    assert report_obj.stability_cv is None
    assert report_obj.unstable is False


def test_stability_low_for_consistent_distributions(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Identical distributions across cycles → CV is exactly 0."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    for i in range(3):
        _seed_cycle(
            conn,
            report_id=f"r{i}",
            measurement_kind="cross_venue_spread_bps",
            measured_at=base + timedelta(hours=i),
            values=[100.0, 200.0, 300.0, 400.0, 500.0],
            threshold=500.0,
        )
    report_obj = suggest_thresholds(conn, measurement_kind="cross_venue_spread_bps")
    assert report_obj.stability_cv == 0.0
    assert report_obj.unstable is False


def test_stability_flags_unstable_when_distributions_swing(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Wildly different distributions → CV exceeds threshold."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    _seed_cycle(
        conn,
        report_id="small",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base,
        values=[10.0, 20.0, 30.0],
        threshold=50.0,
    )
    _seed_cycle(
        conn,
        report_id="big",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base + timedelta(hours=1),
        values=[1000.0, 2000.0, 3000.0],
        threshold=50.0,
    )
    report_obj = suggest_thresholds(conn, measurement_kind="cross_venue_spread_bps")
    assert report_obj.stability_cv is not None
    assert report_obj.stability_cv > 0.5
    assert report_obj.unstable is True


def test_stability_threshold_overridable(conn: duckdb.DuckDBPyConnection) -> None:
    """A stricter threshold can flip a borderline-stable result to unstable."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    # Two cycles with modest variation: CV around 0.1.
    _seed_cycle(
        conn,
        report_id="a",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base,
        values=[90.0, 180.0, 270.0],
        threshold=200.0,
    )
    _seed_cycle(
        conn,
        report_id="b",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base + timedelta(hours=1),
        values=[110.0, 220.0, 330.0],
        threshold=200.0,
    )
    default = suggest_thresholds(conn, measurement_kind="cross_venue_spread_bps")
    strict = suggest_thresholds(
        conn,
        measurement_kind="cross_venue_spread_bps",
        stability_cv_threshold=0.05,
    )
    # Default 0.5 threshold → stable.
    assert default.unstable is False
    # Strict 0.05 threshold → unstable.
    assert strict.unstable is True


def test_stability_skips_zero_observation_cycles(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """Zero-observation cycles don't contribute noise to the CV."""
    base = datetime(2026, 5, 15, tzinfo=UTC)
    # Two real cycles with identical distributions + an empty cycle.
    _seed_cycle(
        conn,
        report_id="a",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base,
        values=[100.0, 200.0, 300.0, 400.0, 500.0],
        threshold=500.0,
    )
    _seed_cycle(
        conn,
        report_id="empty",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base + timedelta(hours=1),
        values=[],
        threshold=500.0,
    )
    _seed_cycle(
        conn,
        report_id="b",
        measurement_kind="cross_venue_spread_bps",
        measured_at=base + timedelta(hours=2),
        values=[100.0, 200.0, 300.0, 400.0, 500.0],
        threshold=500.0,
    )
    report_obj = suggest_thresholds(conn, measurement_kind="cross_venue_spread_bps")
    # Two real, identical cycles → CV exactly 0.
    assert report_obj.stability_cv == 0.0
    assert report_obj.unstable is False


def test_cli_suggest_thresholds_shows_stability_when_known(tmp_path: Path) -> None:
    """The CLI prints a `stability:` line when at least two cycles exist."""
    db_path = tmp_path / "stab.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            for i in range(3):
                persist_threshold_measurement(
                    c,
                    report_id=f"r{i}",
                    measurement_kind="cross_venue_spread_bps",
                    measured_at=datetime(2026, 5, 15, i, tzinfo=UTC),
                    distribution=compute_distribution([100.0, 200.0, 300.0], threshold=200.0),
                )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
        ],
    )
    assert result.exit_code == 0
    assert "stability:" in result.output
    # Identical distributions → "stable".
    assert "stable" in result.output


def test_cli_suggest_thresholds_omits_stability_for_single_cycle(
    tmp_path: Path,
) -> None:
    """With one cycle the CLI doesn't print a stability line."""
    db_path = tmp_path / "stab_single.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 0, tzinfo=UTC),
                distribution=compute_distribution([100.0, 200.0, 300.0], threshold=200.0),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
        ],
    )
    assert result.exit_code == 0
    assert "stability:" not in result.output


def test_cli_apply_confirmation_warns_when_unstable(tmp_path: Path) -> None:
    """The --apply prompt mentions instability when the CV exceeds the threshold."""
    import yaml as _yaml

    db_path = tmp_path / "apply_unstable.duckdb"
    cfg_path = tmp_path / "report.yaml"
    cfg_path.write_text(
        _yaml.safe_dump({"thresholds": {"cross_venue_spread_bps": 100}}),
        encoding="utf-8",
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="small",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 0, tzinfo=UTC),
                distribution=compute_distribution([10.0, 20.0, 30.0], threshold=50.0),
            )
            persist_threshold_measurement(
                c,
                report_id="big",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 1, tzinfo=UTC),
                distribution=compute_distribution([1000.0, 2000.0, 3000.0], threshold=50.0),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--target-pct",
            "0.70",
            "--apply",
            "--yes",
            "--config",
            str(cfg_path),
        ],
    )
    assert result.exit_code == 0
    assert "unstable" in result.output
    assert "noisier than usual" in result.output


def test_cli_unstable_output_uses_descriptive_language_only(
    tmp_path: Path,
) -> None:
    """The instability warning text passes the imperative-language linter."""
    from razor_rooster.position_engine.frame.linter import check_text

    db_path = tmp_path / "unstable_lint.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="small",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 0, tzinfo=UTC),
                distribution=compute_distribution([10.0, 20.0, 30.0], threshold=50.0),
            )
            persist_threshold_measurement(
                c,
                report_id="big",
                measurement_kind="cross_venue_spread_bps",
                measured_at=datetime(2026, 5, 15, 1, tzinfo=UTC),
                distribution=compute_distribution([1000.0, 2000.0, 3000.0], threshold=50.0),
            )
    finally:
        store.close()
    runner = CliRunner()
    result = runner.invoke(
        report_cli,
        [
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
        ],
    )
    assert result.exit_code == 0
    check_text(result.output)


# -- compute_apply_diff (T-RG-COMPAT-DIFF-001 v0.43.0) ---------------------


def test_compute_apply_diff_basic_round_trip(tmp_path: Path) -> None:
    from razor_rooster.report_generator.engines.suggestions import compute_apply_diff

    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    diff = compute_apply_diff(
        config_path=cfg_path,
        measurement_kind="cross_venue_spread_bps",
        new_value=750.0,
    )
    assert "--- " in diff
    assert "+++ " in diff
    assert "@@ thresholds.cross_venue_spread_bps @@" in diff
    assert "- thresholds.cross_venue_spread_bps: 500" in diff
    assert "+ thresholds.cross_venue_spread_bps: 750" in diff


def test_compute_apply_diff_handles_unset_knob(tmp_path: Path) -> None:
    from razor_rooster.report_generator.engines.suggestions import compute_apply_diff

    cfg_path = _seed_config_yaml(tmp_path, {"thresholds": {}})
    diff = compute_apply_diff(
        config_path=cfg_path,
        measurement_kind="cross_venue_spread_bps",
        new_value=600.0,
    )
    assert "(unset)" in diff
    assert "+ thresholds.cross_venue_spread_bps: 600" in diff


def test_compute_apply_diff_renders_float_for_pct_knob(tmp_path: Path) -> None:
    from razor_rooster.report_generator.engines.suggestions import compute_apply_diff

    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"single_venue_dominance_pct": 0.80}},
    )
    diff = compute_apply_diff(
        config_path=cfg_path,
        measurement_kind="single_venue_dominance_share",
        new_value=0.65,
    )
    assert "- thresholds.single_venue_dominance_pct: 0.8" in diff
    assert "+ thresholds.single_venue_dominance_pct: 0.65" in diff


def test_compute_apply_diff_unknown_kind_returns_message(tmp_path: Path) -> None:
    from razor_rooster.report_generator.engines.suggestions import compute_apply_diff

    cfg_path = _seed_config_yaml(tmp_path, {"thresholds": {}})
    diff = compute_apply_diff(
        config_path=cfg_path,
        measurement_kind="hypothetical",
        new_value=1.0,
    )
    assert "diff unavailable" in diff


def test_compute_apply_diff_missing_file_returns_message(tmp_path: Path) -> None:
    from razor_rooster.report_generator.engines.suggestions import compute_apply_diff

    diff = compute_apply_diff(
        config_path=tmp_path / "missing.yaml",
        measurement_kind="cross_venue_spread_bps",
        new_value=600.0,
    )
    assert "diff unavailable" in diff
    assert "does not exist" in diff


def test_cli_diff_requires_apply(tmp_path: Path) -> None:
    """--diff without --apply is rejected."""
    db_path = tmp_path / "diff_no_apply.duckdb"
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
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--diff",
        ],
    )
    assert result.exit_code != 0
    assert "--diff is only meaningful with --apply" in (result.output + (result.stderr or ""))


def test_cli_diff_prints_unified_diff_before_prompt(tmp_path: Path) -> None:
    """--diff prints a unified-diff preview before the operator confirms."""
    db_path = tmp_path / "diff_yes.duckdb"
    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
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
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--target-pct",
            "0.70",
            "--apply",
            "--diff",
            "--yes",
            "--config",
            str(cfg_path),
        ],
    )
    assert result.exit_code == 0
    assert "@@ thresholds.cross_venue_spread_bps @@" in result.output
    assert "- thresholds.cross_venue_spread_bps: 500" in result.output


def test_cli_diff_output_is_descriptive_only(tmp_path: Path) -> None:
    """The diff output passes the imperative-language linter."""
    from razor_rooster.position_engine.frame.linter import check_text

    db_path = tmp_path / "diff_lint.duckdb"
    cfg_path = _seed_config_yaml(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 500}},
    )
    store = DuckDBStore(db_path)
    try:
        with store.connection() as c:
            run_pending_report_generator_migrations(c)
            persist_threshold_measurement(
                c,
                report_id="r1",
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
            "suggest-thresholds",
            "--db",
            str(db_path),
            "--kind",
            "cross_venue_spread_bps",
            "--target-pct",
            "0.70",
            "--apply",
            "--diff",
            "--yes",
            "--config",
            str(cfg_path),
        ],
    )
    assert result.exit_code == 0
    check_text(result.output)
