"""FastAPI app factory for the Razor-Rooster operator GUI.

The app is loopback-only by default and registers no POST/PUT/DELETE
routes — the GUI is strictly a navigation chrome over the existing
DuckDB store. State mutation continues to flow through the CLI.

The imperative-language linter from
:mod:`razor_rooster.position_engine.frame.linter` runs on every
rendered HTML response via :class:`LinterMiddleware` so the GUI
inherits the framing guarantees of the daily report.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from razor_rooster import __version__
from razor_rooster.gui.static_inline import INLINE_CSS
from razor_rooster.position_engine.frame.linter import (
    ImperativeLanguageDetected,
    check_text,
)

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _build_templates() -> Jinja2Templates:
    """Create the Jinja2Templates instance with shared globals."""
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.globals["inline_css"] = INLINE_CSS
    templates.env.globals["app_version"] = __version__
    return templates


class LinterMiddleware(BaseHTTPMiddleware):
    """Run every HTML response through the imperative-language linter.

    Carry-forward of REQ-RG-FRAME-001: every operator-facing rendered
    output passes through the catalog before reaching the operator.
    A linter rejection raises :class:`ImperativeLanguageDetected` and
    returns a 500 with a clear message identifying the offending
    phrase, so the operator notices the regression before the page
    paints.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return response
        # Read the body once for the linter, rebuild the response so
        # downstream middleware (and the client) sees the same bytes.
        body = b""
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            body += chunk
        try:
            check_text(body.decode("utf-8", errors="replace"))
        except ImperativeLanguageDetected as exc:
            logger.exception(
                "gui: imperative-language linter rejected the rendered page",
            )
            error_html = (
                "<!DOCTYPE html><html><head><title>Linter rejection</title>"
                f"<style>{INLINE_CSS}</style></head><body>"
                "<header class='topbar'><h1>Razor-Rooster GUI</h1></header>"
                "<main><h2>Imperative-language linter rejection</h2>"
                "<p>The renderer produced output containing a forbidden "
                "phrase. The page was withheld so the framing rules "
                "remain intact.</p>"
                f"<pre>{exc}</pre>"
                "<p class='muted'>This is a regression in the renderer; "
                "the page itself is not visible until the offending "
                "phrasing is removed.</p></main></body></html>"
            )
            return HTMLResponse(
                content=error_html,
                status_code=500,
            )
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )


def create_app(*, db_path: Path) -> FastAPI:
    """Build the FastAPI app bound to a specific DuckDB store path.

    The ``db_path`` is captured at app-build time and made available
    to route handlers via ``request.app.state.db_path``. Routes open
    a fresh DuckDB connection per request via the existing
    :class:`razor_rooster.data_ingest.persistence.duckdb_store.DuckDBStore`
    helper so connections are not shared across the asyncio loop.
    """
    app = FastAPI(
        title="Razor-Rooster Operator GUI",
        description=(
            "Read-only navigation chrome over the Razor-Rooster DuckDB "
            "store. Loopback-only; no external assets; "
            "imperative-language linter applies to every rendered page."
        ),
        version=__version__,
        docs_url=None,  # disable Swagger UI; we don't expose an API
        redoc_url=None,
    )
    app.state.db_path = Path(db_path)
    app.state.templates = _build_templates()
    app.add_middleware(LinterMiddleware)
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    """Register every page route on the app.

    Imports are local so circular imports across the route modules
    can't bite during app construction.
    """
    from razor_rooster.gui.routes.calibration import router as calibration_router
    from razor_rooster.gui.routes.calibration_backtest import (
        router as calibration_backtest_router,
    )
    from razor_rooster.gui.routes.compare import router as compare_router
    from razor_rooster.gui.routes.digest import router as digest_router
    from razor_rooster.gui.routes.index import router as index_router
    from razor_rooster.gui.routes.reports import router as reports_router
    from razor_rooster.gui.routes.watch import router as watch_router

    app.include_router(index_router)
    app.include_router(reports_router)
    app.include_router(compare_router)
    app.include_router(digest_router)
    app.include_router(watch_router)
    app.include_router(calibration_router)
    app.include_router(calibration_backtest_router)


__all__ = ["LinterMiddleware", "create_app"]
