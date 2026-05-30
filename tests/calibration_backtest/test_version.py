"""T-CB-002 — version constant and system_revision capture tests."""

from __future__ import annotations

import re
import subprocess
from typing import Any

import pytest

from razor_rooster.calibration_backtest import CALIBRATION_BACKTEST_VERSION
from razor_rooster.calibration_backtest import version as version_module
from razor_rooster.calibration_backtest.version import (
    SYSTEM_REVISION_FALLBACK,
    get_system_revision,
)

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def test_version_format() -> None:
    """``CALIBRATION_BACKTEST_VERSION`` is a semver string of form X.Y.Z."""
    assert _SEMVER_RE.match(CALIBRATION_BACKTEST_VERSION) is not None


def test_version_is_string() -> None:
    assert isinstance(CALIBRATION_BACKTEST_VERSION, str)


def test_get_system_revision_returns_str() -> None:
    """The helper returns a non-empty string regardless of git state."""
    revision = get_system_revision()
    assert isinstance(revision, str)
    assert revision != ""


def test_get_system_revision_is_idempotent() -> None:
    """Two consecutive calls within the same git state return the same value."""
    first = get_system_revision()
    second = get_system_revision()
    assert first == second


def test_get_system_revision_handles_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the git binary is missing, the helper returns the fallback."""

    def _raise_filenotfound(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("git binary missing")

    monkeypatch.setattr(
        f"{version_module.__name__}.subprocess.run",
        _raise_filenotfound,
    )
    assert get_system_revision() == SYSTEM_REVISION_FALLBACK


def test_get_system_revision_handles_called_process_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If git exits non-zero (e.g. not a repo), the helper returns the fallback."""

    def _raise_called_process_error(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(returncode=128, cmd=["git", "rev-parse", "HEAD"])

    monkeypatch.setattr(
        f"{version_module.__name__}.subprocess.run",
        _raise_called_process_error,
    )
    assert get_system_revision() == SYSTEM_REVISION_FALLBACK
