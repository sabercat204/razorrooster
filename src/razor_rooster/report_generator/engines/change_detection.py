"""Upstream change detection for ``report watch --on-change`` (v0.46.0).

A small pure helper that fingerprints the upstream state the
report depends on. The watch loop calls this once per interval
and compares the fingerprint to the last one it computed; when
they match, the loop skips ``generate()`` and waits for the
next interval.

The fingerprint covers four max-IDs across the upstream tables:

- latest ``scan_summaries.scan_id`` — captures new signal-scanner
  cycles.
- latest ``comparisons.comparison_id`` — captures new
  mispricing-detector outputs.
- latest ``follow_ups.follow_up_id`` — captures new monitor
  follow-ups.
- latest ``threshold_tuning_log.log_id`` — captures operator
  threshold edits.

These four IDs are persistent and monotonic, so any new row in
any of them changes the fingerprint deterministically. Schema
catalog exceptions (table missing on a fresh-install path) are
treated as ``None`` rather than raising — the comparator
treats two ``None`` IDs as "unchanged" so opt-in pipelines
with missing tables don't churn unnecessarily.

Pure read; never modifies state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import duckdb

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UpstreamFingerprint:
    """Fingerprint of the upstream state the report depends on."""

    latest_scan_id: str | None
    latest_comparison_id: str | None
    latest_follow_up_id: str | None
    latest_tuning_log_id: str | None

    def is_same_as(self, other: UpstreamFingerprint) -> bool:
        return (
            self.latest_scan_id == other.latest_scan_id
            and self.latest_comparison_id == other.latest_comparison_id
            and self.latest_follow_up_id == other.latest_follow_up_id
            and self.latest_tuning_log_id == other.latest_tuning_log_id
        )


def compute_upstream_fingerprint(
    conn: duckdb.DuckDBPyConnection,
) -> UpstreamFingerprint:
    """Read the latest IDs from each upstream table.

    Each query is wrapped in try/except for ``CatalogException``
    so missing-table installs return ``None`` for the relevant
    field rather than blowing up.
    """
    return UpstreamFingerprint(
        latest_scan_id=_max_id(conn, table="scan_summaries", column="scan_id"),
        latest_comparison_id=_max_id(conn, table="comparisons", column="comparison_id"),
        latest_follow_up_id=_max_id(conn, table="follow_ups", column="follow_up_id"),
        latest_tuning_log_id=_max_id(conn, table="threshold_tuning_log", column="log_id"),
    )


# -- internals --------------------------------------------------------------


def _max_id(conn: duckdb.DuckDBPyConnection, *, table: str, column: str) -> str | None:
    """Return the lexicographically-greatest value in ``table.column``.

    Returns ``None`` when the table is empty or doesn't exist.
    UUIDs and timestamped IDs both work because they're sortable
    strings.
    """
    try:
        row = conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
    except duckdb.CatalogException:
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])


__all__ = ["UpstreamFingerprint", "compute_upstream_fingerprint"]
