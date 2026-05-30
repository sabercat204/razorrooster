"""T-PMC-002 — Polymarket config loader tests.

Verifies the three Polymarket-specific config files parse correctly and
that the loader rejects common malformed inputs with informative errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from razor_rooster.polymarket_connector.config.loader import (
    ConfigError,
    PolymarketConfig,
    RestrictedJurisdictionsConfig,
    SectorKeywordsConfig,
    load_polymarket_config,
    load_restricted_jurisdictions,
    load_sector_keywords,
)

# Bundled config paths (verified to load happily).
_BUNDLED_POLYMARKET = Path("config") / "polymarket.yaml"
_BUNDLED_KEYWORDS = Path("config") / "sector_keywords.yaml"
_BUNDLED_RESTRICTED = Path("config") / "restricted_jurisdictions.yaml"


# -- happy paths against the bundled configs ------------------------------


def test_bundled_polymarket_config_loads() -> None:
    cfg = load_polymarket_config(_BUNDLED_POLYMARKET)
    assert isinstance(cfg, PolymarketConfig)
    assert cfg.version >= 1
    assert cfg.rate_limit.bucket_capacity == 50
    assert cfg.rate_limit.refill_per_second == 50.0
    assert cfg.freshness.prices_threshold_seconds == 21_600
    assert cfg.sync.markets.cadence == "daily"
    assert cfg.sync.prices.minimum_interval_seconds >= 60


def test_bundled_sector_keywords_config_loads() -> None:
    cfg = load_sector_keywords(_BUNDLED_KEYWORDS)
    assert isinstance(cfg, SectorKeywordsConfig)
    # Every Razor sector should have at least one keyword in the bundled file.
    expected_sectors = {
        "public_health",
        "geopolitical",
        "regulatory",
        "commodity",
        "climate",
        "infrastructure_energy",
    }
    assert set(cfg.sectors.keys()) == expected_sectors
    for sector_name, keyword_list in cfg.sectors.items():
        assert keyword_list, f"sector {sector_name!r} has no keywords"


def test_bundled_restricted_jurisdictions_config_loads() -> None:
    cfg = load_restricted_jurisdictions(_BUNDLED_RESTRICTED)
    assert isinstance(cfg, RestrictedJurisdictionsConfig)
    assert "US" in {j.upper() for j in cfg.restricted}


# -- error paths ----------------------------------------------------------


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_polymarket_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_polymarket_config(tmp_path / "missing.yaml")


def test_polymarket_config_empty_file_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "empty.yaml", "")
    with pytest.raises(ConfigError, match="empty"):
        load_polymarket_config(p)


def test_polymarket_config_top_level_must_be_mapping(tmp_path: Path) -> None:
    p = _write(tmp_path, "list.yaml", "- a\n- b\n")
    with pytest.raises(ConfigError, match="top-level mapping"):
        load_polymarket_config(p)


def test_polymarket_config_extra_fields_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "extra.yaml",
        "version: 1\nunknown_field: value\n",
    )
    with pytest.raises(ConfigError, match="validation failed"):
        load_polymarket_config(p)


def test_polymarket_config_invalid_time_of_day(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "bad_tod.yaml",
        """
version: 1
sync:
  markets:
    cadence: daily
    time_of_day: "25:00"
""".strip(),
    )
    with pytest.raises(ConfigError):
        load_polymarket_config(p)


def test_polymarket_config_minimum_interval_floor(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "tight_floor.yaml",
        """
version: 1
sync:
  prices:
    minimum_interval_seconds: 30
""".strip(),
    )
    with pytest.raises(ConfigError):
        load_polymarket_config(p)


def test_polymarket_config_inverted_backoff_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "bad_backoff.yaml",
        """
version: 1
rate_limit:
  backoff_base_seconds: 30.0
  backoff_max_seconds: 5.0
""".strip(),
    )
    with pytest.raises(ConfigError, match="backoff"):
        load_polymarket_config(p)


def test_sector_keywords_unknown_sector_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "bad_keywords.yaml",
        """
version: 1
sectors:
  not_a_sector:
    - foo
""".strip(),
    )
    with pytest.raises(ConfigError, match="unknown sector"):
        load_sector_keywords(p)


def test_sector_keywords_empty_list_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "empty_kw.yaml",
        """
version: 1
sectors:
  climate: []
""".strip(),
    )
    with pytest.raises(ConfigError, match="must not be empty"):
        load_sector_keywords(p)


def test_sector_keywords_duplicate_in_one_sector_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "dup_kw.yaml",
        """
version: 1
sectors:
  climate:
    - hurricane
    - Hurricane
""".strip(),
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_sector_keywords(p)


def test_restricted_jurisdictions_empty_list_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "empty_rj.yaml",
        """
version: 1
restricted: []
""".strip(),
    )
    with pytest.raises(ConfigError, match="at least one"):
        load_restricted_jurisdictions(p)


def test_restricted_jurisdictions_duplicate_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "dup_rj.yaml",
        """
version: 1
restricted:
  - US
  - us
""".strip(),
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_restricted_jurisdictions(p)


def test_polymarket_config_is_frozen() -> None:
    cfg = load_polymarket_config(_BUNDLED_POLYMARKET)
    # Pydantic v2 frozen models raise ValidationError on attribute set.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        cfg.version = 999  # type: ignore[misc]


def test_default_freshness_thresholds_match_oqpmc007() -> None:
    """OQ-PMC-007 resolution: 6h prices, 48h resolutions."""
    cfg = load_polymarket_config(_BUNDLED_POLYMARKET)
    assert cfg.freshness.prices_threshold_seconds == 21_600  # 6h
    assert cfg.freshness.resolutions_threshold_seconds == 172_800  # 48h
    assert cfg.freshness.markets_threshold_seconds == 172_800  # 48h
