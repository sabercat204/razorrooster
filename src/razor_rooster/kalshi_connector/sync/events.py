"""Events daily sync (T-KSI-041 step 2; design §3.4).

Reconciles ``kalshi_events`` with Kalshi's current ``/events``
listing. Pulls open events plus closed/settled events whose
``expected_expiration_time`` is within the live cutoff window so the
diff detects status transitions reliably.

The simpler path — paginate ``/events`` without a status filter — is
not used because it returns every event ever, which is unbounded.
We instead pull (a) open events for every active series and (b) all
events whose status changed recently. Step (b) is approximated by
re-pulling closed/settled status across pagination; cutoff routing
ensures we don't re-pull historical-cutoff-side events.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import duckdb
import pyarrow as pa

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.staging_merge import staging_merge
from razor_rooster.kalshi_connector.client.models import KalshiEvent
from razor_rooster.kalshi_connector.client.rest import KalshiRESTClient
from razor_rooster.kalshi_connector.persistence.source import (
    KALSHI_LIVE_SOURCE_ID,
)
from razor_rooster.kalshi_connector.sync._common import (
    CONNECTOR_VERSION,
    encode_payload,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class EventsSyncReport:
    """Outcome of one ``sync_events`` invocation."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    events_total_seen: int = 0
    events_inserted: int = 0
    events_updated: int = 0
    events_unchanged: int = 0
    events_removed: int = 0
    errors: list[str] = field(default_factory=list)


def sync_events(
    store: DuckDBStore,
    *,
    client: KalshiRESTClient,
    series_tickers: Iterable[str] | None = None,
    statuses: Iterable[str] = ("open", "closed", "settled"),
    page_size: int = 100,
    now: datetime | None = None,
) -> EventsSyncReport:
    """Run one Kalshi events daily sync.

    When ``series_tickers`` is None, the function reads every active
    series from ``kalshi_series`` and pulls events per series. Pulling
    per-series rather than via a global ``/events`` paginated list
    bounds the response size and lets the connector resume more
    cleanly mid-cycle.
    """
    started = now or datetime.now(tz=UTC)
    report = EventsSyncReport(started_at=started)

    if series_tickers is None:
        with store.connection() as conn:
            tickers = sorted(_active_series_tickers(conn))
    else:
        tickers = sorted(set(series_tickers))

    if not tickers:
        logger.info("kalshi events sync: no active series to scan")
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report

    upstream: list[KalshiEvent] = []
    try:
        for ticker in tickers:
            for status in statuses:
                cursor: str | None = None
                while True:
                    page = client.list_events(
                        series_ticker=ticker,
                        status=status,
                        cursor=cursor,
                        limit=page_size,
                    )
                    for entry in page.items:
                        if isinstance(entry, KalshiEvent) and entry.event_ticker:
                            upstream.append(entry)
                    if page.cursor is None:
                        break
                    cursor = page.cursor
    except Exception as exc:
        logger.exception("Kalshi events fetch failed")
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report

    seen: set[str] = set()
    deduped: list[KalshiEvent] = []
    for entry in upstream:
        if entry.event_ticker in seen:
            continue
        seen.add(entry.event_ticker)
        deduped.append(entry)
    report.events_total_seen = len(deduped)

    with store.connection() as conn:
        existing_tickers = _existing_active_event_tickers(conn, series_tickers=tickers)
        upstream_tickers = {e.event_ticker for e in deduped}
        removed = existing_tickers - upstream_tickers

        if deduped:
            arrow_batch = _events_to_arrow(deduped, fetch_ts=started)
            merge_result = staging_merge(
                conn,
                target_table="kalshi_events",
                batch=arrow_batch,
            )
            report.events_inserted = merge_result.inserted
            report.events_updated = merge_result.revised
            report.events_unchanged = merge_result.unchanged

        if removed:
            for ticker in sorted(removed):
                conn.execute(
                    "UPDATE kalshi_events "
                    "SET removed_at = ? "
                    "WHERE event_ticker = ? AND removed_at IS NULL",
                    [started, ticker],
                )
            report.events_removed = len(removed)

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "kalshi events sync: seen=%d inserted=%d updated=%d unchanged=%d removed=%d",
        report.events_total_seen,
        report.events_inserted,
        report.events_updated,
        report.events_unchanged,
        report.events_removed,
    )
    return report


# -- internals --------------------------------------------------------------


def _active_series_tickers(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT series_ticker FROM kalshi_series "
        "WHERE superseded_at IS NULL AND removed_at IS NULL"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _existing_active_event_tickers(
    conn: duckdb.DuckDBPyConnection, *, series_tickers: list[str]
) -> set[str]:
    if not series_tickers:
        return set()
    placeholders = ", ".join(["?"] * len(series_tickers))
    rows = conn.execute(
        f"SELECT DISTINCT event_ticker FROM kalshi_events "
        f"WHERE superseded_at IS NULL AND removed_at IS NULL "
        f"AND series_ticker IN ({placeholders})",
        list(series_tickers),
    ).fetchall()
    return {str(r[0]) for r in rows}


def _events_to_arrow(items: list[KalshiEvent], *, fetch_ts: datetime) -> pa.Table:
    column_data: dict[str, list[Any]] = {
        "source_id": [],
        "source_record_id": [],
        "source_publication_ts": [],
        "fetch_ts": [],
        "connector_version": [],
        "source_payload_json": [],
        "superseded_at": [],
        "event_ticker": [],
        "series_ticker": [],
        "title": [],
        "sub_title": [],
        "category": [],
        "mutually_exclusive": [],
        "expected_expiration_time": [],
        "strike_period": [],
        "status": [],
        "created_at": [],
        "last_updated_at": [],
        "removed_at": [],
    }
    for item in items:
        publication_ts = item.last_updated_at or item.created_at or fetch_ts
        column_data["source_id"].append(KALSHI_LIVE_SOURCE_ID)
        column_data["source_record_id"].append(item.event_ticker)
        column_data["source_publication_ts"].append(publication_ts)
        column_data["fetch_ts"].append(fetch_ts)
        column_data["connector_version"].append(CONNECTOR_VERSION)
        column_data["source_payload_json"].append(
            encode_payload(
                {
                    "event_ticker": item.event_ticker,
                    "series_ticker": item.series_ticker,
                    "title": item.title,
                    "sub_title": item.sub_title,
                    "category": item.category,
                    "mutually_exclusive": item.mutually_exclusive,
                    "expected_expiration_time": (
                        item.expected_expiration_time.isoformat()
                        if item.expected_expiration_time
                        else None
                    ),
                    "strike_period": item.strike_period,
                    "status": item.status,
                }
            )
        )
        column_data["superseded_at"].append(None)
        column_data["event_ticker"].append(item.event_ticker)
        column_data["series_ticker"].append(item.series_ticker)
        column_data["title"].append(item.title)
        column_data["sub_title"].append(item.sub_title)
        column_data["category"].append(item.category)
        column_data["mutually_exclusive"].append(item.mutually_exclusive)
        column_data["expected_expiration_time"].append(item.expected_expiration_time)
        column_data["strike_period"].append(item.strike_period)
        column_data["status"].append(item.status)
        column_data["created_at"].append(item.created_at)
        column_data["last_updated_at"].append(item.last_updated_at)
        column_data["removed_at"].append(None)
    schema = pa.schema(
        [
            ("source_id", pa.string()),
            ("source_record_id", pa.string()),
            ("source_publication_ts", pa.timestamp("us", tz="UTC")),
            ("fetch_ts", pa.timestamp("us", tz="UTC")),
            ("connector_version", pa.string()),
            ("source_payload_json", pa.string()),
            ("superseded_at", pa.timestamp("us", tz="UTC")),
            ("event_ticker", pa.string()),
            ("series_ticker", pa.string()),
            ("title", pa.string()),
            ("sub_title", pa.string()),
            ("category", pa.string()),
            ("mutually_exclusive", pa.bool_()),
            ("expected_expiration_time", pa.timestamp("us", tz="UTC")),
            ("strike_period", pa.string()),
            ("status", pa.string()),
            ("created_at", pa.timestamp("us", tz="UTC")),
            ("last_updated_at", pa.timestamp("us", tz="UTC")),
            ("removed_at", pa.timestamp("us", tz="UTC")),
        ]
    )
    return pa.Table.from_pydict(column_data, schema=schema)


__all__ = ["EventsSyncReport", "sync_events"]
