"""Report-listing and report-detail routes."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from razor_rooster.gui._db import open_store
from razor_rooster.gui._render import render_template
from razor_rooster.report_generator.persistence.operations import (
    get_report,
    list_reports,
)

router = APIRouter()


@router.get("/reports", response_class=HTMLResponse)
async def reports_list(
    request: Request,
    since: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> Response:
    """List reports with optional ISO-8601 ``since`` filter and limit."""
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid since: {exc}") from exc
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)
    db_path = request.app.state.db_path
    with open_store(db_path) as conn:
        reports = list_reports(conn, since=since_dt, limit=limit)
    return render_template(
        request,
        "reports_list.html",
        {
            "active": "reports",
            "reports": reports,
            "filters": {"since": since, "limit": limit},
        },
    )


@router.get("/reports/{report_id}", response_class=HTMLResponse)
async def report_detail(request: Request, report_id: str) -> Response:
    """Drilldown view for a single report with its terminal output."""
    db_path = request.app.state.db_path
    with open_store(db_path) as conn:
        record = get_report(conn, report_id=report_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no report found: {report_id}")
        # Look up the immediately-prior report so the template can offer
        # a "compare to previous" link.
        prior = list_reports(conn, since=None, limit=2)
    prev_report_id: str | None = None
    if len(prior) >= 2 and prior[0].report_id == report_id:
        prev_report_id = prior[1].report_id
    return render_template(
        request,
        "report_detail.html",
        {
            "active": "reports",
            "report": record,
            "prev_report_id": prev_report_id,
        },
    )


@router.get("/reports/{report_id}/html", response_class=HTMLResponse)
async def report_html(request: Request, report_id: str) -> Response:
    """Serve the persisted standalone HTML rendering of a report."""
    db_path = request.app.state.db_path
    with open_store(db_path) as conn:
        record = get_report(conn, report_id=report_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no report found: {report_id}")
    if not record.rendered_html_text:
        raise HTTPException(
            status_code=404,
            detail=(
                "no HTML rendering persisted for this report. Re-run "
                "`razor-rooster report generate --html PATH` to record one."
            ),
        )
    return Response(
        content=record.rendered_html_text,
        media_type="text/html; charset=utf-8",
    )
