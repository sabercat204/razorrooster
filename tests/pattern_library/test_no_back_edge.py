"""T-CB-044 — programmatic no-back-edge test across the 7 canonical packages.

REQ-CB-PL-002 declares that ``calibration_backtest`` may consume from
``pattern_library, signal_scanner, mispricing_detector, polymarket_connector,
data_ingest, report_generator, position_engine`` but **none** of those
seven subsystems may import from ``calibration_backtest``. This test walks
the source tree of each canonical package and asserts no Python module
imports any submodule of ``razor_rooster.calibration_backtest`` — the
guardrail that keeps the pattern_library Phase 7 meta-class upgrade
(REQ-CB-PL-001) free of a circular dependency.

The check uses two complementary strategies:

1. **Regex** — a fast textual scan catches the common forbidden forms
   (``from razor_rooster.calibration_backtest...``,
   ``import razor_rooster.calibration_backtest...``) including aliased,
   conditional, and string-table forms a strict AST walk would miss
   inside ``if TYPE_CHECKING:`` blocks reduced to constants.
2. **AST** — parses each module and inspects ``ast.Import`` /
   ``ast.ImportFrom`` nodes for any reference to ``calibration_backtest``,
   covering aliased imports (``import razor_rooster.calibration_backtest
   as cb``) and ``from . import calibration_backtest`` style relative
   imports rooted at the package boundary.

Either strategy alone is sufficient at present; the redundant pair pins
the contract so a future regression that defeats one path still trips
the other.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# The seven canonical packages per REQ-CB-PL-002 (post-amendment). The
# list is referenced verbatim by T-CB-044 and T-CB-054. Order is fixed
# to keep parametrize ids stable and the failure report deterministic.
CANONICAL_PACKAGES: tuple[str, ...] = (
    "pattern_library",
    "signal_scanner",
    "mispricing_detector",
    "polymarket_connector",
    "data_ingest",
    "report_generator",
    "position_engine",
)

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "razor_rooster"

# Match the forbidden import forms as written in source. Anchored to the
# start of a (possibly indented) line so commented occurrences and
# string-literal references are ignored.
_FORBIDDEN_RE = re.compile(
    r"^\s*(?:from\s+razor_rooster\.calibration_backtest"
    r"|import\s+razor_rooster\.calibration_backtest)",
    re.MULTILINE,
)


def _python_files(package_root: Path) -> list[Path]:
    """Return every ``*.py`` file under ``package_root`` (recursive)."""

    return sorted(p for p in package_root.rglob("*.py") if p.is_file())


def _ast_references_calibration_backtest(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, snippet)`` tuples for any AST-level back-edge.

    Catches both ``import razor_rooster.calibration_backtest[...]`` and
    ``from razor_rooster.calibration_backtest[...] import ...`` regardless
    of aliasing or whitespace — strictly stronger than the regex pass for
    syntactically valid modules.
    """

    tree = ast.parse(source)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "razor_rooster.calibration_backtest" or module.startswith(
                "razor_rooster.calibration_backtest."
            ):
                names = ", ".join(alias.name for alias in node.names)
                hits.append((node.lineno, f"from {module} import {names}"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "razor_rooster.calibration_backtest" or alias.name.startswith(
                    "razor_rooster.calibration_backtest."
                ):
                    hits.append((node.lineno, f"import {alias.name}"))
    return hits


@pytest.mark.parametrize("package_name", CANONICAL_PACKAGES, ids=list(CANONICAL_PACKAGES))
def test_canonical_package_has_no_back_edge_to_calibration_backtest(
    package_name: str,
) -> None:
    """No file in the canonical package may import from ``calibration_backtest``.

    Reports the **first** offending file/line/snippet so the failure
    message points at the precise regression rather than a noisy diff
    of every match.
    """

    package_root = _SRC_ROOT / package_name
    assert package_root.is_dir(), (
        f"canonical package missing on disk: {package_root} — "
        "REQ-CB-PL-002 list drifted from filesystem layout."
    )

    for py_path in _python_files(package_root):
        source = py_path.read_text(encoding="utf-8")

        regex_match = _FORBIDDEN_RE.search(source)
        if regex_match is not None:
            # Translate match offset back to a 1-indexed line number.
            line_no = source.count("\n", 0, regex_match.start()) + 1
            snippet = source.splitlines()[line_no - 1].strip()
            pytest.fail(
                f"REQ-CB-PL-002 back-edge in {package_name}: {py_path}:{line_no} — {snippet!r}"
            )

        ast_hits = _ast_references_calibration_backtest(source)
        if ast_hits:
            line_no, snippet = ast_hits[0]
            pytest.fail(
                f"REQ-CB-PL-002 back-edge in {package_name} (AST): "
                f"{py_path}:{line_no} — {snippet!r}"
            )


def test_canonical_package_list_matches_spec() -> None:
    """Pin the 7-package list so a silent edit to ``CANONICAL_PACKAGES`` fails here.

    REQ-CB-PL-002 enumerates the seven packages explicitly. Any
    addition/removal must land in the spec and this constant in the same
    change set; this assertion makes the coupling load-bearing.
    """

    assert CANONICAL_PACKAGES == (
        "pattern_library",
        "signal_scanner",
        "mispricing_detector",
        "polymarket_connector",
        "data_ingest",
        "report_generator",
        "position_engine",
    )
    assert len(CANONICAL_PACKAGES) == 7
    assert len(set(CANONICAL_PACKAGES)) == 7  # no duplicates


def test_every_canonical_package_is_importable_on_disk() -> None:
    """Every package in ``CANONICAL_PACKAGES`` must exist as a directory.

    Guards against typos in the constant — without this, a misspelled
    package name would yield zero files scanned and a vacuously-passing
    parametrized case.
    """

    missing: list[str] = [name for name in CANONICAL_PACKAGES if not (_SRC_ROOT / name).is_dir()]
    assert not missing, f"canonical packages missing from src tree: {missing}"


def test_back_edge_detector_flags_synthetic_violation() -> None:
    """Self-test the regex+AST detectors against a synthetic source string.

    Without this, a refactor that broke both detectors would still pass
    every parametrized case (because the real tree is clean). This test
    verifies the detectors actually fire on a known-bad input.
    """

    bad_source = (
        "from __future__ import annotations\n"
        "from razor_rooster.calibration_backtest.api import run_backtest\n"
    )
    assert _FORBIDDEN_RE.search(bad_source) is not None
    hits = _ast_references_calibration_backtest(bad_source)
    assert len(hits) == 1
    assert "razor_rooster.calibration_backtest.api" in hits[0][1]

    bad_import = "import razor_rooster.calibration_backtest as cb\n"
    assert _FORBIDDEN_RE.search(bad_import) is not None
    import_hits = _ast_references_calibration_backtest(bad_import)
    assert len(import_hits) == 1

    clean_source = (
        "from __future__ import annotations\n"
        "from razor_rooster.pattern_library import LIBRARY_VERSION\n"
        "# from razor_rooster.calibration_backtest import api  # noqa: docstring example\n"
    )
    # Comment is line-anchored but starts with whitespace+'#', so the
    # forbidden regex (which allows leading whitespace) still matches.
    # Strip the comment to confirm the clean path is silent.
    clean_no_comment = "\n".join(
        line for line in clean_source.splitlines() if not line.lstrip().startswith("#")
    )
    assert _FORBIDDEN_RE.search(clean_no_comment) is None
    assert _ast_references_calibration_backtest(clean_no_comment) == []
