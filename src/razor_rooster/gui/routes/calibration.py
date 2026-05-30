"""Calibration / measurements route."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from razor_rooster.gui._db import open_store
from razor_rooster.gui._render import render_template
from razor_rooster.report_generator.persistence.operations import (
    list_threshold_measurements,
)

router = APIRouter()


@router.get("/calibration", response_class=HTMLResponse)
async def calibration(request: Request) -> Response:
    """Per-cycle calibration measurements summary."""
    db_path = request.app.state.db_path
    with open_store(db_path) as conn:
        measurements = list_threshold_measurements(
            conn,
            measurement_kind="cross_venue_spread_bps",
            since=None,
            limit=30,
        )
    return render_template(
        request,
        "calibration.html",
        {
            "active": "calibration",
            "measurements": measurements,
        },
    )
