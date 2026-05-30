"""Auto-expiration of watch states (T-PE-061; OQ-PE-005 resolution).

When the underlying Polymarket market resolves, any active watch
state on the resulting analyses (``'watching'`` or ``'acted_on'``)
automatically transitions to ``'expired'`` (``set_by='system'``).

The pass is idempotent: re-runs do not duplicate transitions.
Expirations append a fresh row to ``watch_states`` rather than
mutating an existing one (the table is append-only by design).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.position_engine.persistence.operations import (
    append_watch_state,
    latest_watch_state,
)

logger = logging.getLogger(__name__)


# Watch states that are still "active" — eligible for auto-expiration
# when the market resolves.
_ACTIVE_STATES: frozenset[str] = frozenset({"watching", "acted_on"})


@dataclass(slots=True)
class ExpirationReport:
    """Aggregate result of one expiration pass."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    expirations_written: int = 0
    analyses_examined: int = 0
    errors: list[str] = field(default_factory=list)


def run_expiration_pass(store: DuckDBStore, *, now: datetime | None = None) -> ExpirationReport:
    """For each comparison_resolutions row, expire active watch states.

    This pass is meant to be called at the end of each
    ``position_engine`` cycle (T-PE-051) and is also exposed via the
    standalone ``--expire-only`` CLI flag for operator triage.

    Implementation:
    1. Find every ``analyses`` row whose ``comparison_id`` has a
       corresponding row in ``comparison_resolutions``.
    2. For each, look up the latest watch state.
    3. If the latest state is ``'watching'`` or ``'acted_on'``,
       append a fresh ``'expired'`` row with ``set_by='system'``.
    """
    started = now or datetime.now(tz=UTC)
    report = ExpirationReport(started_at=started)

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT a.analysis_id, a.comparison_id, r.condition_id, "
            "r.resolution_outcome, r.resolution_ts "
            "FROM analyses a "
            "INNER JOIN comparison_resolutions r "
            "  ON a.comparison_id = r.comparison_id "
            "ORDER BY r.resolution_ts ASC"
        ).fetchall()

    report.analyses_examined = len(rows)
    for row in rows:
        analysis_id = str(row[0])
        try:
            with store.connection() as conn:
                current = latest_watch_state(conn, analysis_id=analysis_id)
                if current is None or current.state not in _ACTIVE_STATES:
                    continue
                append_watch_state(
                    conn,
                    analysis_id=analysis_id,
                    state="expired",
                    notes=(
                        f"market resolved at {row[4].isoformat() if row[4] is not None else '?'} "
                        f"(outcome={row[3]!s})"
                    ),
                    set_by="system",
                    when=started,
                )
            report.expirations_written += 1
        except Exception as exc:
            logger.exception("expiration pass failed for analysis_id=%s", analysis_id)
            report.errors.append(f"{analysis_id}: {type(exc).__name__}: {exc}")

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    return report


__all__ = ["ExpirationReport", "run_expiration_pass"]


_RESERVED: tuple[object, ...] = (Iterable,)
