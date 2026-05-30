"""T-PMC-020 — geo-restriction gate tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from razor_rooster.polymarket_connector.config.loader import (
    RestrictedJurisdictionsConfig,
)
from razor_rooster.polymarket_connector.gates.geo import (
    StartupRefusal,
    check_jurisdiction,
)


@pytest.fixture
def restricted_config() -> RestrictedJurisdictionsConfig:
    return RestrictedJurisdictionsConfig(
        version=1,
        restricted=["US", "CU", "IR", "KP", "SY", "RU", "BY"],
    )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any pre-existing OPERATOR_JURISDICTION so tests start clean."""
    monkeypatch.delenv("OPERATOR_JURISDICTION", raising=False)


def _absent_operator_config(tmp_path: Path) -> Path:
    return tmp_path / "operator-not-present.yaml"


def test_refusal_when_no_jurisdiction_configured(
    tmp_path: Path,
    restricted_config: RestrictedJurisdictionsConfig,
) -> None:
    with pytest.raises(StartupRefusal, match="OPERATOR_JURISDICTION is not configured"):
        check_jurisdiction(
            operator_config_path=_absent_operator_config(tmp_path),
            restricted=restricted_config,
        )


def test_refusal_for_restricted_jurisdiction_via_env(
    tmp_path: Path,
    restricted_config: RestrictedJurisdictionsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATOR_JURISDICTION", "US")
    with pytest.raises(StartupRefusal, match="restricted list"):
        check_jurisdiction(
            operator_config_path=_absent_operator_config(tmp_path),
            restricted=restricted_config,
        )


def test_pass_for_permitted_jurisdiction_via_env(
    tmp_path: Path,
    restricted_config: RestrictedJurisdictionsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATOR_JURISDICTION", "DE")
    accepted = check_jurisdiction(
        operator_config_path=_absent_operator_config(tmp_path),
        restricted=restricted_config,
    )
    assert accepted == "DE"


def test_case_insensitive_match(
    tmp_path: Path,
    restricted_config: RestrictedJurisdictionsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'us' lowercase still hits the US entry."""
    monkeypatch.setenv("OPERATOR_JURISDICTION", "us")
    with pytest.raises(StartupRefusal, match="restricted list"):
        check_jurisdiction(
            operator_config_path=_absent_operator_config(tmp_path),
            restricted=restricted_config,
        )


def test_operator_yaml_provides_jurisdiction(
    tmp_path: Path,
    restricted_config: RestrictedJurisdictionsConfig,
) -> None:
    cfg = tmp_path / "operator.yaml"
    cfg.write_text("jurisdiction: PT\n", encoding="utf-8")
    accepted = check_jurisdiction(
        operator_config_path=cfg,
        restricted=restricted_config,
    )
    assert accepted == "PT"


def test_env_var_wins_over_operator_yaml(
    tmp_path: Path,
    restricted_config: RestrictedJurisdictionsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = tmp_path / "operator.yaml"
    cfg.write_text("jurisdiction: PT\n", encoding="utf-8")
    monkeypatch.setenv("OPERATOR_JURISDICTION", "DE")
    accepted = check_jurisdiction(
        operator_config_path=cfg,
        restricted=restricted_config,
    )
    assert accepted == "DE"


def test_operator_yaml_with_restricted_value_is_refused(
    tmp_path: Path,
    restricted_config: RestrictedJurisdictionsConfig,
) -> None:
    cfg = tmp_path / "operator.yaml"
    cfg.write_text("jurisdiction: KP\n", encoding="utf-8")
    with pytest.raises(StartupRefusal, match="restricted list"):
        check_jurisdiction(
            operator_config_path=cfg,
            restricted=restricted_config,
        )


def test_invalid_yaml_in_operator_config_refuses(
    tmp_path: Path,
    restricted_config: RestrictedJurisdictionsConfig,
) -> None:
    cfg = tmp_path / "operator.yaml"
    cfg.write_text("jurisdiction: : not yaml\n", encoding="utf-8")
    with pytest.raises(StartupRefusal, match="invalid YAML"):
        check_jurisdiction(
            operator_config_path=cfg,
            restricted=restricted_config,
        )


def test_non_string_jurisdiction_refused(
    tmp_path: Path,
    restricted_config: RestrictedJurisdictionsConfig,
) -> None:
    cfg = tmp_path / "operator.yaml"
    cfg.write_text("jurisdiction: 42\n", encoding="utf-8")
    with pytest.raises(StartupRefusal, match="must be a string"):
        check_jurisdiction(
            operator_config_path=cfg,
            restricted=restricted_config,
        )


def test_empty_jurisdiction_string_treated_as_missing(
    tmp_path: Path,
    restricted_config: RestrictedJurisdictionsConfig,
) -> None:
    cfg = tmp_path / "operator.yaml"
    cfg.write_text("jurisdiction: ''\n", encoding="utf-8")
    with pytest.raises(StartupRefusal, match="not configured"):
        check_jurisdiction(
            operator_config_path=cfg,
            restricted=restricted_config,
        )


def test_loads_restricted_config_from_path_when_not_injected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `restricted=None`, the gate reads from the named config path."""
    monkeypatch.setenv("OPERATOR_JURISDICTION", "FR")
    restricted_yaml = tmp_path / "restricted.yaml"
    restricted_yaml.write_text(
        "version: 1\nrestricted:\n  - US\n  - CU\n",
        encoding="utf-8",
    )
    accepted = check_jurisdiction(
        operator_config_path=_absent_operator_config(tmp_path),
        restricted_config_path=restricted_yaml,
    )
    assert accepted == "FR"


def test_missing_restricted_config_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATOR_JURISDICTION", "FR")
    with pytest.raises(StartupRefusal, match="failed to load"):
        check_jurisdiction(
            operator_config_path=_absent_operator_config(tmp_path),
            restricted_config_path=tmp_path / "missing.yaml",
        )
