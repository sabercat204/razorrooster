"""Digest route — sortable, filterable per-cycle listing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from razor_rooster.gui._db import open_store
from razor_rooster.gui._render import render_template
from razor_rooster.report_generator.models import ReportRecord
from razor_rooster.report_generator.persistence.operations import list_reports

router = APIRouter()


def _sort_reports(
    reports: tuple[ReportRecord, ...],
    *,
    sort_by: str,
    sort_direction: str,
) -> tuple[ReportRecord, ...]:
    """Mirror of ``cli._sort_digest_reports`` with the same tie-breaker."""
    reverse = sort_direction.lower() == "desc"
    key = sort_by.lower()
    if key == "generated_at":
        return tuple(sorted(reports, key=lambda r: r.generated_at, reverse=reverse))
    if key == "sections_failed":
        return tuple(
            sorted(
                reports,
                key=lambda r: (len(r.sections_failed), r.generated_at),
                reverse=reverse,
            )
        )
    if key == "terminal_chars":
        return tuple(
            sorted(
                reports,
                key=lambda r: (len(r.rendered_terminal_text), r.generated_at),
                reverse=reverse,
            )
        )
    return reports


@router.get("/digest", response_class=HTMLResponse)
async def digest(
    request: Request,
    days: int = Query(default=7, ge=1, le=365),
    sort_by: Literal["generated_at", "sections_failed", "terminal_chars"] = Query(
        default="generated_at"
    ),
    sort_direction: Literal["asc", "desc"] = Query(default="desc"),
    top: int | None = Query(default=None, ge=1, le=1000),
    report_id: str | None = Query(default=None),
) -> Response:
    """Sortable digest mirroring ``razor-rooster report digest``."""
    if top is not None and (top < 1 or top > 1000):
        raise HTTPException(status_code=400, detail=f"top {top} is out of range [1, 1000]")
    cutoff = datetime.now(tz=UTC) - timedelta(days=days)
    db_path = request.app.state.db_path
    with open_store(db_path) as conn:
        reports = list_reports(conn, since=cutoff, limit=None)
    if report_id:
        reports = tuple(r for r in reports if r.report_id.startswith(report_id))
    reports = _sort_reports(reports, sort_by=sort_by, sort_direction=sort_direction)
    full_reports = reports
    sliced = reports[:top] if top is not None else reports
    n_full = len(full_reports)
    aggregate: dict[str, object]
    if n_full == 0:
        aggregate = {
            "report_count": 0,
            "cycles_with_failures": 0,
            "cycles_with_markdown": 0,
            "cycles_with_html": 0,
            "avg_sections_rendered": None,
            "avg_terminal_chars": None,
        }
    else:
        aggregate = {
            "report_count": n_full,
            "cycles_with_failures": sum(1 for r in full_reports if len(r.sections_failed) > 0),
            "cycles_with_markdown": sum(1 for r in full_reports if r.markdown_path),
            "cycles_with_html": sum(1 for r in full_reports if r.html_path),
            "avg_sections_rendered": (sum(len(r.sections_rendered) for r in full_reports) / n_full),
            "avg_terminal_chars": (
                sum(len(r.rendered_terminal_text) for r in full_reports) / n_full
            ),
        }
    return render_template(
        request,
        "digest.html",
        {
            "active": "digest",
            "reports": sliced,
            "aggregate": aggregate,
            "filters": {
                "days": days,
                "sort_by": sort_by,
                "sort_direction": sort_direction,
                "top": top,
                "report_id_prefix": report_id,
            },
        },
    )
