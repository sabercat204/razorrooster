"""Calibration-scaffolding linkage pass (T-MD-041; OQ-MD-005 resolution).

For each ``polymarket_resolutions`` row whose ``resolution_ts`` is
later than the persisted ``last_linkage_ts``, find any
``comparisons`` rows referencing the resolved market and write
``comparison_resolutions`` rows linking them. The pass is idempotent:
re-runs do not duplicate.

The polarity stored on the comparison determines how the resolution
maps to ``outcome_observed``:

- aligned: outcome_observed = 1 iff resolution_outcome == 'yes'
- inverted: outcome_observed = 1 iff resolution_outcome == 'no'
- invalid resolutions (resolution_outcome == 'invalid') get
  ``outcome_observed = 0`` regardless of polarity. The calibration
  backtest is expected to filter on resolution_outcome to handle
  these cases.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal

import duckdb

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.mispricing_detector.models import (
    ComparisonResolution,
    Polarity,
    ResolutionOutcome,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    query_comparisons_for_market,
    query_existing_resolution_links,
    state_get,
    state_set,
    write_resolution_link,
)

logger = logging.getLogger(__name__)


_LAST_LINKAGE_KEY: Final[str] = "last_linkage_ts"


@dataclass(slots=True)
class LinkageReport:
    """Aggregate result of one linkage pass."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    new_resolutions_processed: int = 0
    new_links_written: int = 0
    last_linkage_ts: datetime | None = None
    errors: list[str] = field(default_factory=list)


def run_linkage_pass(store: DuckDBStore, *, now: datetime | None = None) -> LinkageReport:
    """Walk forward through polymarket_resolutions and link any matched
    comparisons. Returns an aggregate report.
    """
    started = now or datetime.now(tz=UTC)
    report = LinkageReport(started_at=started)

    with store.connection() as conn:
        last_ts_str = state_get(conn, _LAST_LINKAGE_KEY)
        last_ts = _parse_ts(last_ts_str)

    new_resolutions = _fetch_new_resolutions(store, since=last_ts)
    if not new_resolutions:
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        report.last_linkage_ts = last_ts
        return report

    max_seen_ts = last_ts
    for resolution in new_resolutions:
        try:
            new_links = _link_resolution(store, resolution=resolution, now=started)
            report.new_resolutions_processed += 1
            report.new_links_written += new_links
        except Exception as exc:
            logger.exception(
                "linkage pass failed for resolution condition_id=%s",
                resolution.condition_id,
            )
            report.errors.append(f"{resolution.condition_id}: {type(exc).__name__}: {exc}")
            continue
        if max_seen_ts is None or resolution.resolution_ts > max_seen_ts:
            max_seen_ts = resolution.resolution_ts

    if max_seen_ts is not None:
        with store.connection() as conn:
            state_set(conn, _LAST_LINKAGE_KEY, max_seen_ts.isoformat())

    report.last_linkage_ts = max_seen_ts
    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    return report


# -- internals --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ResolutionRow:
    """Compact projection of one polymarket_resolutions row."""

    condition_id: str
    resolution_outcome: ResolutionOutcome
    resolution_ts: datetime


def _fetch_new_resolutions(
    store: DuckDBStore, *, since: datetime | None
) -> tuple[_ResolutionRow, ...]:
    """Pull resolutions newer than ``since``, ordered by resolution_ts."""
    base_query = (
        "SELECT condition_id, winning_outcome_label, invalidated, resolution_ts "
        "FROM polymarket_resolutions "
        "WHERE superseded_at IS NULL"
    )
    params: list[object] = []
    if since is not None:
        base_query += " AND resolution_ts > ?"
        params.append(since)
    base_query += " ORDER BY resolution_ts ASC"
    with store.connection() as conn:
        rows = conn.execute(base_query, params).fetchall()

    resolutions: list[_ResolutionRow] = []
    for r in rows:
        condition_id = str(r[0])
        outcome = _resolution_outcome_from_row(label=r[1], invalidated=r[2])
        resolution_ts = r[3]
        if resolution_ts is None:
            continue
        if resolution_ts.tzinfo is None:
            resolution_ts = resolution_ts.replace(tzinfo=UTC)
        resolutions.append(
            _ResolutionRow(
                condition_id=condition_id,
                resolution_outcome=outcome,
                resolution_ts=resolution_ts,
            )
        )
    return tuple(resolutions)


def _resolution_outcome_from_row(*, label: str | None, invalidated: bool) -> ResolutionOutcome:
    """Map a polymarket_resolutions row to the typed outcome."""
    if invalidated:
        return "invalid"
    if label is None:
        return "invalid"
    label_lower = str(label).strip().lower()
    if label_lower == "yes":
        return "yes"
    if label_lower == "no":
        return "no"
    # Unknown labels treated as invalid for the calibration backtest.
    return "invalid"


def _link_resolution(store: DuckDBStore, *, resolution: _ResolutionRow, now: datetime) -> int:
    """Write linkage rows for any comparisons referencing the resolution.

    Returns the number of new links created.
    """
    with store.connection() as conn:
        comparisons = query_comparisons_for_market(conn, condition_id=resolution.condition_id)
        already_linked = query_existing_resolution_links(conn, condition_id=resolution.condition_id)

    new_links = 0
    for comparison in comparisons:
        if comparison.comparison_id in already_linked:
            continue
        outcome_observed = _outcome_observed(
            resolution=resolution.resolution_outcome,
            polarity=comparison.polarity,
        )
        link = ComparisonResolution(
            comparison_id=comparison.comparison_id,
            condition_id=resolution.condition_id,
            resolution_outcome=resolution.resolution_outcome,
            resolution_ts=resolution.resolution_ts,
            model_probability_at_comparison=comparison.model_probability,
            market_probability_at_comparison=comparison.market_probability,
            polarity_at_comparison=comparison.polarity,
            outcome_observed=outcome_observed,
            linked_at=now,
        )
        with store.connection() as conn:
            write_resolution_link(conn, link)
        new_links += 1
    return new_links


def _outcome_observed(*, resolution: ResolutionOutcome, polarity: Polarity) -> Literal[0, 1]:
    """Polarity-aware mapping from market resolution to model-event outcome."""
    if resolution == "invalid":
        return 0
    if polarity == "aligned":
        return 1 if resolution == "yes" else 0
    # inverted
    return 1 if resolution == "no" else 0


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


__all__ = ["LinkageReport", "run_linkage_pass"]


_RESERVED: tuple[object, ...] = (Iterable, duckdb)
