"""T-022 verification — config loader.

Verifies:
- ``load_ingest_schedule`` parses the bundled config and returns a typed model.
- ``load_source_caps`` parses the bundled caps and validates the global section.
- Validation rejects: missing top-level keys, malformed time_of_day, bad cadence,
  inverted warn/pause percentages, empty sources map, extra fields.
- Configs are frozen — mutating them raises.
- Bundled config files match the v1 source set from the spec.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from razor_rooster.data_ingest.config.loader import (
    ConfigError,
    GlobalCaps,
    IngestScheduleConfig,
    PerSourceCaps,
    SourceCapsConfig,
    SourceSchedule,
    load_ingest_schedule,
    load_source_caps,
)

# Path to the bundled config files (relative to repo root).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_SCHEDULE = _REPO_ROOT / "config" / "ingest_schedule.yaml"
_BUNDLED_CAPS = _REPO_ROOT / "config" / "source_caps.yaml"


def test_bundled_schedule_loads(tmp_path: Path) -> None:
    config = load_ingest_schedule(_BUNDLED_SCHEDULE)
    assert isinstance(config, IngestScheduleConfig)
    assert config.version == 1
    assert "fred" in config.sources
    assert "acled" in config.sources
    assert "gdelt_events" in config.sources


def test_bundled_caps_loads() -> None:
    config = load_source_caps(_BUNDLED_CAPS)
    assert isinstance(config, SourceCapsConfig)
    assert config.version == 1
    assert config.global_caps.max_corpus_bytes == 107374182400
    assert "gdelt_events" in config.per_source
    assert config.per_source["gdelt_events"].max_backfill_years == 5
    assert config.per_source["gdelt_events"].max_bytes == 32212254720


def test_bundled_schedule_covers_all_v1_sources() -> None:
    config = load_ingest_schedule(_BUNDLED_SCHEDULE)
    expected = {
        "fred",
        "worldbank",
        "who_don",
        "acled",
        "gdelt_events",
        "federal_register",
        "noaa",
        "usgs_minerals",
        "eia",
        "nrc_adams",
        "regulations_gov",
        "bdi",
    }
    assert expected <= set(config.sources.keys())


def test_acled_freshness_threshold_is_3_days(tmp_path: Path) -> None:
    """Per the v0.12.0 ACLED amendment, ACLED's freshness window is 3 days."""
    config = load_ingest_schedule(_BUNDLED_SCHEDULE)
    assert config.sources["acled"].freshness_threshold_seconds == 259200


def _write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    bad = _write(tmp_path / "bad.yaml", "version: 1\n  invalid: indentation")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_ingest_schedule(bad)


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_ingest_schedule(tmp_path / "nonexistent.yaml")


def test_empty_file_raises_config_error(tmp_path: Path) -> None:
    empty = _write(tmp_path / "empty.yaml", "")
    with pytest.raises(ConfigError, match="empty"):
        load_ingest_schedule(empty)


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    bad = _write(tmp_path / "list.yaml", "- one\n- two\n")
    with pytest.raises(ConfigError, match="top-level mapping"):
        load_ingest_schedule(bad)


def test_schedule_rejects_extra_fields(tmp_path: Path) -> None:
    bad = _write(
        tmp_path / "extra.yaml",
        """
version: 1
unexpected_field: foo
sources:
  fred:
    cadence: daily
    freshness_threshold_seconds: 172800
""",
    )
    with pytest.raises(ConfigError, match="validation failed"):
        load_ingest_schedule(bad)


def test_schedule_rejects_empty_sources(tmp_path: Path) -> None:
    bad = _write(tmp_path / "no_sources.yaml", "version: 1\nsources: {}\n")
    with pytest.raises(ConfigError, match="at least one source"):
        load_ingest_schedule(bad)


def test_schedule_rejects_unknown_cadence(tmp_path: Path) -> None:
    bad = _write(
        tmp_path / "bad_cadence.yaml",
        """
version: 1
sources:
  fred:
    cadence: hourly
    freshness_threshold_seconds: 172800
""",
    )
    with pytest.raises(ConfigError, match="validation failed"):
        load_ingest_schedule(bad)


def test_schedule_rejects_malformed_time_of_day(tmp_path: Path) -> None:
    bad = _write(
        tmp_path / "bad_time.yaml",
        """
version: 1
sources:
  fred:
    cadence: daily
    time_of_day: "25:99"
    freshness_threshold_seconds: 172800
""",
    )
    with pytest.raises(ConfigError, match="time_of_day"):
        load_ingest_schedule(bad)


def test_schedule_accepts_optional_time_of_day(tmp_path: Path) -> None:
    ok = _write(
        tmp_path / "no_time.yaml",
        """
version: 1
sources:
  usgs_minerals:
    cadence: annual
    freshness_threshold_seconds: 31536000
""",
    )
    config = load_ingest_schedule(ok)
    assert config.sources["usgs_minerals"].time_of_day is None


def test_schedule_rejects_negative_freshness_threshold(tmp_path: Path) -> None:
    bad = _write(
        tmp_path / "bad_freshness.yaml",
        """
version: 1
sources:
  fred:
    cadence: daily
    freshness_threshold_seconds: -1
""",
    )
    with pytest.raises(ConfigError, match="validation failed"):
        load_ingest_schedule(bad)


def test_schedule_rejects_max_workers_out_of_range(tmp_path: Path) -> None:
    bad = _write(
        tmp_path / "bad_workers.yaml",
        """
version: 1
defaults:
  max_workers: 0
sources:
  fred:
    cadence: daily
    freshness_threshold_seconds: 172800
""",
    )
    with pytest.raises(ConfigError, match="validation failed"):
        load_ingest_schedule(bad)


def test_caps_rejects_inverted_thresholds(tmp_path: Path) -> None:
    bad = _write(
        tmp_path / "inverted.yaml",
        """
version: 1
global:
  max_corpus_bytes: 1073741824
  warn_at_pct: 95.0
  pause_backfill_at_pct: 80.0
""",
    )
    with pytest.raises(ConfigError, match="strictly less than"):
        load_source_caps(bad)


def test_caps_rejects_zero_max_corpus(tmp_path: Path) -> None:
    bad = _write(
        tmp_path / "zero.yaml",
        """
version: 1
global:
  max_corpus_bytes: 0
  warn_at_pct: 80.0
  pause_backfill_at_pct: 95.0
""",
    )
    with pytest.raises(ConfigError, match="validation failed"):
        load_source_caps(bad)


def test_caps_rejects_pct_above_100(tmp_path: Path) -> None:
    bad = _write(
        tmp_path / "high_pct.yaml",
        """
version: 1
global:
  max_corpus_bytes: 1073741824
  warn_at_pct: 80.0
  pause_backfill_at_pct: 101.0
""",
    )
    with pytest.raises(ConfigError, match="validation failed"):
        load_source_caps(bad)


def test_caps_per_source_caps_optional(tmp_path: Path) -> None:
    """``per_source`` is optional; default empty dict is valid."""
    ok = _write(
        tmp_path / "only_global.yaml",
        """
version: 1
global:
  max_corpus_bytes: 1073741824
  warn_at_pct: 80.0
  pause_backfill_at_pct: 95.0
""",
    )
    config = load_source_caps(ok)
    assert config.per_source == {}


def test_schedule_config_is_frozen() -> None:
    config = load_ingest_schedule(_BUNDLED_SCHEDULE)
    with pytest.raises((ValueError, AttributeError, TypeError)):
        config.version = 2  # type: ignore[misc]


def test_source_schedule_is_frozen() -> None:
    schedule = SourceSchedule(
        cadence="daily",
        freshness_threshold_seconds=172800,
    )
    with pytest.raises((ValueError, AttributeError, TypeError)):
        schedule.cadence = "weekly"  # type: ignore[misc]


def test_source_caps_config_is_frozen() -> None:
    config = load_source_caps(_BUNDLED_CAPS)
    with pytest.raises((ValueError, AttributeError, TypeError)):
        config.version = 2  # type: ignore[misc]


def test_global_caps_dataclass_construction() -> None:
    caps = GlobalCaps(max_corpus_bytes=1024, warn_at_pct=70.0, pause_backfill_at_pct=90.0)
    assert caps.warn_at_pct == 70.0


def test_per_source_caps_optional_fields() -> None:
    """Both fields are optional individually."""
    just_years = PerSourceCaps(max_backfill_years=10)
    assert just_years.max_backfill_years == 10
    assert just_years.max_bytes is None

    just_bytes = PerSourceCaps(max_bytes=1024)
    assert just_bytes.max_backfill_years is None

    neither = PerSourceCaps()
    assert neither.max_backfill_years is None
    assert neither.max_bytes is None
