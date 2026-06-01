"""Framing constants and linter wrapper for calibration_backtest renders (T-CB-033).

Centralises the standard disclaimer block (design §3.12) carried by every
operator-facing render (terminal, markdown, html, json) and the footer
note appended to the non-JSON renders, plus a thin wrapper around
``position_engine.frame.linter.check_text`` that pre-binds the absolute
forbidden-phrases catalog path.

The catalog is loaded **once** at module import using an absolute path
resolved against the repository root via
``Path(__file__).resolve().parents[3]``. The upstream linter's
``DEFAULT_CATALOG_PATH`` is CWD-relative; relying on it from a CLI
invoked from any working directory other than the repo root would
silently fall back to the 10-phrase default list and under-lint
calibration_backtest output. Resolving the absolute path at import time
avoids that silent under-coverage.

A startup assertion guards the failure mode: if the YAML cannot be
loaded (path layout broken, file missing, payload malformed) the
catalog falls through to the 10-phrase default and the assertion fires
loudly during import rather than during render time.

Public API:

* ``DISCLAIMER`` — the canonical disclaimer string, identical across
  ``position_engine`` and ``report_generator`` (REQ-PE-FRAME-001,
  REQ-RG-FRAME-004).
* ``FOOTER_NOTE`` — the non-JSON footer note text.
* ``check_cli_framing(text)`` — wrapper around
  ``position_engine.frame.linter.check_text`` that injects the absolute
  catalog. Lets ``ImperativeLanguageDetected`` propagate so callers can
  surface the offending phrase via the existing ``.phrase`` and
  ``.snippet`` attributes.
"""

from __future__ import annotations

from pathlib import Path

from razor_rooster.position_engine.frame.linter import (
    LinterCatalog,
    check_text,
)

DISCLAIMER: str = (
    "This output is decision-support calibration evidence, not a trading "
    "recommendation. Razor-Rooster does not place orders, execute trades, "
    "or size positions on behalf of the operator. Brier scores, "
    "reliability bins, and Kelly figures shown anywhere in the system are "
    "theoretical optima derived from the model's stated probabilities; "
    "whether the model's probabilities are accurate, whether displayed "
    "market prices are tradeable in the operator's chosen venue, and "
    "whether to act on any disagreement between model and market are "
    "operator judgments. Any per-cell, per-sector, or aggregate "
    "calibration result describes how the system would have performed if "
    "it had run historically; it does not predict future calibration. "
    "Paper-analysis remains the v1 contract regardless of calibration "
    "outcome."
)

FOOTER_NOTE: str = (
    "Disagreement between the model and a market is an observation. The "
    "operator decides, the system describes."
)

_CATALOG_PATH: Path = Path(__file__).resolve().parents[3] / "config" / "forbidden_phrases.yaml"
"""Absolute path to the shared forbidden-phrases catalog.

Resolved against the repository root at import time (``parents[3]``
walks ``calibration_backtest`` → ``razor_rooster`` → ``src`` → repo
root). Hard-coding the absolute path here avoids the CWD-relative
``DEFAULT_CATALOG_PATH`` fallback in the upstream linter, which would
silently load the 10-phrase default list when the CLI is invoked from
any directory other than the repo root.
"""

_LINTER_CATALOG: LinterCatalog = LinterCatalog.from_yaml(_CATALOG_PATH)
"""Forbidden-phrase catalog built once at import time.

The full ``config/forbidden_phrases.yaml`` carries roughly 42 phrases;
the linter's default fallback carries 10. The startup assertion below
distinguishes "loaded from YAML" from "fell back to defaults" so a
silent under-coverage regression fails loudly at import.
"""

assert len(_LINTER_CATALOG.phrases) > 10, (
    "calibration_backtest framing catalog under-loaded; check repo layout"
)


def check_cli_framing(text: str) -> None:
    """Lint ``text`` against the calibration_backtest forbidden catalog.

    Thin wrapper around
    :func:`razor_rooster.position_engine.frame.linter.check_text` that
    pre-binds the absolute :data:`_LINTER_CATALOG`. Lets
    :class:`razor_rooster.position_engine.frame.linter.ImperativeLanguageDetected`
    propagate to the caller so the offending phrase and snippet remain
    available on the exception's ``.phrase`` and ``.snippet`` attributes
    for structured error reporting.

    Args:
        text: Rendered analysis output to lint.

    Returns:
        None on clean output.

    Raises:
        ImperativeLanguageDetected: if any forbidden phrase matches.
    """

    check_text(text, catalog=_LINTER_CATALOG)


__all__ = [
    "DISCLAIMER",
    "FOOTER_NOTE",
    "check_cli_framing",
]
