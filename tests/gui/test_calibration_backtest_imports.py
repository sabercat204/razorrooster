"""Static + smoke checks for the calibration-backtest GUI integration (T-CB-041).

These tests guard the hard-won architectural invariants Phase 6 inherits:

* The ``razor_rooster.gui.app`` module must NOT import
  ``razor_rooster.calibration_backtest`` at top level. Doing so would
  pull the calibration-backtest subsystem (and every transitive import
  it carries — pattern_library, signal_scanner, etc.) into module
  initialisation time for the GUI, defeating the no-circular guarantee
  and inflating cold-start cost. The router import lives **inside**
  ``_register_routes`` so the dependency only resolves when the GUI is
  actually constructed.
* The ``razor_rooster.report_generator`` package must NOT import
  ``calibration_backtest`` *or* ``gui``. ``report_generator`` is an
  upstream subsystem for both — a back-edge would reintroduce the
  circular-dependency tangle the calibration-backtest design explicitly
  walks around (see CALIBRATION_BACKTEST_DESIGN.md §3.15 / Scout
  amendment 2026-05-31).
* ``create_app`` must construct cleanly without raising
  ``ImportError`` from a circular import — a smoke instantiation
  catches any regression where someone accidentally promotes one of
  the local imports back to module scope.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Lay out the absolute paths this test file leans on so each assertion
# fails loud if the repo layout changes underneath us.
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_GUI_APP_PATH: Path = _REPO_ROOT / "src" / "razor_rooster" / "gui" / "app.py"
_REPORT_GENERATOR_DIR: Path = _REPO_ROOT / "src" / "razor_rooster" / "report_generator"


def _module_has_top_level_import(
    source: str,
    *,
    forbidden_prefixes: tuple[str, ...],
) -> tuple[str, ...]:
    """Return every top-level import in ``source`` that hits any forbidden prefix.

    ``ast.walk`` yields nested imports too, so we explicitly walk only the
    top-level ``ast.Module.body`` — imports nested inside ``def`` /
    ``class`` (e.g. ``_register_routes``'s local imports) are deliberately
    excluded.
    """

    tree = ast.parse(source)
    matches: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for prefix in forbidden_prefixes:
                if module == prefix or module.startswith(f"{prefix}."):
                    matches.append(f"from {module} import ...")
                    break
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                for prefix in forbidden_prefixes:
                    if name == prefix or name.startswith(f"{prefix}."):
                        matches.append(f"import {name}")
                        break
    return tuple(matches)


def test_no_top_level_calibration_backtest_import_in_app() -> None:
    """``gui/app.py`` must not import ``razor_rooster.calibration_backtest`` at top level.

    The router is imported locally inside ``_register_routes`` so the
    GUI module's import graph stays free of the calibration-backtest
    subsystem until app construction. This test parses the source AST
    rather than monkey-patching ``sys.modules`` so the assertion is
    deterministic even if some upstream test has already pulled the
    module in.
    """

    assert _GUI_APP_PATH.is_file(), _GUI_APP_PATH
    source = _GUI_APP_PATH.read_text(encoding="utf-8")
    matches = _module_has_top_level_import(
        source,
        forbidden_prefixes=("razor_rooster.calibration_backtest",),
    )
    assert matches == (), (
        f"gui/app.py contains a top-level calibration_backtest import: {matches}. "
        "The router import belongs inside ``_register_routes`` so the "
        "no-circular-import guarantee survives. See "
        "CALIBRATION_BACKTEST_DESIGN.md §3.15."
    )


def test_report_generator_does_not_import_calibration_backtest() -> None:
    """``report_generator/`` must not import ``calibration_backtest`` or ``gui``.

    ``report_generator`` is the upstream subsystem; the calibration-backtest
    design routes around it via parity tests instead of runtime imports
    (T-CB-022 / Scout amendment 2026-05-31). A back-edge from
    ``report_generator`` into either ``calibration_backtest`` or ``gui``
    would reintroduce the exact circular-dependency tangle the design
    walks around.
    """

    assert _REPORT_GENERATOR_DIR.is_dir(), _REPORT_GENERATOR_DIR
    forbidden_prefixes = (
        "razor_rooster.calibration_backtest",
        "razor_rooster.gui",
    )
    offences: list[str] = []
    for py_path in _REPORT_GENERATOR_DIR.rglob("*.py"):
        # Skip ``__pycache__`` artefacts and any test modules inside the
        # subsystem (test files may legitimately reference downstream
        # subsystems for parity contracts).
        if "__pycache__" in py_path.parts:
            continue
        try:
            source = py_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AssertionError(f"failed to read {py_path}: {exc}") from exc
        # Walk the entire AST (not just module top level) — a nested
        # local import would still register a runtime dependency edge
        # the moment that branch executes.
        try:
            tree = ast.parse(source, filename=str(py_path))
        except SyntaxError as exc:
            raise AssertionError(f"unexpected syntax error in {py_path}: {exc}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for prefix in forbidden_prefixes:
                    if module == prefix or module.startswith(f"{prefix}."):
                        offences.append(f"{py_path}: from {module} import ...")
                        break
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    for prefix in forbidden_prefixes:
                        if name == prefix or name.startswith(f"{prefix}."):
                            offences.append(f"{py_path}: import {name}")
                            break
    assert offences == [], (
        "report_generator imports calibration_backtest or gui:\n  "
        + "\n  ".join(offences)
        + "\nReport_generator is the upstream subsystem — a back-edge would "
        "reintroduce the circular-dependency tangle the calibration-backtest "
        "design explicitly avoids."
    )


def test_create_app_smoke_no_circular_import(tmp_path: Path) -> None:
    """Constructing the GUI app must not raise ``ImportError``.

    Imports happen at function scope so ``create_app`` is exercised in
    isolation — ``app.py``'s module-level imports already resolved by
    pytest's collection phase, so the failure mode this test catches
    is the local-import inside ``_register_routes`` bringing in a
    cyclic dependency at construction time. We additionally verify
    that the calibration-backtest router is reachable on the app's
    ``routes`` table so a silent registration regression also surfaces.
    """

    from razor_rooster.calibration_backtest.persistence.migrations import (
        run_pending_calibration_backtest_migrations,
    )
    from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
    from razor_rooster.gui.app import create_app

    db_path = tmp_path / "smoke.duckdb"
    store = DuckDBStore(db_path)
    try:
        with store.connection() as conn:
            run_pending_calibration_backtest_migrations(conn)
    finally:
        store.close()

    app = create_app(db_path=db_path)
    cb_paths = [
        getattr(route, "path", "")
        for route in app.routes
        if isinstance(getattr(route, "path", ""), str)
        and getattr(route, "path", "").startswith("/calibration-backtest")
    ]
    assert cb_paths, (
        "calibration-backtest router missing from app.routes after create_app — "
        "the local import in _register_routes regressed."
    )
