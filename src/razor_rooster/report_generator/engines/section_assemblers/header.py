"""Header assembler (T-RG-020; design §3.5).

Returns a content dict shaped like::

    {
      "type": "header",
      "report_id": "...",
      "cycle_date": "2026-05-15",
      "library_version": 7,
      "since_ts": datetime(...),
      "until_ts": datetime(...),
      "stale_source_count": int,
      "library_age_days": int | None,
      "disabled_sections": tuple[str, ...],
    }
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb

from razor_rooster.data_ingest.persistence.provenance import query_freshness


def assemble(
    conn: duckdb.DuckDBPyConnection,
    *,
    report_id: str,
    since_ts: datetime,
    until_ts: datetime,
    library_version: int,
    library_age_days: int | None,
    disabled_sections: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the header content dict."""
    rows = query_freshness(conn)
    stale_count = sum(1 for r in rows if r.is_stale)
    return {
        "type": "header",
        "report_id": report_id,
        "cycle_date": until_ts.date().isoformat(),
        "library_version": library_version,
        "since_ts": since_ts,
        "until_ts": until_ts,
        "stale_source_count": stale_count,
        "library_age_days": library_age_days,
        "disabled_sections": disabled_sections,
    }


__all__ = ["assemble"]
