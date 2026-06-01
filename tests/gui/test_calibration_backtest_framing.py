"""Framing-coverage tests for the calibration-backtest GUI surface (T-CB-039).

The framing linter is wired globally via
:class:`razor_rooster.gui.app.LinterMiddleware`; these tests verify

* the canonical ``DISCLAIMER`` block surfaces on both the list and
  detail views,
* the ``FOOTER_NOTE`` substring surfaces on both views,
* ``LinterMiddleware`` actually rejects an injected forbidden phrase
  on a sibling route (the rejection path is otherwise unreachable),
* ``gui/routes/calibration_backtest.py`` does not re-import or call
  ``check_text`` (would double-run the global middleware).
"""

from __future__ import annotations

import ast
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from razor_rooster.calibration_backtest.frame import DISCLAIMER, FOOTER_NOTE
from razor_rooster.gui.app import LinterMiddleware

# ---------------------------------------------------------------------------
# Disclaimer / footer presence — both routes
# ---------------------------------------------------------------------------


# Both ``DISCLAIMER`` and ``FOOTER_NOTE`` are multi-sentence blocks; we pick
# substrings that are unique to each constant and stable across copy edits
# of the surrounding paragraph. Matching against the full text would be
# brittle (HTML-escaping, line wrapping in the template, etc.).
_DISCLAIMER_MARKER = "decision-support calibration evidence"
_FOOTER_MARKER = "Disagreement between the model and a market is an observation"


def test_disclaimer_marker_is_in_disclaimer_constant() -> None:
    """The marker we assert for is genuinely a substring of DISCLAIMER."""

    assert _DISCLAIMER_MARKER in DISCLAIMER


def test_footer_marker_is_in_footer_note_constant() -> None:
    """The marker we assert for is genuinely a substring of FOOTER_NOTE."""

    assert _FOOTER_MARKER in FOOTER_NOTE


def test_disclaimer_in_list_response(backtest_client: TestClient) -> None:
    """``GET /calibration-backtest`` carries the canonical disclaimer."""

    response = backtest_client.get("/calibration-backtest")
    assert response.status_code == 200
    assert _DISCLAIMER_MARKER in response.text


def test_disclaimer_in_detail_response(backtest_client: TestClient) -> None:
    """``GET /calibration-backtest/{run_id}`` carries the canonical disclaimer."""

    response = backtest_client.get("/calibration-backtest/run-healthy-aaaaaaaaaaaaaaa")
    assert response.status_code == 200
    assert _DISCLAIMER_MARKER in response.text


def test_footer_note_in_both_responses(backtest_client: TestClient) -> None:
    """The footer note surfaces at the bottom of both list and detail views."""

    list_response = backtest_client.get("/calibration-backtest")
    assert list_response.status_code == 200
    assert _FOOTER_MARKER in list_response.text

    detail_response = backtest_client.get("/calibration-backtest/run-healthy-aaaaaaaaaaaaaaa")
    assert detail_response.status_code == 200
    assert _FOOTER_MARKER in detail_response.text


# ---------------------------------------------------------------------------
# Live middleware rejection on a sibling route
# ---------------------------------------------------------------------------


def test_lintermiddleware_catches_forbidden_phrase() -> None:
    """A test-only route that emits a forbidden phrase trips the middleware.

    Builds a fresh FastAPI app wearing the same ``LinterMiddleware`` the
    real GUI uses, then mounts a route whose response body embeds
    ``"you should buy"`` (one of the seed phrases in
    ``config/forbidden_phrases.yaml``). The middleware should swallow the
    response and surface the rejection page (``status_code=500`` with the
    "Imperative-language linter rejection" body the GUI ships).
    """

    app = FastAPI(docs_url=None, redoc_url=None)
    app.add_middleware(LinterMiddleware)

    @app.get("/__forbidden", response_class=HTMLResponse)
    async def forbidden_route() -> HTMLResponse:
        # The body must contain a phrase from
        # ``config/forbidden_phrases.yaml`` so ``check_text`` raises
        # ``ImperativeLanguageDetected``.
        return HTMLResponse(
            content=("<!DOCTYPE html><html><body><p>you should buy this contract</p></body></html>")
        )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/__forbidden")

    # ``LinterMiddleware`` short-circuits to 500 with a structured rejection
    # page; the body identifies the offending phrase via the linter's
    # ``__str__`` output.
    assert response.status_code == 500
    assert "Imperative-language linter rejection" in response.text


def test_lintermiddleware_passes_clean_response() -> None:
    """A clean response on the same harness sails through the middleware.

    Pairs with :func:`test_lintermiddleware_catches_forbidden_phrase` so the
    rejection assertion is not silently masking a 500 from some unrelated
    middleware misconfiguration: the same harness passes a clean body.
    """

    app = FastAPI(docs_url=None, redoc_url=None)
    app.add_middleware(LinterMiddleware)

    @app.get("/__clean", response_class=HTMLResponse)
    async def clean_route() -> HTMLResponse:
        return HTMLResponse(content="<!DOCTYPE html><html><body><p>clean output</p></body></html>")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/__clean")

    assert response.status_code == 200
    assert "clean output" in response.text


# ---------------------------------------------------------------------------
# Static guarantee: the route module itself never calls ``check_text``
# ---------------------------------------------------------------------------


_ROUTE_MODULE_PATH: Path = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "razor_rooster"
    / "gui"
    / "routes"
    / "calibration_backtest.py"
)


def test_no_per_route_linter_call_in_calibration_backtest_module() -> None:
    """The GUI route module must rely on the global ``LinterMiddleware``.

    AST-walks ``gui/routes/calibration_backtest.py`` and asserts that

    * no ``import``/``from`` line references ``check_text`` or
      ``LinterCatalog`` (those belong to the global middleware), and
    * no expression ``Call`` node is ``check_text(...)``.

    A double-run would not be a *correctness* bug (the catalog is the
    same), but it would defeat the whole point of the global middleware
    and double the per-response lint cost.
    """

    assert _ROUTE_MODULE_PATH.is_file(), _ROUTE_MODULE_PATH
    source = _ROUTE_MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_ROUTE_MODULE_PATH))

    forbidden_names = {"check_text", "LinterCatalog", "ImperativeLanguageDetected"}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in forbidden_names, (
                    f"calibration_backtest GUI route module imports {alias.name!r} "
                    f"from {node.module!r}; the global LinterMiddleware already "
                    f"covers framing — re-importing would double-run."
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "frame.linter" not in alias.name, (
                    f"calibration_backtest GUI route module imports "
                    f"{alias.name!r}; rely on LinterMiddleware instead."
                )
        elif isinstance(node, ast.Call):
            func = node.func
            called: str | None = None
            if isinstance(func, ast.Name):
                called = func.id
            elif isinstance(func, ast.Attribute):
                called = func.attr
            if called == "check_text":
                raise AssertionError(
                    "calibration_backtest GUI route module calls check_text "
                    "directly; rely on the global LinterMiddleware instead."
                )
