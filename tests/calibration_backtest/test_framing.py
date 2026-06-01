"""T-CB-033 — frame.py: DISCLAIMER, FOOTER_NOTE, and linter wrapper tests.

These tests cover the four critical guarantees of the framing module:

* The disclaimer and footer constants are non-empty (so renderers cannot
  silently skip them).
* ``check_cli_framing`` lets clean text pass and raises
  :class:`ImperativeLanguageDetected` (NOT a calibration_backtest-local
  exception) on a known forbidden phrase from
  ``config/forbidden_phrases.yaml``.
* The propagated exception preserves the upstream ``.phrase`` and
  ``.snippet`` attributes so callers can surface structured error
  context.
* The catalog is loaded from the absolute repo-root path at import
  time, NOT the upstream linter's CWD-relative
  ``DEFAULT_CATALOG_PATH``. Reimporting the module from a different
  CWD must still yield the full ~42-phrase YAML, not the 10-phrase
  fallback.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from razor_rooster.calibration_backtest import frame as frame_module
from razor_rooster.calibration_backtest.frame import (
    DISCLAIMER,
    FOOTER_NOTE,
    check_cli_framing,
)
from razor_rooster.position_engine.frame.linter import ImperativeLanguageDetected


def test_disclaimer_constant_nonempty() -> None:
    """DISCLAIMER must be a non-empty string the renderers can embed."""

    assert isinstance(DISCLAIMER, str)
    assert DISCLAIMER.strip() != ""


def test_footer_note_constant_nonempty() -> None:
    """FOOTER_NOTE must be a non-empty string the renderers can append."""

    assert isinstance(FOOTER_NOTE, str)
    assert FOOTER_NOTE.strip() != ""


def test_check_cli_framing_passes_clean_text() -> None:
    """Clean conditional-language text must not raise."""

    clean = (
        "If the operator chose to consider this evidence, the model would "
        "describe a calibration outcome that the operator could weigh."
    )
    # Should return None without raising; absence of an exception is the
    # contract (mypy: ``check_cli_framing`` returns ``None`` so we cannot
    # assert on its return value directly).
    check_cli_framing(clean)


def test_check_cli_framing_raises_on_forbidden_phrase() -> None:
    """A known phrase from the YAML must trigger ImperativeLanguageDetected."""

    text = "Per the latest data, you should buy the dip immediately."
    with pytest.raises(ImperativeLanguageDetected):
        check_cli_framing(text)


def test_imperative_language_detected_has_phrase_attribute() -> None:
    """The propagated exception must carry .phrase and .snippet."""

    text = "The system told the operator to go long without delay."
    with pytest.raises(ImperativeLanguageDetected) as excinfo:
        check_cli_framing(text)
    err = excinfo.value
    assert err.phrase == "go long"
    assert isinstance(err.snippet, str)
    assert "go long" in err.snippet.lower()


def test_cwd_independence(tmp_path: Path) -> None:
    """Catalog must load fully even when CWD is not the repo root.

    The upstream linter's ``DEFAULT_CATALOG_PATH`` is CWD-relative; if
    the calibration_backtest frame module relied on it, importing from
    a non-repo-root CWD would silently fall back to the 10-phrase
    default list. The module instead resolves an absolute Path against
    ``parents[3]`` so phrase coverage is stable across CWDs.
    """

    original_cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        reloaded = importlib.reload(frame_module)
        assert len(reloaded._LINTER_CATALOG.phrases) > 10
    finally:
        os.chdir(original_cwd)
        # Restore the canonical module state for downstream tests.
        importlib.reload(frame_module)


def test_module_catalog_loaded_at_import() -> None:
    """The module-level catalog must be loaded with the full YAML phrase set."""

    assert len(frame_module._LINTER_CATALOG.phrases) > 10
