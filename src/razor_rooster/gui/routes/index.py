"""Dashboard route — summary cards + recent reports list."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from razor_rooster.gui._db import open_store
from razor_rooster.gui._render import render_template
from razor_rooster.report_generator.persistence.operations import list_reports

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    """Dashboard with summary cards and the most recent reports."""
    db_path = request.app.state.db_path
    cutoff = datetime.now(tz=UTC) - timedelta(days=7)
    with open_store(db_path) as conn:
        recent = list_reports(conn, since=cutoff, limit=10)
        all_in_window = list_reports(conn, since=cutoff, limit=None)
    n_window = len(all_in_window)
    cycles_with_failures = sum(1 for r in all_in_window if len(r.sections_failed) > 0)
    avg_sections_rendered: float | None = (
        sum(len(r.sections_rendered) for r in all_in_window) / n_window if n_window > 0 else None
    )
    library_version: int | None = all_in_window[0].library_version if all_in_window else None
    stats = {
        "report_count_7d": n_window,
        "cycles_with_failures": cycles_with_failures,
        "avg_sections_rendered": avg_sections_rendered,
        "library_version": library_version,
    }
    return render_template(
        request,
        "index.html",
        {
            "active": "index",
            "stats": stats,
            "recent_reports": recent,
        },
    )
