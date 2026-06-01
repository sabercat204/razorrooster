"""Calibration-backtest routes — list and detail views.

Read-only navigation chrome over the ``backtest_runs`` /
``backtest_predictions`` / ``backtest_traces`` tables. T-CB-036
fills in the list view; T-CB-037 fills in the detail view; T-CB-038
extends the detail view with a paginated, filterable predictions
table.

The framing linter is wired globally via
:class:`razor_rooster.gui.app.LinterMiddleware`; this module never
calls ``check_text`` directly so the response body is never linted
twice.
"""

# DEFER-CB-006: AJAX endpoint exposing the per-prediction
# ``trace_diff_summary`` payload is intentionally out-of-scope for v1
# (deferred to v2). The detail view's predictions table renders the
# row metadata but does not surface trace decompression. See
# ``CALIBRATION_BACKTEST_DESIGN.md`` §7 for the deferred-features
# inventory.

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from razor_rooster.calibration_backtest.errors import BacktestConfigError
from razor_rooster.calibration_backtest.frame import DISCLAIMER, FOOTER_NOTE
from razor_rooster.calibration_backtest.models import (
    BacktestRun,
    PredictionStatus,
    SkipReason,
)
from razor_rooster.calibration_backtest.persistence.operations import (
    count_predictions,
    fetch_run,
    list_predictions,
    list_runs,
)
from razor_rooster.calibration_backtest.renderers._diagram_hydrate import (
    reliability_diagrams_from_run,
)
from razor_rooster.calibration_backtest.renderers.reliability_svg import (
    render_reliability_svg,
)
from razor_rooster.gui._db import open_store
from razor_rooster.gui._render import render_template

router = APIRouter()

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

_PREDICTIONS_DEFAULT_PAGE_SIZE = 20
_PREDICTIONS_MAX_PAGE_SIZE = 200


def _fallback_polarity_rate(run: BacktestRun) -> float | None:
    """Return ``fallback_polarity_count / predictions_scored`` or ``None``.

    The list view shows the rate as a percentage; when no predictions
    were scored the rate is undefined and the renderer falls back to an
    em-dash placeholder.
    """

    if run.predictions_scored <= 0:
        return None
    return run.fallback_polarity_count / run.predictions_scored


@router.get("/calibration-backtest", response_class=HTMLResponse)
async def list_view(
    request: Request,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> Response:
    """List calibration-backtest runs ordered ``started_at DESC`` (T-CB-036).

    Pagination is driven by ``?limit`` and ``?offset`` — the template
    surfaces prev/next links that walk the seeded runs in fixed-size
    pages. ``limit`` is bounded ``[1, 200]`` so an operator can not
    accidentally request a page that exhausts memory.
    """

    db_path = request.app.state.db_path
    with open_store(db_path) as conn:
        runs = list_runs(conn, limit=limit, offset=offset)
    rows: list[dict[str, Any]] = []
    for run in runs:
        rows.append(
            {
                "run_id": run.run_id,
                "run_id_short": run.run_id[:12],
                "started_at": run.started_at,
                "library_version": run.library_version,
                "system_revision": run.system_revision,
                "system_revision_short": run.system_revision[:16],
                "lag_days": run.lag_days,
                "predictions_total": run.predictions_total,
                "predictions_scored": run.predictions_scored,
                "overall_brier": run.overall_brier,
                "fallback_polarity_rate": _fallback_polarity_rate(run),
                "status": str(run.status),
            }
        )
    has_prev = offset > 0
    has_next = len(runs) >= limit
    prev_offset = max(0, offset - limit)
    next_offset = offset + limit
    context: dict[str, Any] = {
        "active": "calibration_backtest",
        "rows": rows,
        "limit": limit,
        "offset": offset,
        "has_prev": has_prev,
        "has_next": has_next,
        "prev_offset": prev_offset,
        "next_offset": next_offset,
        "disclaimer": DISCLAIMER,
        "footer_note": FOOTER_NOTE,
    }
    return render_template(request, "calibration_backtest/list.html", context)


def _summary_brier_table(
    summary: Any,
    key: str,
) -> list[tuple[str, float]]:
    """Extract a sorted ``(label, brier)`` table from ``summary[key]``.

    The summary's per-sector / per-class Brier dicts are persisted as
    sorted mappings (``ScoreSummary.as_mapping``); rebuilding the
    presentation list defensively handles legacy or hand-edited rows
    where the value is missing or non-numeric.
    """

    if not isinstance(summary, dict):
        return []
    raw = summary.get(key)
    if not isinstance(raw, dict):
        return []
    out: list[tuple[str, float]] = []
    for label in sorted(raw.keys()):
        value = raw[label]
        if isinstance(value, (int, float)):
            out.append((str(label), float(value)))
    return out


def _skip_reason_breakdown(run: BacktestRun) -> dict[str, int]:
    """Return ``{skip_reason: count}`` from the persisted summary.

    The summary itself does not break skips down by reason today; the
    detail view surfaces an empty mapping when none is recorded so the
    template renders an em-dash placeholder rather than crashing.
    """

    summary = run.summary_json
    if not isinstance(summary, dict):
        return {}
    raw = summary.get("predictions_skipped_by_reason")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int):
            out[str(key)] = value
    return dict(sorted(out.items()))


def _parse_status_filter(raw: str | None) -> PredictionStatus | None:
    """Coerce a ``?status=`` query value to :class:`PredictionStatus`.

    Empty / missing values yield ``None`` (no filter). Unknown values
    raise ``HTTPException(400)`` so an operator typo surfaces as a
    deterministic client error instead of a silent empty page.
    """

    if raw is None or raw == "":
        return None
    try:
        return PredictionStatus(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid status filter: {raw!r}",
        ) from exc


def _parse_skip_reason_filter(raw: str | None) -> SkipReason | None:
    """Coerce a ``?skip_reason=`` query value to :class:`SkipReason`.

    Same contract as :func:`_parse_status_filter`: missing -> ``None``;
    unknown -> ``HTTPException(400)``.
    """

    if raw is None or raw == "":
        return None
    try:
        return SkipReason(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid skip_reason filter: {raw!r}",
        ) from exc


def _parse_positive_int(
    raw: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
    field: str,
) -> int:
    """Parse a positive-int query param, clamping into ``[minimum, maximum]``.

    Non-integer values surface as 400 so the operator's typo is loud;
    out-of-range values clamp silently into the supported window
    (mirrors ``Query(ge=..., le=...)`` for the typed endpoints elsewhere
    in the GUI).
    """

    if raw is None or raw == "":
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid {field}: {raw!r}",
        ) from exc
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def _build_query_string(
    *,
    status: PredictionStatus | None,
    skip_reason: SkipReason | None,
    page: int | None,
    limit: int,
) -> str:
    """Reconstruct a ``?...`` query string carrying the active filters.

    The pagination / filter links surface ``status``, ``skip_reason``,
    ``page``, and ``limit`` so navigating between pages preserves the
    operator's current view. ``page=None`` omits the param so the
    caller can build a "filter-only" link that resets to page 1.
    """

    parts: list[str] = []
    if status is not None:
        parts.append(f"status={status!s}")
    if skip_reason is not None:
        parts.append(f"skip_reason={skip_reason!s}")
    if page is not None:
        parts.append(f"page={page}")
    parts.append(f"limit={limit}")
    return "?" + "&".join(parts)


def _row_for_prediction_table(
    prediction: Any,
) -> dict[str, Any]:
    """Format a :class:`BacktestPrediction` for the detail-view table.

    Every field is pre-formatted server-side so the template stays a
    pure data sink — no Python expressions in Jinja, no implicit
    coercions to surprise mypy. ``model_p`` formats to 4 decimals to
    match the per-sector / per-class Brier tables; ``skip_reason``
    renders as an em-dash unless the row is actually skipped.
    """

    skip_reason_display = "—"
    if prediction.status == PredictionStatus.SKIPPED and prediction.skip_reason is not None:
        skip_reason_display = str(prediction.skip_reason)
    model_p_display = f"{prediction.model_p:.4f}" if prediction.model_p is not None else "—"
    observed_display = f"{prediction.observed:.0f}" if prediction.observed is not None else "—"
    polarity_display = str(prediction.polarity) if prediction.polarity is not None else "—"
    return {
        "prediction_id": prediction.prediction_id,
        "prediction_id_short": prediction.prediction_id[:12],
        "class_id": prediction.class_id,
        "condition_id": prediction.condition_id,
        "venue": prediction.venue,
        "sector": prediction.sector,
        "prediction_ts": prediction.prediction_ts.isoformat(),
        "resolution_ts": prediction.resolution_ts.isoformat(),
        "model_p_display": model_p_display,
        "observed_display": observed_display,
        "polarity_display": polarity_display,
        "polarity_source": str(prediction.polarity_source),
        "status": str(prediction.status),
        "skip_reason_display": skip_reason_display,
    }


@router.get("/calibration-backtest/{run_id}", response_class=HTMLResponse)
async def detail_view(request: Request, run_id: str) -> Response:
    """Render a single calibration-backtest run (T-CB-037, T-CB-038).

    Hydrates the per-sector reliability diagrams from
    ``run.summary_json`` and pre-renders each as an inline SVG via
    :func:`render_reliability_svg`. Missing-run and degenerate-diagram
    paths surface as 404 / 500 with structured messages.

    The predictions table (T-CB-038) is paginated and filterable via
    ``?status=``, ``?skip_reason=``, ``?page=``, and ``?limit=``. The
    filter dropdown / tabs include counts per unique ``skip_reason``
    seen in this run so an operator can jump directly to the most
    common skip path.
    """

    status_filter = _parse_status_filter(request.query_params.get("status"))
    skip_reason_filter = _parse_skip_reason_filter(request.query_params.get("skip_reason"))
    page = _parse_positive_int(
        request.query_params.get("page"),
        default=1,
        minimum=1,
        maximum=1_000_000,
        field="page",
    )
    limit = _parse_positive_int(
        request.query_params.get("limit"),
        default=_PREDICTIONS_DEFAULT_PAGE_SIZE,
        minimum=1,
        maximum=_PREDICTIONS_MAX_PAGE_SIZE,
        field="limit",
    )
    offset = (page - 1) * limit

    db_path = request.app.state.db_path
    with open_store(db_path) as conn:
        run = fetch_run(conn, run_id)
        if run is None:
            raise HTTPException(
                status_code=404,
                detail=f"no calibration-backtest run found: {run_id}",
            )
        predictions = list_predictions(
            conn,
            run_id,
            status=status_filter,
            skip_reason=skip_reason_filter,
            limit=limit,
            offset=offset,
        )
        total_predictions = count_predictions(
            conn,
            run_id,
            status=status_filter,
            skip_reason=skip_reason_filter,
        )
        # Per-skip-reason counts feed the filter dropdown so an
        # operator can jump straight to the most common skip path.
        skip_reason_counts: dict[str, int] = {}
        for reason in SkipReason:
            cnt = count_predictions(
                conn,
                run_id,
                status=PredictionStatus.SKIPPED,
                skip_reason=reason,
            )
            if cnt > 0:
                skip_reason_counts[str(reason)] = cnt
        scored_total = count_predictions(conn, run_id, status=PredictionStatus.SCORED)
        skipped_total = count_predictions(conn, run_id, status=PredictionStatus.SKIPPED)
        all_total = count_predictions(conn, run_id)

    diagrams = reliability_diagrams_from_run(run)
    reliability_svgs: dict[str, str] = {}
    try:
        for sector in sorted(diagrams):
            reliability_svgs[sector] = render_reliability_svg(diagrams[sector], sector_label=sector)
    except BacktestConfigError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"failed to render reliability diagram for run {run_id}: {exc}",
        ) from exc

    per_sector_brier = _summary_brier_table(run.summary_json, "per_sector_brier")
    per_class_brier = _summary_brier_table(run.summary_json, "per_class_brier")
    fallback_rate = _fallback_polarity_rate(run)
    skip_breakdown = _skip_reason_breakdown(run)

    total_pages = max(1, (total_predictions + limit - 1) // limit)
    has_prev = page > 1
    has_next = page < total_pages
    prediction_rows = [_row_for_prediction_table(p) for p in predictions]

    # Filter-tab links carry the same limit but reset to page 1; the
    # active filter renders as the matching tab.
    all_link = _build_query_string(status=None, skip_reason=None, page=1, limit=limit)
    scored_link = _build_query_string(
        status=PredictionStatus.SCORED, skip_reason=None, page=1, limit=limit
    )
    skipped_link = _build_query_string(
        status=PredictionStatus.SKIPPED, skip_reason=None, page=1, limit=limit
    )
    skip_reason_links: list[dict[str, Any]] = []
    for reason_value in sorted(skip_reason_counts.keys()):
        reason_enum = SkipReason(reason_value)
        skip_reason_links.append(
            {
                "reason": reason_value,
                "count": skip_reason_counts[reason_value],
                "href": _build_query_string(
                    status=PredictionStatus.SKIPPED,
                    skip_reason=reason_enum,
                    page=1,
                    limit=limit,
                ),
                "active": skip_reason_filter is not None
                and str(skip_reason_filter) == reason_value,
            }
        )

    prev_link = (
        _build_query_string(
            status=status_filter,
            skip_reason=skip_reason_filter,
            page=page - 1,
            limit=limit,
        )
        if has_prev
        else None
    )
    next_link = (
        _build_query_string(
            status=status_filter,
            skip_reason=skip_reason_filter,
            page=page + 1,
            limit=limit,
        )
        if has_next
        else None
    )

    active_status = str(status_filter) if status_filter is not None else "all"
    active_skip_reason = str(skip_reason_filter) if skip_reason_filter is not None else None

    context: dict[str, Any] = {
        "active": "calibration_backtest",
        "run": run,
        "reliability_svgs": reliability_svgs,
        "per_sector_brier": per_sector_brier,
        "per_class_brier": per_class_brier,
        "fallback_polarity_rate": fallback_rate,
        "fallback_polarity_high": fallback_rate is not None and fallback_rate > 0.05,
        "skip_breakdown": skip_breakdown,
        "disclaimer": DISCLAIMER,
        "footer_note": FOOTER_NOTE,
        # Predictions table (T-CB-038)
        "prediction_rows": prediction_rows,
        "predictions_page": page,
        "predictions_total_pages": total_pages,
        "predictions_total": total_predictions,
        "predictions_limit": limit,
        "predictions_prev_link": prev_link,
        "predictions_next_link": next_link,
        "predictions_active_status": active_status,
        "predictions_active_skip_reason": active_skip_reason,
        "predictions_all_link": all_link,
        "predictions_scored_link": scored_link,
        "predictions_skipped_link": skipped_link,
        "predictions_skip_reason_links": skip_reason_links,
        "predictions_count_all": all_total,
        "predictions_count_scored": scored_total,
        "predictions_count_skipped": skipped_total,
    }
    return render_template(request, "calibration_backtest/detail.html", context)


__all__ = ["router"]
