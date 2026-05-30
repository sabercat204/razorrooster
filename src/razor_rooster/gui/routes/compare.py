"""Report-comparison routes.

Mirrors the ``razor-rooster report compare`` CLI flow:
- ``GET /compare`` shows a form with two report-id selects + the diff
  table when both are provided.
- ``GET /compare/{a}/{b}/html`` serves the existing self-contained
  compare-HTML page (the same rendering the CLI's ``--html`` flag
  produces).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from razor_rooster.gui._db import open_store
from razor_rooster.gui._render import render_template
from razor_rooster.report_generator.engines.compare import (
    ReportDiff,
    compare_reports,
)
from razor_rooster.report_generator.engines.compare_html import (
    render_compare_html,
)
from razor_rooster.report_generator.persistence.operations import (
    get_report,
    list_reports,
)

router = APIRouter()


@router.get("/compare", response_class=HTMLResponse)
async def compare_form(
    request: Request,
    a: str | None = Query(default=None),
    b: str | None = Query(default=None),
) -> Response:
    """Comparison-picker page; renders the diff inline when both ids resolve."""
    db_path = request.app.state.db_path
    with open_store(db_path) as conn:
        available_reports = list_reports(conn, since=None, limit=50)
        record_a = get_report(conn, report_id=a) if a else None
        record_b = get_report(conn, report_id=b) if b else None
    missing: list[str] = []
    if a and record_a is None:
        missing.append(a)
    if b and record_b is None:
        missing.append(b)
    diff: ReportDiff | None = None
    if record_a is not None and record_b is not None:
        diff = compare_reports(record_a, record_b)
    return render_template(
        request,
        "compare_form.html",
        {
            "active": "compare",
            "available_reports": available_reports,
            "a": a,
            "b": b,
            "diff": diff,
            "missing": missing,
        },
    )


@router.get("/compare/{a}/{b}/html", response_class=HTMLResponse)
async def compare_html_view(request: Request, a: str, b: str) -> Response:
    """Serve the standalone two-column compare-HTML page for a pair."""
    db_path = request.app.state.db_path
    with open_store(db_path) as conn:
        record_a = get_report(conn, report_id=a)
        record_b = get_report(conn, report_id=b)
    missing = [rid for rid, rec in [(a, record_a), (b, record_b)] if rec is None]
    if missing:
        raise HTTPException(status_code=404, detail=f"no report found: {', '.join(missing)}")
    assert record_a is not None and record_b is not None
    diff = compare_reports(record_a, record_b)
    html = render_compare_html(
        record_a=record_a,
        record_b=record_b,
        diff=diff,
        diff_line_limit=500,
        word_diff=True,
        side_by_side=True,
        quick_jump=True,
    )
    return Response(content=html, media_type="text/html; charset=utf-8")
