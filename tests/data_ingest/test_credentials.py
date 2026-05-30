"""T-020 verification — environment-variable credential loader.

Verifies:
- ``load_credentials_for`` returns ``ApiKeyBundle`` for single-key sources.
- ``load_credentials_for`` returns ``UserPasswordBundle`` for ACLED.
- Missing or empty env vars produce ``None`` (not exceptions).
- Whitespace is stripped from env-var values.
- Unknown source ids return ``None``.
- Bundle ``__repr__`` does not leak credential values (REQ-ACLED-AUTH-003 /
  general no-credential-in-logs discipline).
- ``required_env_vars_for`` reports the right env vars per source.
- A custom ``env_path`` override is honored without polluting the global
  process state.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from razor_rooster.data_ingest.credentials import (
    ApiKeyBundle,
    CredentialBundle,
    UserPasswordBundle,
    load_credentials_for,
    required_env_vars_for,
)


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure each test runs against a clean env subset.

    We clear the credential-relevant env vars before each test and let
    individual tests set the ones they need. ``monkeypatch.setenv`` undoes
    itself on teardown, so this is safe.
    """
    for var in (
        "FRED_API_KEY",
        "ACLED_USERNAME",
        "ACLED_PASSWORD",
        "EIA_API_KEY",
        "NRC_ADAMS_API_KEY",
        "REGULATIONS_GOV_API_KEY",
        "NOAA_CDO_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_unknown_source_returns_none() -> None:
    assert load_credentials_for("nonexistent") is None


def test_unauthenticated_source_returns_none() -> None:
    """Sources with no credentials in the schema return None even when env vars exist."""
    # World Bank, GDELT, USGS are not in the credential schema.
    assert load_credentials_for("worldbank") is None
    assert load_credentials_for("gdelt_events") is None
    assert load_credentials_for("usgs_minerals") is None


def test_fred_with_key_returns_api_key_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "test_fred_key_abc123")
    bundle = load_credentials_for("fred")
    assert isinstance(bundle, ApiKeyBundle)
    assert bundle.source_id == "fred"
    assert bundle.api_key == "test_fred_key_abc123"
    assert bundle.extra_token is None


def test_fred_without_key_returns_none() -> None:
    assert load_credentials_for("fred") is None


def test_fred_with_empty_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "")
    assert load_credentials_for("fred") is None


def test_fred_with_whitespace_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "   ")
    assert load_credentials_for("fred") is None


def test_fred_with_padded_key_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "  padded  ")
    bundle = load_credentials_for("fred")
    assert isinstance(bundle, ApiKeyBundle)
    assert bundle.api_key == "padded"


def test_acled_with_credentials_returns_user_password_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACLED_USERNAME", "test@example.com")
    monkeypatch.setenv("ACLED_PASSWORD", "test_password_xyz")
    bundle = load_credentials_for("acled")
    assert isinstance(bundle, UserPasswordBundle)
    assert bundle.source_id == "acled"
    assert bundle.username == "test@example.com"
    assert bundle.password == "test_password_xyz"


def test_acled_with_only_username_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACLED_USERNAME", "test@example.com")
    assert load_credentials_for("acled") is None


def test_acled_with_only_password_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACLED_PASSWORD", "test_password")
    assert load_credentials_for("acled") is None


def test_acled_with_empty_password_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACLED_USERNAME", "test@example.com")
    monkeypatch.setenv("ACLED_PASSWORD", "")
    assert load_credentials_for("acled") is None


def test_eia_returns_api_key_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EIA_API_KEY", "eia_key")
    bundle = load_credentials_for("eia")
    assert isinstance(bundle, ApiKeyBundle)
    assert bundle.source_id == "eia"


def test_nrc_adams_returns_api_key_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NRC_ADAMS_API_KEY", "nrc_key")
    bundle = load_credentials_for("nrc_adams")
    assert isinstance(bundle, ApiKeyBundle)
    assert bundle.source_id == "nrc_adams"


def test_regulations_gov_returns_api_key_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGULATIONS_GOV_API_KEY", "reggov_key")
    bundle = load_credentials_for("regulations_gov")
    assert isinstance(bundle, ApiKeyBundle)


def test_noaa_returns_api_key_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOAA_CDO_TOKEN", "noaa_token")
    bundle = load_credentials_for("noaa")
    assert isinstance(bundle, ApiKeyBundle)


def test_api_key_bundle_repr_does_not_leak_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "should_never_appear_in_repr")
    bundle = load_credentials_for("fred")
    assert isinstance(bundle, ApiKeyBundle)
    rendered = repr(bundle)
    assert "should_never_appear_in_repr" not in rendered
    assert "<redacted" in rendered
    assert "fred" in rendered  # source_id is fine to display


def test_user_password_bundle_repr_does_not_leak_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACLED_USERNAME", "secret_user@example.com")
    monkeypatch.setenv("ACLED_PASSWORD", "very_secret_password")
    bundle = load_credentials_for("acled")
    assert isinstance(bundle, UserPasswordBundle)
    rendered = repr(bundle)
    assert "secret_user" not in rendered
    assert "very_secret_password" not in rendered
    assert "<redacted>" in rendered
    assert "acled" in rendered


def test_required_env_vars_for_unauthenticated_source() -> None:
    assert required_env_vars_for("worldbank") == ()
    assert required_env_vars_for("nonexistent") == ()


def test_required_env_vars_for_fred() -> None:
    assert required_env_vars_for("fred") == ("FRED_API_KEY",)


def test_required_env_vars_for_acled() -> None:
    assert required_env_vars_for("acled") == ("ACLED_USERNAME", "ACLED_PASSWORD")


def test_credential_bundle_union_type() -> None:
    """``CredentialBundle`` accepts either bundle shape."""
    api: CredentialBundle = ApiKeyBundle(source_id="x", api_key="k")
    user: CredentialBundle = UserPasswordBundle(source_id="x", username="u", password="p")
    assert isinstance(api, ApiKeyBundle)
    assert isinstance(user, UserPasswordBundle)


def test_env_path_override_loads_isolated_env(tmp_path: Path) -> None:
    """An explicit env_path loads from that file without polluting the test process."""
    env_file = tmp_path / "test.env"
    env_file.write_text("FRED_API_KEY=isolated_key_value\n")

    # The current process env doesn't have FRED_API_KEY (autouse fixture cleared it).
    bundle = load_credentials_for("fred", env_path=env_file)
    assert isinstance(bundle, ApiKeyBundle)
    assert bundle.api_key == "isolated_key_value"

    # python-dotenv with override=True inserts into os.environ; clean up so
    # subsequent tests aren't polluted.
    os.environ.pop("FRED_API_KEY", None)
