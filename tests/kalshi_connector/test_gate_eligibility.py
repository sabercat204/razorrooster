"""T-KSI-020 — Kalshi eligibility allow-list gate acceptance tests.

Verifies:
- No jurisdiction → refusal naming the env var + config file.
- Jurisdiction declared but not on allow-list → refusal.
- Allowed jurisdiction → returns the normalized value.
- Case-insensitive matching.
- Operator config YAML fallback when env var absent.
- Env var wins on conflict.
- Cross-connector: setting OPERATOR_JURISDICTION='US' allows Kalshi
  but Polymarket refuses (US is on Polymarket's deny-list); setting
  'DE' allows Polymarket but Kalshi refuses.
- Config-load failure raises a clear EligibilityRefusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from razor_rooster.kalshi_connector.config.loader import (
    KalshiAllowedJurisdictionsConfig,
)
from razor_rooster.kalshi_connector.gates.eligibility import (
    EligibilityRefusal,
    check_eligibility,
)
from razor_rooster.polymarket_connector.config.loader import (
    RestrictedJurisdictionsConfig,
)
from razor_rooster.polymarket_connector.gates.geo import (
    StartupRefusal,
    check_jurisdiction,
)


@pytest.fixture
def allowed_us_only() -> KalshiAllowedJurisdictionsConfig:
    return KalshiAllowedJurisdictionsConfig(version=1, allowed=["US"])


@pytest.fixture
def allowed_us_and_pt() -> KalshiAllowedJurisdictionsConfig:
    return KalshiAllowedJurisdictionsConfig(version=1, allowed=["US", "PT"])


def _absent_operator_config(tmp_path: Path) -> Path:
    """Return a path that does not exist."""
    return tmp_path / "operator_does_not_exist.yaml"


def test_refuses_when_jurisdiction_not_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    with pytest.raises(EligibilityRefusal, match="OPERATOR_JURISDICTION is not configured"):
        check_eligibility(
            operator_config_path=_absent_operator_config(tmp_path),
            allowed=allowed_us_only,
        )


def test_refuses_when_jurisdiction_not_on_allow_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    monkeypatch.setenv("OPERATOR_JURISDICTION", "DE")
    with pytest.raises(EligibilityRefusal, match="not on the Kalshi allow-list"):
        check_eligibility(
            operator_config_path=_absent_operator_config(tmp_path),
            allowed=allowed_us_only,
        )


def test_accepts_allowed_jurisdiction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    monkeypatch.setenv("OPERATOR_JURISDICTION", "US")
    accepted = check_eligibility(
        operator_config_path=_absent_operator_config(tmp_path),
        allowed=allowed_us_only,
    )
    assert accepted == "US"


def test_case_insensitive_matching(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    monkeypatch.setenv("OPERATOR_JURISDICTION", "us")
    accepted = check_eligibility(
        operator_config_path=_absent_operator_config(tmp_path),
        allowed=allowed_us_only,
    )
    assert accepted == "US"


def test_reads_jurisdiction_from_operator_config_yaml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_and_pt: KalshiAllowedJurisdictionsConfig,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    cfg = tmp_path / "operator.yaml"
    cfg.write_text("jurisdiction: PT\n", encoding="utf-8")
    accepted = check_eligibility(
        operator_config_path=cfg,
        allowed=allowed_us_and_pt,
    )
    assert accepted == "PT"


def test_env_var_wins_over_operator_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    cfg = tmp_path / "operator.yaml"
    cfg.write_text("jurisdiction: PT\n", encoding="utf-8")
    monkeypatch.setenv("OPERATOR_JURISDICTION", "US")
    accepted = check_eligibility(
        operator_config_path=cfg,
        allowed=allowed_us_only,
    )
    assert accepted == "US"


def test_disallowed_jurisdiction_in_operator_config_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    cfg = tmp_path / "operator.yaml"
    cfg.write_text("jurisdiction: KP\n", encoding="utf-8")
    with pytest.raises(EligibilityRefusal, match="not on the Kalshi allow-list"):
        check_eligibility(
            operator_config_path=cfg,
            allowed=allowed_us_only,
        )


def test_invalid_yaml_in_operator_config_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    cfg = tmp_path / "operator.yaml"
    cfg.write_text("jurisdiction: : not yaml\n", encoding="utf-8")
    with pytest.raises(EligibilityRefusal, match="invalid YAML"):
        check_eligibility(
            operator_config_path=cfg,
            allowed=allowed_us_only,
        )


def test_non_string_jurisdiction_in_operator_config_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    cfg = tmp_path / "operator.yaml"
    cfg.write_text("jurisdiction: 42\n", encoding="utf-8")
    with pytest.raises(EligibilityRefusal, match="must be a string"):
        check_eligibility(
            operator_config_path=cfg,
            allowed=allowed_us_only,
        )


def test_empty_jurisdiction_string_in_operator_config_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)
    cfg = tmp_path / "operator.yaml"
    cfg.write_text("jurisdiction: ''\n", encoding="utf-8")
    with pytest.raises(EligibilityRefusal, match="not configured"):
        check_eligibility(
            operator_config_path=cfg,
            allowed=allowed_us_only,
        )


def test_loads_allowed_config_from_disk_when_no_arg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPERATOR_JURISDICTION", "US")
    allowed_yaml = tmp_path / "kalshi_allowed.yaml"
    allowed_yaml.write_text(
        "version: 1\nallowed:\n  - US\n  - PT\n",
        encoding="utf-8",
    )
    accepted = check_eligibility(
        operator_config_path=_absent_operator_config(tmp_path),
        allowed_config_path=allowed_yaml,
    )
    assert accepted == "US"


def test_missing_allowed_config_raises_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPERATOR_JURISDICTION", "FR")
    with pytest.raises(EligibilityRefusal, match="failed to load"):
        check_eligibility(
            operator_config_path=_absent_operator_config(tmp_path),
            allowed_config_path=tmp_path / "missing.yaml",
        )


# -- cross-connector behavior ----------------------------------------------


def test_cross_connector_us_allows_kalshi_blocks_polymarket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    """OPERATOR_JURISDICTION='US' allows Kalshi but blocks Polymarket.

    The same operator declaration drives both gates; the inverse
    postures produce inverse outcomes.
    """
    monkeypatch.setenv("OPERATOR_JURISDICTION", "US")
    # Polymarket deny-list includes US.
    polymarket_restricted = RestrictedJurisdictionsConfig(version=1, restricted=["US", "FR"])
    # Kalshi allow-list is US-only.
    accepted = check_eligibility(
        operator_config_path=_absent_operator_config(tmp_path),
        allowed=allowed_us_only,
    )
    assert accepted == "US"
    with pytest.raises(StartupRefusal, match="restricted list"):
        check_jurisdiction(
            operator_config_path=_absent_operator_config(tmp_path),
            restricted=polymarket_restricted,
        )


def test_cross_connector_de_allows_polymarket_blocks_kalshi(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowed_us_only: KalshiAllowedJurisdictionsConfig,
) -> None:
    """OPERATOR_JURISDICTION='DE' allows Polymarket but blocks Kalshi."""
    monkeypatch.setenv("OPERATOR_JURISDICTION", "DE")
    polymarket_restricted = RestrictedJurisdictionsConfig(version=1, restricted=["US", "FR"])
    accepted = check_jurisdiction(
        operator_config_path=_absent_operator_config(tmp_path),
        restricted=polymarket_restricted,
    )
    assert accepted == "DE"
    with pytest.raises(EligibilityRefusal, match="not on the Kalshi allow-list"):
        check_eligibility(
            operator_config_path=_absent_operator_config(tmp_path),
            allowed=allowed_us_only,
        )
