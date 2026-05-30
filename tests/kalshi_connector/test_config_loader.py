"""T-KSI-002 — Kalshi config loader acceptance tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from razor_rooster.kalshi_connector.config.loader import (
    KalshiAllowedJurisdictionsConfig,
    KalshiConfig,
    KalshiConfigError,
    KalshiSectorKeywordsConfig,
    load_kalshi_allowed_jurisdictions,
    load_kalshi_config,
    load_kalshi_sector_keywords,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_KALSHI = _REPO_ROOT / "config" / "kalshi.yaml"
_BUNDLED_KEYWORDS = _REPO_ROOT / "config" / "kalshi_sector_keywords.yaml"
_BUNDLED_ALLOWED = _REPO_ROOT / "config" / "kalshi_allowed_jurisdictions.yaml"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# --- bundled-config sanity tests -----------------------------------------


def test_bundled_kalshi_config_loads() -> None:
    cfg = load_kalshi_config(_BUNDLED_KALSHI)
    assert isinstance(cfg, KalshiConfig)
    assert cfg.version >= 1
    assert cfg.tier == "Basic"
    assert cfg.base_url.startswith("https://external-api.kalshi.com")
    assert cfg.sync.prices.default_cadence == "every_30min"
    assert cfg.sync.prices.minimum_interval_seconds >= 60
    assert cfg.sync.cutoff.cadence == "every_cycle"
    assert cfg.tos_url.startswith("https://")


def test_bundled_kalshi_keywords_loads() -> None:
    cfg = load_kalshi_sector_keywords(_BUNDLED_KEYWORDS)
    assert isinstance(cfg, KalshiSectorKeywordsConfig)
    # All eight Razor sectors plus out_of_scope present.
    expected = {
        "public_health",
        "geopolitical",
        "regulatory",
        "commodity",
        "climate",
        "infrastructure_energy",
        "macroeconomic",
        "cross_cutting",
        "out_of_scope",
    }
    assert set(cfg.sectors.keys()) == expected
    assert "CPI" in cfg.sectors["macroeconomic"]
    assert "Super Bowl" in cfg.sectors["out_of_scope"]


def test_bundled_kalshi_allowed_jurisdictions_loads() -> None:
    cfg = load_kalshi_allowed_jurisdictions(_BUNDLED_ALLOWED)
    assert isinstance(cfg, KalshiAllowedJurisdictionsConfig)
    assert "US" in cfg.allowed


# --- kalshi.yaml validation ----------------------------------------------


def test_kalshi_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(KalshiConfigError, match="not found"):
        load_kalshi_config(tmp_path / "missing.yaml")


def test_kalshi_config_empty_file_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "empty.yaml", "")
    with pytest.raises(KalshiConfigError, match="empty"):
        load_kalshi_config(p)


def test_kalshi_config_top_level_must_be_mapping(tmp_path: Path) -> None:
    p = _write(tmp_path, "list.yaml", "- a\n- b\n")
    with pytest.raises(KalshiConfigError, match="top-level mapping"):
        load_kalshi_config(p)


def test_kalshi_config_extra_fields_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "extra.yaml",
        "version: 1\nsync:\n  unknown_field: 5\n",
    )
    with pytest.raises(KalshiConfigError, match="validation failed"):
        load_kalshi_config(p)


def test_kalshi_config_invalid_time_of_day(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "bad_time.yaml",
        """
version: 1
sync:
  series:
    cadence: daily
    time_of_day: "25:99"
""",
    )
    with pytest.raises(KalshiConfigError, match="wall-clock time"):
        load_kalshi_config(p)


def test_kalshi_config_malformed_time_of_day(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "bad_format.yaml",
        """
version: 1
sync:
  series:
    cadence: daily
    time_of_day: "noon"
""",
    )
    with pytest.raises(KalshiConfigError, match="HH:MM"):
        load_kalshi_config(p)


def test_kalshi_config_minimum_interval_floor(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "min_interval.yaml",
        """
version: 1
sync:
  prices:
    default_cadence: every_30min
    minimum_interval_seconds: 30
""",
    )
    with pytest.raises(KalshiConfigError, match="validation failed"):
        load_kalshi_config(p)


def test_kalshi_config_inverted_backoff_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "backoff.yaml",
        """
version: 1
rate_limit:
  backoff_base_seconds: 60
  backoff_max_seconds: 5
""",
    )
    with pytest.raises(KalshiConfigError, match="backoff_base_seconds"):
        load_kalshi_config(p)


def test_kalshi_config_unknown_tier_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "tier.yaml",
        "version: 1\ntier: Platinum\n",
    )
    with pytest.raises(KalshiConfigError, match="validation failed"):
        load_kalshi_config(p)


def test_kalshi_config_demo_url_refused(tmp_path: Path) -> None:
    """Demo environment is reserved for v2 trading work; v1 production-only."""
    p = _write(
        tmp_path,
        "demo.yaml",
        "version: 1\nbase_url: https://external-api.demo.kalshi.co/trade-api/v2\n",
    )
    with pytest.raises(KalshiConfigError, match="demo environment"):
        load_kalshi_config(p)


def test_kalshi_config_non_https_base_url_refused(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "bad_scheme.yaml",
        "version: 1\nbase_url: ftp://kalshi.com/api\n",
    )
    with pytest.raises(KalshiConfigError, match="https://"):
        load_kalshi_config(p)


def test_kalshi_config_missing_tier_in_budget_map_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "missing_tier.yaml",
        """
version: 1
rate_limit:
  tier_budget_tokens_per_sec:
    Basic: 200
    Advanced: 300
""",
    )
    with pytest.raises(KalshiConfigError, match="missing"):
        load_kalshi_config(p)


def test_kalshi_config_headroom_helper() -> None:
    cfg = load_kalshi_config(_BUNDLED_KALSHI)
    # Basic tier 200 tokens/sec * 0.5 headroom = 100 tokens/sec.
    assert cfg.headroom_tokens_per_sec() == pytest.approx(100.0)


# --- kalshi_sector_keywords.yaml validation ------------------------------


def test_kalshi_keywords_unknown_sector_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "unknown.yaml",
        """
version: 1
sectors:
  invented_sector:
    - foo
""",
    )
    with pytest.raises(KalshiConfigError, match="unknown sector names"):
        load_kalshi_sector_keywords(p)


def test_kalshi_keywords_empty_list_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "empty_list.yaml",
        """
version: 1
sectors:
  public_health: []
""",
    )
    with pytest.raises(KalshiConfigError, match="must not be empty"):
        load_kalshi_sector_keywords(p)


def test_kalshi_keywords_duplicate_in_one_sector_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "dupes.yaml",
        """
version: 1
sectors:
  public_health:
    - PHEIC
    - pheic
""",
    )
    with pytest.raises(KalshiConfigError, match="duplicate keyword"):
        load_kalshi_sector_keywords(p)


def test_kalshi_keywords_out_of_scope_is_allowed_sector(tmp_path: Path) -> None:
    """OQ-KSI-001: out_of_scope is a valid sector key for Kalshi."""
    p = _write(
        tmp_path,
        "oos.yaml",
        """
version: 1
sectors:
  out_of_scope:
    - Super Bowl
""",
    )
    cfg = load_kalshi_sector_keywords(p)
    assert "out_of_scope" in cfg.sectors


# --- kalshi_allowed_jurisdictions.yaml validation ------------------------


def test_kalshi_allowed_empty_list_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "empty.yaml",
        "version: 1\nallowed: []\n",
    )
    with pytest.raises(KalshiConfigError, match="at least one entry"):
        load_kalshi_allowed_jurisdictions(p)


def test_kalshi_allowed_duplicate_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "dupes.yaml",
        """
version: 1
allowed:
  - US
  - us
""",
    )
    with pytest.raises(KalshiConfigError, match="duplicate"):
        load_kalshi_allowed_jurisdictions(p)


def test_kalshi_allowed_blank_entry_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "blank.yaml",
        """
version: 1
allowed:
  - "  "
""",
    )
    with pytest.raises(KalshiConfigError, match="empty strings"):
        load_kalshi_allowed_jurisdictions(p)


# --- frozen-model invariant ----------------------------------------------


def test_kalshi_config_is_frozen() -> None:
    cfg = load_kalshi_config(_BUNDLED_KALSHI)
    # Pydantic v2 frozen models raise ValidationError on attribute set.
    with pytest.raises(ValidationError):
        cfg.tier = "Premier"  # type: ignore[misc]


def test_default_freshness_thresholds_match_oqksi() -> None:
    """Design §4: 3h prices (tighter than Polymarket's 6h), 48h settlements."""
    cfg = load_kalshi_config(_BUNDLED_KALSHI)
    assert cfg.freshness.prices_threshold_seconds == 10_800
    assert cfg.freshness.settlements_threshold_seconds == 172_800
    assert cfg.freshness.markets_threshold_seconds == 172_800
