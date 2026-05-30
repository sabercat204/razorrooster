"""Tests for the report-generator config loader (DEFER-RG-COMPAT-001).

Covers the v0.39.0 ``thresholds:`` block: defaults, parsing, range
clamping, type-coercion fallbacks, and the threshold-pass-through to
section assemblers via the generator dispatch.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from razor_rooster.report_generator.config.loader import (
    ALL_SECTIONS,
    DEFAULT_SINGLE_VENUE_DOMINANCE_PCT,
    ReportConfig,
    ReportThresholds,
    load_config,
)
from razor_rooster.report_generator.engines.section_assemblers.calibration import (
    DEFAULT_BRIER_WINDOW_DAYS,
    DEFAULT_MISCALIBRATION_THRESHOLD,
)
from razor_rooster.report_generator.engines.section_assemblers.cross_venue import (
    DEFAULT_SPREAD_THRESHOLD_BPS,
)


def _write_config(path: Path, payload: dict[str, object]) -> Path:
    file_path = path / "report.yaml"
    file_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return file_path


# -- defaults --------------------------------------------------------------


def test_load_returns_default_thresholds_when_block_missing(tmp_path: Path) -> None:
    """No ``thresholds:`` key → all four defaults match section-assembler constants."""
    cfg_path = _write_config(tmp_path, {"enabled_sections": list(ALL_SECTIONS)})
    cfg = load_config(cfg_path)
    assert cfg.thresholds.cross_venue_spread_bps == DEFAULT_SPREAD_THRESHOLD_BPS
    assert cfg.thresholds.single_venue_dominance_pct == DEFAULT_SINGLE_VENUE_DOMINANCE_PCT
    assert cfg.thresholds.brier_window_days == DEFAULT_BRIER_WINDOW_DAYS
    assert cfg.thresholds.brier_miscalibration == DEFAULT_MISCALIBRATION_THRESHOLD


def test_default_constructed_config_uses_default_thresholds() -> None:
    cfg = ReportConfig()
    assert cfg.thresholds == ReportThresholds()
    assert cfg.thresholds.cross_venue_spread_bps == DEFAULT_SPREAD_THRESHOLD_BPS
    assert cfg.thresholds.single_venue_dominance_pct == DEFAULT_SINGLE_VENUE_DOMINANCE_PCT
    assert cfg.thresholds.brier_window_days == DEFAULT_BRIER_WINDOW_DAYS
    assert cfg.thresholds.brier_miscalibration == DEFAULT_MISCALIBRATION_THRESHOLD


def test_missing_config_file_returns_full_defaults(tmp_path: Path) -> None:
    """Loader is robust to a missing config file path."""
    cfg = load_config(tmp_path / "nonexistent.yaml")
    assert cfg.enabled_sections == ALL_SECTIONS
    assert cfg.thresholds == ReportThresholds()


# -- explicit overrides ----------------------------------------------------


def test_explicit_thresholds_override_defaults(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "cross_venue_spread_bps": 250,
                "single_venue_dominance_pct": 0.65,
                "brier_window_days": 30,
                "brier_miscalibration": 0.15,
            },
        },
    )
    cfg = load_config(cfg_path)
    assert cfg.thresholds.cross_venue_spread_bps == 250
    assert cfg.thresholds.single_venue_dominance_pct == pytest.approx(0.65)
    assert cfg.thresholds.brier_window_days == 30
    assert cfg.thresholds.brier_miscalibration == pytest.approx(0.15)


def test_partial_threshold_block_keeps_other_defaults(tmp_path: Path) -> None:
    """Only one knob set → the other three keep their defaults."""
    cfg_path = _write_config(
        tmp_path,
        {"thresholds": {"cross_venue_spread_bps": 250}},
    )
    cfg = load_config(cfg_path)
    assert cfg.thresholds.cross_venue_spread_bps == 250
    assert cfg.thresholds.single_venue_dominance_pct == DEFAULT_SINGLE_VENUE_DOMINANCE_PCT
    assert cfg.thresholds.brier_window_days == DEFAULT_BRIER_WINDOW_DAYS
    assert cfg.thresholds.brier_miscalibration == DEFAULT_MISCALIBRATION_THRESHOLD


# -- bounds and validation -------------------------------------------------


def test_out_of_range_int_falls_back_to_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "cross_venue_spread_bps": 99_999,  # above the 10_000 cap
                "brier_window_days": 0,  # below the [1, 3650] range
            },
        },
    )
    with caplog.at_level(logging.WARNING, logger="razor_rooster.report_generator.config.loader"):
        cfg = load_config(cfg_path)
    assert cfg.thresholds.cross_venue_spread_bps == DEFAULT_SPREAD_THRESHOLD_BPS
    assert cfg.thresholds.brier_window_days == DEFAULT_BRIER_WINDOW_DAYS
    messages = " ".join(record.message for record in caplog.records)
    assert "cross_venue_spread_bps" in messages
    assert "brier_window_days" in messages


def test_out_of_range_float_falls_back_to_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "single_venue_dominance_pct": 1.5,  # above 1.0
                "brier_miscalibration": -0.1,  # below 0.0
            },
        },
    )
    with caplog.at_level(logging.WARNING, logger="razor_rooster.report_generator.config.loader"):
        cfg = load_config(cfg_path)
    assert cfg.thresholds.single_venue_dominance_pct == DEFAULT_SINGLE_VENUE_DOMINANCE_PCT
    assert cfg.thresholds.brier_miscalibration == DEFAULT_MISCALIBRATION_THRESHOLD
    messages = " ".join(record.message for record in caplog.records)
    assert "single_venue_dominance_pct" in messages
    assert "brier_miscalibration" in messages


def test_non_numeric_threshold_falls_back_to_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "cross_venue_spread_bps": "tight",  # not coercible
                "single_venue_dominance_pct": "very dominant",
            },
        },
    )
    with caplog.at_level(logging.WARNING, logger="razor_rooster.report_generator.config.loader"):
        cfg = load_config(cfg_path)
    assert cfg.thresholds.cross_venue_spread_bps == DEFAULT_SPREAD_THRESHOLD_BPS
    assert cfg.thresholds.single_venue_dominance_pct == DEFAULT_SINGLE_VENUE_DOMINANCE_PCT
    messages = " ".join(record.message for record in caplog.records)
    assert "could not be coerced" in messages


def test_non_dict_thresholds_block_falls_back(tmp_path: Path) -> None:
    """A scalar/list under ``thresholds:`` returns full defaults."""
    cfg_path = _write_config(tmp_path, {"thresholds": "not a dict"})
    cfg = load_config(cfg_path)
    assert cfg.thresholds == ReportThresholds()


# -- boundary inclusivity --------------------------------------------------


def test_boundary_values_accepted(tmp_path: Path) -> None:
    """Boundary values inside the documented ranges are accepted."""
    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "cross_venue_spread_bps": 0,  # lower bound
                "single_venue_dominance_pct": 1.0,  # upper bound
                "brier_window_days": 3650,  # upper bound
                "brier_miscalibration": 0.0,  # lower bound
            },
        },
    )
    cfg = load_config(cfg_path)
    assert cfg.thresholds.cross_venue_spread_bps == 0
    assert cfg.thresholds.single_venue_dominance_pct == 1.0
    assert cfg.thresholds.brier_window_days == 3650
    assert cfg.thresholds.brier_miscalibration == 0.0


def test_threshold_dataclass_is_frozen() -> None:
    """ReportThresholds is frozen — sanity check on the immutability contract."""
    thresholds = ReportThresholds()
    with pytest.raises(AttributeError):
        thresholds.cross_venue_spread_bps = 999  # type: ignore[misc]


# -- integration: generator dispatch passes thresholds through -------------


def test_generator_dispatches_cross_venue_threshold(tmp_path: Path) -> None:
    """The generator passes ``thresholds.cross_venue_spread_bps`` to the assembler."""
    from datetime import UTC, datetime

    import duckdb

    from razor_rooster.report_generator.config.loader import ReportConfig
    from razor_rooster.report_generator.engines.generator import _assemble_section

    db_path = tmp_path / "dispatch.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE comparisons ("
            "comparison_id VARCHAR, class_id VARCHAR, condition_id VARCHAR, "
            "venue VARCHAR, market_probability DOUBLE, market_volume_24h DOUBLE, "
            "market_spread_bps INTEGER, market_snapshot_ts TIMESTAMPTZ, "
            "model_probability DOUBLE, model_ci_lower DOUBLE, model_ci_upper DOUBLE, "
            "computed_at TIMESTAMPTZ"
            ")"
        )
        conn.execute(
            "CREATE TABLE pl_event_classes ("
            "class_id VARCHAR PRIMARY KEY, title VARCHAR, "
            "description VARCHAR, domain_sector VARCHAR, "
            "secondary_sectors VARCHAR, definition_version INTEGER, "
            "outcome_type VARCHAR, registered_at TIMESTAMPTZ, "
            "last_evaluated_at TIMESTAMPTZ, library_version_at_last_eval INTEGER, "
            "removed_at TIMESTAMPTZ"
            ")"
        )
        cfg = ReportConfig(
            thresholds=ReportThresholds(cross_venue_spread_bps=200),
        )
        out = _assemble_section(
            conn,
            section_name="cross_venue",
            since_ts=datetime(2026, 5, 14, tzinfo=UTC),
            until_ts=datetime(2026, 5, 15, tzinfo=UTC),
            cfg=cfg,
        )
        # Empty data, but the threshold echoed back in the content dict
        # confirms the dispatch wired the kwarg.
        assert out["spread_threshold_bps"] == 200
    finally:
        conn.close()


# -- per-sector overrides (DEFER-RG-COMPAT-002 v0.40.0) --------------------


def test_per_sector_block_loads_for_all_four_knobs(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "cross_venue_spread_bps": 500,
                "cross_venue_spread_bps_per_sector": {
                    "geopolitical": 700,
                    "macroeconomic": 300,
                },
                "single_venue_dominance_pct": 0.80,
                "single_venue_dominance_pct_per_sector": {
                    "regulatory": 0.65,
                },
                "brier_window_days": 90,
                "brier_window_days_per_sector": {
                    "macroeconomic": 30,
                    "geopolitical": 180,
                },
                "brier_miscalibration": 0.25,
                "brier_miscalibration_per_sector": {
                    "public_health": 0.20,
                },
            },
        },
    )
    cfg = load_config(cfg_path)
    assert cfg.thresholds.cross_venue_spread_bps_per_sector == {
        "geopolitical": 700,
        "macroeconomic": 300,
    }
    assert cfg.thresholds.single_venue_dominance_pct_per_sector == {"regulatory": 0.65}
    assert cfg.thresholds.brier_window_days_per_sector == {
        "macroeconomic": 30,
        "geopolitical": 180,
    }
    assert cfg.thresholds.brier_miscalibration_per_sector == {"public_health": 0.20}


def test_per_sector_lookup_falls_back_to_global() -> None:
    """Sectors without a per-sector entry get the global value."""
    cfg = ReportConfig(
        thresholds=ReportThresholds(
            cross_venue_spread_bps=500,
            cross_venue_spread_bps_per_sector={"geopolitical": 700},
            single_venue_dominance_pct=0.80,
            single_venue_dominance_pct_per_sector={"regulatory": 0.65},
            brier_window_days=90,
            brier_window_days_per_sector={"macroeconomic": 30},
            brier_miscalibration=0.25,
            brier_miscalibration_per_sector={"public_health": 0.20},
        ),
    )
    # Explicit override sectors return the override.
    assert cfg.thresholds.cross_venue_spread_bps_for_sector("geopolitical") == 700
    assert cfg.thresholds.single_venue_dominance_pct_for_sector("regulatory") == 0.65
    assert cfg.thresholds.brier_window_days_for_sector("macroeconomic") == 30
    assert cfg.thresholds.brier_miscalibration_for_sector("public_health") == 0.20
    # Sectors without an override return the global value.
    assert cfg.thresholds.cross_venue_spread_bps_for_sector("commodity") == 500
    assert cfg.thresholds.single_venue_dominance_pct_for_sector("commodity") == 0.80
    assert cfg.thresholds.brier_window_days_for_sector("commodity") == 90
    assert cfg.thresholds.brier_miscalibration_for_sector("commodity") == 0.25
    # None and unknown also return the global value.
    assert cfg.thresholds.cross_venue_spread_bps_for_sector(None) == 500
    assert cfg.thresholds.cross_venue_spread_bps_for_sector("") == 500


def test_per_sector_invalid_value_falls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "cross_venue_spread_bps_per_sector": {
                    "geopolitical": 99_999,  # out of range
                    "macroeconomic": "not a number",  # not coercible
                    "regulatory": 250,  # valid
                },
            },
        },
    )
    with caplog.at_level(logging.WARNING, logger="razor_rooster.report_generator.config.loader"):
        cfg = load_config(cfg_path)
    # The valid entry stays. The two invalid entries get the global
    # default through the per-sector dict (since the loader populates
    # them with the global value rather than skipping them — but the
    # lookup helper would return the same global value for those
    # sectors regardless, so behavior is correct either way.)
    assert cfg.thresholds.cross_venue_spread_bps_per_sector["regulatory"] == 250
    # Even though invalid entries get coerced to the global default,
    # the lookup helper returns the same value, so behavior matches
    # the contract.
    assert (
        cfg.thresholds.cross_venue_spread_bps_for_sector("geopolitical")
        == DEFAULT_SPREAD_THRESHOLD_BPS
    )
    assert (
        cfg.thresholds.cross_venue_spread_bps_for_sector("macroeconomic")
        == DEFAULT_SPREAD_THRESHOLD_BPS
    )


def test_non_dict_per_sector_falls_back_silently(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "cross_venue_spread_bps": 500,
                "cross_venue_spread_bps_per_sector": "not a dict",
            },
        },
    )
    cfg = load_config(cfg_path)
    assert cfg.thresholds.cross_venue_spread_bps_per_sector == {}


def test_per_sector_with_non_string_keys_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "brier_miscalibration_per_sector": {
                    "geopolitical": 0.20,
                    # YAML parses bare integers as int keys; we skip them
                    # because the lookup is keyed by sector name.
                    42: 0.30,
                    "": 0.40,  # empty string also skipped
                },
            },
        },
    )
    with caplog.at_level(logging.WARNING, logger="razor_rooster.report_generator.config.loader"):
        cfg = load_config(cfg_path)
    assert cfg.thresholds.brier_miscalibration_per_sector == {"geopolitical": 0.20}


# -- integration: per-sector dispatched into cross_venue assembler ---------


def test_cross_venue_threshold_applied_per_sector(tmp_path: Path) -> None:
    """A per-sector override changes the threshold for that sector only."""
    from datetime import UTC, datetime

    import duckdb

    from razor_rooster.report_generator.engines.generator import _assemble_section

    db_path = tmp_path / "per_sector_dispatch.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE comparisons ("
            "comparison_id VARCHAR, class_id VARCHAR, condition_id VARCHAR, "
            "venue VARCHAR, market_probability DOUBLE, market_volume_24h DOUBLE, "
            "market_spread_bps INTEGER, market_snapshot_ts TIMESTAMPTZ, "
            "model_probability DOUBLE, model_ci_lower DOUBLE, model_ci_upper DOUBLE, "
            "computed_at TIMESTAMPTZ"
            ")"
        )
        conn.execute(
            "CREATE TABLE pl_event_classes ("
            "class_id VARCHAR PRIMARY KEY, title VARCHAR, "
            "description VARCHAR, domain_sector VARCHAR, "
            "secondary_sectors VARCHAR, definition_version INTEGER, "
            "outcome_type VARCHAR, registered_at TIMESTAMPTZ, "
            "last_evaluated_at TIMESTAMPTZ, library_version_at_last_eval INTEGER, "
            "removed_at TIMESTAMPTZ"
            ")"
        )
        # Geopolitical class with a 4pp (400 bps) spread.
        conn.execute(
            "INSERT INTO pl_event_classes (class_id, title, domain_sector, "
            "definition_version, outcome_type, registered_at) VALUES "
            "('geo1', 'geo class', 'geopolitical', 1, 'binary', "
            "'2026-05-01 00:00:00+00')"
        )
        for i, (venue, market_p) in enumerate([("polymarket", 0.30), ("kalshi", 0.34)]):
            conn.execute(
                "INSERT INTO comparisons VALUES "
                f"('cmp-{i}', 'geo1', 'cond-{i}', '{venue}', {market_p}, 1000.0, "
                f"50, '2026-05-14 12:00:00+00', 0.50, 0.40, 0.60, "
                f"'2026-05-14 14:00:00+00')"
            )

        # Default 500 threshold → 400 bps spread is too small → no items.
        cfg_default = ReportConfig(thresholds=ReportThresholds())
        out_default = _assemble_section(
            conn,
            section_name="cross_venue",
            since_ts=datetime(2026, 5, 14, tzinfo=UTC),
            until_ts=datetime(2026, 5, 15, tzinfo=UTC),
            cfg=cfg_default,
        )
        assert out_default["items"] == []

        # Override geopolitical to 200 bps → 400 bps now exceeds → item appears.
        cfg_override = ReportConfig(
            thresholds=ReportThresholds(
                cross_venue_spread_bps=500,
                cross_venue_spread_bps_per_sector={"geopolitical": 200},
            ),
        )
        out_override = _assemble_section(
            conn,
            section_name="cross_venue",
            since_ts=datetime(2026, 5, 14, tzinfo=UTC),
            until_ts=datetime(2026, 5, 15, tzinfo=UTC),
            cfg=cfg_override,
        )
        assert len(out_override["items"]) == 1
        assert out_override["items"][0]["domain_sector"] == "geopolitical"
        assert out_override["items"][0]["applicable_threshold_bps"] == 200
        assert out_override["items"][0]["spread_bps"] == 400
    finally:
        conn.close()


def test_brier_window_per_sector_applied(tmp_path: Path) -> None:
    """Per-sector window narrows the rolling Brier window for that sector only."""
    from datetime import UTC, datetime, timedelta

    import duckdb

    from razor_rooster.report_generator.engines.generator import _assemble_section

    db_path = tmp_path / "per_sector_brier.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE comparisons ("
            "comparison_id VARCHAR, class_id VARCHAR, condition_id VARCHAR, "
            "venue VARCHAR, market_probability DOUBLE, model_probability DOUBLE, "
            "computed_at TIMESTAMPTZ"
            ")"
        )
        conn.execute(
            "CREATE TABLE pl_event_classes ("
            "class_id VARCHAR PRIMARY KEY, title VARCHAR, "
            "domain_sector VARCHAR, definition_version INTEGER, "
            "outcome_type VARCHAR, registered_at TIMESTAMPTZ"
            ")"
        )
        conn.execute(
            "CREATE TABLE comparison_resolutions ("
            "comparison_id VARCHAR, condition_id VARCHAR, "
            "resolution_outcome VARCHAR, resolution_ts TIMESTAMPTZ, "
            "model_probability_at_comparison DOUBLE, "
            "market_probability_at_comparison DOUBLE, "
            "polarity_at_comparison VARCHAR, outcome_observed INTEGER, "
            "linked_at TIMESTAMPTZ, venue VARCHAR"
            ")"
        )
        conn.execute(
            "INSERT INTO pl_event_classes (class_id, title, domain_sector, "
            "definition_version, outcome_type, registered_at) VALUES "
            "('macro1', 'macro class', 'macroeconomic', 1, 'binary', "
            "'2026-01-01 00:00:00+00')"
        )
        conn.execute(
            "INSERT INTO comparisons (comparison_id, class_id, condition_id, "
            "venue, model_probability, market_probability, computed_at) VALUES "
            "('cmp-a', 'macro1', 'cond-a', 'polymarket', 0.50, 0.45, "
            "'2026-02-15 12:00:00+00')"
        )
        conn.execute(
            "INSERT INTO comparisons (comparison_id, class_id, condition_id, "
            "venue, model_probability, market_probability, computed_at) VALUES "
            "('cmp-b', 'macro1', 'cond-b', 'polymarket', 0.50, 0.55, "
            "'2026-05-12 12:00:00+00')"
        )
        # Old resolution: 60 days before until_ts.
        until = datetime(2026, 5, 15, tzinfo=UTC)
        old_ts = until - timedelta(days=60)
        recent_ts = until - timedelta(days=10)
        conn.execute(
            "INSERT INTO comparison_resolutions VALUES "
            "('cmp-a', 'cond-a', 'yes', ?, 0.50, 0.45, 'aligned', 1, ?, 'polymarket')",
            [old_ts, until - timedelta(days=58)],
        )
        conn.execute(
            "INSERT INTO comparison_resolutions VALUES "
            "('cmp-b', 'cond-b', 'yes', ?, 0.50, 0.55, 'aligned', 1, ?, 'polymarket')",
            [recent_ts, until - timedelta(days=8)],
        )
        # 90-day window includes both → Brier averages both errors.
        cfg_default = ReportConfig(thresholds=ReportThresholds(brier_window_days=90))
        out_default = _assemble_section(
            conn,
            section_name="calibration",
            since_ts=until - timedelta(days=1),
            until_ts=until,
            cfg=cfg_default,
        )
        macro_default = next(
            entry
            for entry in out_default["sector_brier_scores"]
            if entry["sector"] == "macroeconomic"
        )
        assert macro_default["n_resolutions"] == 2
        assert macro_default["window_days"] == 90

        # 30-day window for macroeconomic → only the recent resolution.
        cfg_override = ReportConfig(
            thresholds=ReportThresholds(
                brier_window_days=90,
                brier_window_days_per_sector={"macroeconomic": 30},
            ),
        )
        out_override = _assemble_section(
            conn,
            section_name="calibration",
            since_ts=until - timedelta(days=1),
            until_ts=until,
            cfg=cfg_override,
        )
        macro_override = next(
            entry
            for entry in out_override["sector_brier_scores"]
            if entry["sector"] == "macroeconomic"
        )
        assert macro_override["n_resolutions"] == 1
        assert macro_override["window_days"] == 30
    finally:
        conn.close()


def test_brier_miscalibration_per_sector_applied(tmp_path: Path) -> None:
    """Per-sector miscalibration threshold flips the miscalibrated flag."""
    from datetime import UTC, datetime, timedelta

    import duckdb

    from razor_rooster.report_generator.engines.section_assemblers import (
        calibration as calibration_assembler,
    )

    db_path = tmp_path / "per_sector_miscal.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE comparisons ("
            "comparison_id VARCHAR, class_id VARCHAR, condition_id VARCHAR, "
            "venue VARCHAR, market_probability DOUBLE, model_probability DOUBLE, "
            "computed_at TIMESTAMPTZ"
            ")"
        )
        conn.execute(
            "CREATE TABLE pl_event_classes ("
            "class_id VARCHAR PRIMARY KEY, title VARCHAR, "
            "domain_sector VARCHAR, definition_version INTEGER, "
            "outcome_type VARCHAR, registered_at TIMESTAMPTZ"
            ")"
        )
        conn.execute(
            "CREATE TABLE comparison_resolutions ("
            "comparison_id VARCHAR, condition_id VARCHAR, "
            "resolution_outcome VARCHAR, resolution_ts TIMESTAMPTZ, "
            "model_probability_at_comparison DOUBLE, "
            "market_probability_at_comparison DOUBLE, "
            "polarity_at_comparison VARCHAR, outcome_observed INTEGER, "
            "linked_at TIMESTAMPTZ, venue VARCHAR"
            ")"
        )
        conn.execute(
            "INSERT INTO pl_event_classes (class_id, title, domain_sector, "
            "definition_version, outcome_type, registered_at) VALUES "
            "('ph1', 'ph class', 'public_health', 1, 'binary', "
            "'2026-01-01 00:00:00+00')"
        )
        conn.execute(
            "INSERT INTO comparisons (comparison_id, class_id, condition_id, "
            "venue, model_probability, market_probability, computed_at) VALUES "
            "('cmp-c', 'ph1', 'cond-c', 'polymarket', 0.50, 0.50, "
            "'2026-05-01 12:00:00+00')"
        )
        until = datetime(2026, 5, 15, tzinfo=UTC)
        # squared error = (0.50 - 1)^2 = 0.25 — exactly at default threshold.
        conn.execute(
            "INSERT INTO comparison_resolutions VALUES "
            "('cmp-c', 'cond-c', 'yes', ?, 0.50, 0.50, 'aligned', 1, ?, 'polymarket')",
            [until - timedelta(days=10), until - timedelta(days=8)],
        )
        # Default threshold 0.25, strict > → not miscalibrated at boundary.
        out_default = calibration_assembler.assemble(
            conn,
            since_ts=until - timedelta(days=1),
            until_ts=until,
            miscalibration_threshold=0.25,
        )
        ph_default = next(
            entry
            for entry in out_default["sector_brier_scores"]
            if entry["sector"] == "public_health"
        )
        assert ph_default["brier_score"] == 0.25
        assert ph_default["miscalibrated"] is False

        # Override public_health threshold to 0.20 → 0.25 now exceeds.
        out_override = calibration_assembler.assemble(
            conn,
            since_ts=until - timedelta(days=1),
            until_ts=until,
            miscalibration_threshold=0.25,
            miscalibration_threshold_per_sector={"public_health": 0.20},
        )
        ph_override = next(
            entry
            for entry in out_override["sector_brier_scores"]
            if entry["sector"] == "public_health"
        )
        assert ph_override["brier_score"] == 0.25
        assert ph_override["miscalibrated"] is True
        assert ph_override["miscalibration_threshold"] == 0.20
    finally:
        conn.close()


# -- reliability config knobs (DEFER-RG-COMPAT-003 v0.41.0) ----------------


def test_reliability_defaults_match_assembler_constants(tmp_path: Path) -> None:
    from razor_rooster.report_generator.engines.section_assemblers.reliability import (
        DEFAULT_BIN_COUNT,
        DEFAULT_MIN_RESOLUTIONS_PER_BIN,
    )

    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.thresholds.reliability_bin_count == DEFAULT_BIN_COUNT
    assert cfg.thresholds.reliability_min_resolutions_per_bin == DEFAULT_MIN_RESOLUTIONS_PER_BIN


def test_reliability_overrides_load(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "reliability_bin_count": 20,
                "reliability_min_resolutions_per_bin": 10,
            },
        },
    )
    cfg = load_config(cfg_path)
    assert cfg.thresholds.reliability_bin_count == 20
    assert cfg.thresholds.reliability_min_resolutions_per_bin == 10


def test_reliability_out_of_range_falls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from razor_rooster.report_generator.engines.section_assemblers.reliability import (
        DEFAULT_BIN_COUNT,
        DEFAULT_MIN_RESOLUTIONS_PER_BIN,
    )

    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "reliability_bin_count": 1,  # below [2, 50]
                "reliability_min_resolutions_per_bin": 99_999,  # above [1, 1000]
            },
        },
    )
    with caplog.at_level(logging.WARNING, logger="razor_rooster.report_generator.config.loader"):
        cfg = load_config(cfg_path)
    assert cfg.thresholds.reliability_bin_count == DEFAULT_BIN_COUNT
    assert cfg.thresholds.reliability_min_resolutions_per_bin == DEFAULT_MIN_RESOLUTIONS_PER_BIN


def test_reliability_in_all_sections_after_calibration() -> None:
    """The section ordering puts reliability between calibration and watchlist."""
    cal_idx = ALL_SECTIONS.index("calibration")
    rel_idx = ALL_SECTIONS.index("reliability")
    wl_idx = ALL_SECTIONS.index("watchlist")
    assert cal_idx < rel_idx < wl_idx


# -- per-sector reliability overrides (v0.40.0) ----------------------------


def test_reliability_per_sector_overrides_load(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "reliability_bin_count": 10,
                "reliability_bin_count_per_sector": {
                    "macroeconomic": 20,
                    "geopolitical": 5,
                },
                "reliability_min_resolutions_per_bin": 5,
                "reliability_min_resolutions_per_bin_per_sector": {
                    "macroeconomic": 10,
                    "geopolitical": 3,
                },
            },
        },
    )
    cfg = load_config(cfg_path)
    assert cfg.thresholds.reliability_bin_count_per_sector == {
        "macroeconomic": 20,
        "geopolitical": 5,
    }
    assert cfg.thresholds.reliability_min_resolutions_per_bin_per_sector == {
        "macroeconomic": 10,
        "geopolitical": 3,
    }


def test_reliability_per_sector_lookup_falls_back() -> None:
    cfg = ReportConfig(
        thresholds=ReportThresholds(
            reliability_bin_count=10,
            reliability_bin_count_per_sector={"macroeconomic": 20},
            reliability_min_resolutions_per_bin=5,
            reliability_min_resolutions_per_bin_per_sector={"climate": 3},
        ),
    )
    assert cfg.thresholds.reliability_bin_count_for_sector("macroeconomic") == 20
    assert cfg.thresholds.reliability_bin_count_for_sector("commodity") == 10
    assert cfg.thresholds.reliability_bin_count_for_sector(None) == 10
    assert cfg.thresholds.reliability_min_resolutions_per_bin_for_sector("climate") == 3
    assert cfg.thresholds.reliability_min_resolutions_per_bin_for_sector("commodity") == 5
    assert cfg.thresholds.reliability_min_resolutions_per_bin_for_sector(None) == 5


def test_reliability_per_sector_invalid_value_falls_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_path = _write_config(
        tmp_path,
        {
            "thresholds": {
                "reliability_bin_count_per_sector": {
                    "macroeconomic": 1,  # below [2, 50]
                    "geopolitical": 25,  # valid
                },
            },
        },
    )
    with caplog.at_level(logging.WARNING, logger="razor_rooster.report_generator.config.loader"):
        cfg = load_config(cfg_path)
    # Valid entry stays.
    assert cfg.thresholds.reliability_bin_count_per_sector["geopolitical"] == 25
    # Invalid entry falls back to the global default via the lookup helper.
    assert cfg.thresholds.reliability_bin_count_for_sector("macroeconomic") == 10
