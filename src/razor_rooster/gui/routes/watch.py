"""Watch / threshold-tuning route.

Surfaces:

- Recent operator-controlled watch-state transitions for position-engine
  analyses, grouped by current state. Read-only — operators continue to
  use the CLI to mark / dismiss / acted-on / expire transitions.
- Recent ``threshold_tuning_log`` entries from the report-generator's
  threshold-suggestion feedback loop.

The watch-states view re-derives current-state lists from the
append-only ``watch_states`` log via
``position_engine.persistence.operations.list_by_state``. ``analyses``
metadata is fetched per analysis so the operator sees venue,
class_id, model vs. market probability, and the time of the last
transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from razor_rooster.gui._db import open_store
from razor_rooster.gui._render import render_template
from razor_rooster.position_engine.models import (
    Analysis,
    WatchState,
    WatchStateValue,
)
from razor_rooster.position_engine.persistence.operations import (
    get_analysis,
    list_by_state,
)
from razor_rooster.report_generator.persistence.operations import (
    list_tuning_log_entries,
)

if TYPE_CHECKING:
    import duckdb

router = APIRouter()


# Display order mirrors the alert-tier ordering implied by ``monitor.engines.comb``:
# operator-active first, then operator-resolved, then system-resolved.
_DISPLAY_STATES: tuple[WatchStateValue, ...] = (
    "watching",
    "acted_on",
    "dismissed",
    "expired",
)


@dataclass(frozen=True, slots=True)
class WatchedAnalysisRow:
    """One row of the per-state watched-analyses table.

    Computed at request time from the ``watch_states`` append-only log
    plus the corresponding ``analyses`` metadata. ``analysis`` is
    optional because a watch_states row can outlive its analysis if the
    operator re-runs cycles with a fresh DuckDB; the GUI degrades
    gracefully rather than throwing.
    """

    state: WatchStateValue
    analysis_id: str
    set_at: datetime
    set_by: str
    notes: str | None
    analysis: Analysis | None


def _collect_watched_rows(
    conn: duckdb.DuckDBPyConnection,
) -> dict[WatchStateValue, tuple[WatchedAnalysisRow, ...]]:
    """Return rows grouped by current state, newest set_at first within group."""
    out: dict[WatchStateValue, tuple[WatchedAnalysisRow, ...]] = {}
    for state in _DISPLAY_STATES:
        watch_rows: tuple[WatchState, ...] = list_by_state(conn, state=state)
        rows: list[WatchedAnalysisRow] = []
        for ws in watch_rows:
            analysis = get_analysis(conn, analysis_id=ws.analysis_id)
            rows.append(
                WatchedAnalysisRow(
                    state=ws.state,
                    analysis_id=ws.analysis_id,
                    set_at=ws.set_at,
                    set_by=ws.set_by,
                    notes=ws.notes,
                    analysis=analysis,
                )
            )
        rows.sort(key=lambda r: r.set_at, reverse=True)
        out[state] = tuple(rows)
    return out


@router.get("/watch", response_class=HTMLResponse)
async def watch(request: Request) -> Response:
    """Watch / threshold-tuning page (read-only)."""
    db_path = request.app.state.db_path
    with open_store(db_path) as conn:
        tuning_log = list_tuning_log_entries(conn, limit=50)
        watched_by_state = _collect_watched_rows(conn)
    counts: dict[WatchStateValue, int] = {
        state: len(watched_by_state[state]) for state in _DISPLAY_STATES
    }
    return render_template(
        request,
        "watch.html",
        {
            "active": "watch",
            "tuning_log": tuning_log,
            "watched_by_state": watched_by_state,
            "watched_counts": counts,
            "watched_states_order": _DISPLAY_STATES,
        },
    )
