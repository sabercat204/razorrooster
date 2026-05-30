"""Recent threshold-changes section (T-RG-COMPAT-RECENT-001 v0.44.0).

Reads recent ``threshold_tuning_log`` entries (since the report's
``since_ts``) and surfaces them in a small section so operators see
threshold tuning alongside the data the thresholds shape, instead
of having to remember to run ``razor-rooster report tuning-log``.

Opt-in via ``enabled_sections`` — most days have no tuning history
to show, so adding it to every report by default would be noise.
Operators who tune frequently can enable it.

Returns a content dict shaped like::

    {
      "type": "recent_tuning",
      "entries": [
        {
          "log_id": "...",
          "applied_at": datetime,
          "measurement_kind": "...",
          "knob": "...",
          "previous_value": float | None,
          "new_value": float,
          "target_percentile": float | None,
          "note": str | None,
        },
        ...
      ],
    }

Entries are ordered newest-first. Empty list when no tuning
happened in the cycle window.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import duckdb

from razor_rooster.report_generator.persistence.operations import (
    list_tuning_log_entries,
)

logger = logging.getLogger(__name__)


def assemble(
    conn: duckdb.DuckDBPyConnection,
    *,
    since_ts: datetime,
    until_ts: datetime,
) -> dict[str, Any]:
    """Assemble the recent-tuning section content.

    Reads tuning-log entries between ``since_ts`` and ``until_ts``,
    newest first. Pure read; never mutates state.
    """
    del until_ts  # The list helper filters via since only; no
    # natural upper bound is needed because the table only ever
    # appends.
    try:
        entries = list_tuning_log_entries(conn, since=since_ts)
    except duckdb.CatalogException:
        # Table doesn't exist yet (pre-m7003 store). Return empty.
        return {"type": "recent_tuning", "entries": []}
    items: list[dict[str, Any]] = []
    for entry in entries:
        items.append(
            {
                "log_id": entry.log_id,
                "applied_at": entry.applied_at,
                "measurement_kind": entry.measurement_kind,
                "knob": entry.knob,
                "previous_value": entry.previous_value,
                "new_value": entry.new_value,
                "target_percentile": entry.target_percentile,
                "note": entry.note,
            }
        )
    return {"type": "recent_tuning", "entries": items}


__all__ = ["assemble"]
