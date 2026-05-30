"""Series daily sync (T-KSI-041 step 1; design §3.4).

Reconciles ``kalshi_series`` with Kalshi's current ``/series`` listing.

1. Paginate ``GET /series`` and collect every series row.
2. Diff against the local table:
   - In response and absent locally → insert.
   - In response with changed payload → upsert via staging-merge.
   - Locally present but absent from response → set ``removed_at``.
3. Update ``sources.last_successful_fetch`` for ``kalshi``.

Concurrency: writes only to ``kalshi_series`` and the ``sources`` row
for ``kalshi``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import duckdb
import pyarrow as pa

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.provenance import (
    update_last_successful_fetch,
)
from razor_rooster.data_ingest.persistence.staging_merge import staging_merge
from razor_rooster.kalshi_connector.client.models import KalshiSeries
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
class SeriesSyncReport:
    """Outcome of one ``sync_series`` invocation."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    series_total_seen: int = 0
    series_inserted: int = 0
    series_updated: int = 0
    series_unchanged: int = 0
    series_removed: int = 0
    errors: list[str] = field(default_factory=list)


def sync_series(
    store: DuckDBStore,
    *,
    client: KalshiRESTClient,
    page_size: int = 100,
    now: datetime | None = None,
) -> SeriesSyncReport:
    """Run one Kalshi series daily sync."""
    started = now or datetime.now(tz=UTC)
    report = SeriesSyncReport(started_at=started)

    upstream: list[KalshiSeries] = []
    cursor: str | None = None
    try:
        while True:
            page = client.list_series(cursor=cursor, limit=page_size)
            for item in page.items:
                if isinstance(item, KalshiSeries) and item.series_ticker:
                    upstream.append(item)
            if page.cursor is None:
                break
            cursor = page.cursor
    except Exception as exc:
        logger.exception("Kalshi series fetch failed")
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report

    # Dedup by series_ticker; keep the first occurrence (paginated lists
    # rarely overlap but defensive dedup costs nothing).
    seen: set[str] = set()
    deduped: list[KalshiSeries] = []
    for s in upstream:
        if s.series_ticker in seen:
            continue
        seen.add(s.series_ticker)
        deduped.append(s)
    report.series_total_seen = len(deduped)

    with store.connection() as conn:
        existing_tickers = _existing_active_series_tickers(conn)
        upstream_tickers = {s.series_ticker for s in deduped}
        removed = existing_tickers - upstream_tickers

        if deduped:
            arrow_batch = _series_to_arrow(deduped, fetch_ts=started)
            merge_result = staging_merge(
                conn,
                target_table="kalshi_series",
                batch=arrow_batch,
            )
            report.series_inserted = merge_result.inserted
            report.series_updated = merge_result.revised
            report.series_unchanged = merge_result.unchanged

        if removed:
            for ticker in sorted(removed):
                conn.execute(
                    "UPDATE kalshi_series "
                    "SET removed_at = ? "
                    "WHERE series_ticker = ? AND removed_at IS NULL",
                    [started, ticker],
                )
            report.series_removed = len(removed)

        update_last_successful_fetch(conn, KALSHI_LIVE_SOURCE_ID, when=started)

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "kalshi series sync: seen=%d inserted=%d updated=%d unchanged=%d removed=%d",
        report.series_total_seen,
        report.series_inserted,
        report.series_updated,
        report.series_unchanged,
        report.series_removed,
    )
    return report


# -- internals --------------------------------------------------------------


def _existing_active_series_tickers(conn: duckdb.DuckDBPyConnection) -> set[str]:
    """Return tickers currently active in ``kalshi_series``.

    "Active" = not superseded and not removed. This is the set the
    diff classifies as removed when missing upstream.
    """
    rows = conn.execute(
        "SELECT DISTINCT series_ticker FROM kalshi_series "
        "WHERE superseded_at IS NULL AND removed_at IS NULL"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _series_to_arrow(items: list[KalshiSeries], *, fetch_ts: datetime) -> pa.Table:
    """Build the Arrow batch the staging-merge expects."""
    column_data: dict[str, list[Any]] = {
        "source_id": [],
        "source_record_id": [],
        "source_publication_ts": [],
        "fetch_ts": [],
        "connector_version": [],
        "source_payload_json": [],
        "superseded_at": [],
        "series_ticker": [],
        "title": [],
        "category": [],
        "frequency": [],
        "tags": [],
        "settlement_source": [],
        "contract_url": [],
        "created_at": [],
        "last_updated_at": [],
        "removed_at": [],
    }
    for item in items:
        publication_ts = item.last_updated_at or item.created_at or fetch_ts
        column_data["source_id"].append(KALSHI_LIVE_SOURCE_ID)
        column_data["source_record_id"].append(item.series_ticker)
        column_data["source_publication_ts"].append(publication_ts)
        column_data["fetch_ts"].append(fetch_ts)
        column_data["connector_version"].append(CONNECTOR_VERSION)
        column_data["source_payload_json"].append(
            encode_payload(
                {
                    "series_ticker": item.series_ticker,
                    "title": item.title,
                    "category": item.category,
                    "frequency": item.frequency,
                    "tags": list(item.tags),
                    "settlement_source": item.settlement_source,
                    "contract_url": item.contract_url,
                }
            )
        )
        column_data["superseded_at"].append(None)
        column_data["series_ticker"].append(item.series_ticker)
        column_data["title"].append(item.title)
        column_data["category"].append(item.category)
        column_data["frequency"].append(item.frequency)
        column_data["tags"].append(encode_payload(list(item.tags)))
        column_data["settlement_source"].append(item.settlement_source)
        column_data["contract_url"].append(item.contract_url)
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
            ("series_ticker", pa.string()),
            ("title", pa.string()),
            ("category", pa.string()),
            ("frequency", pa.string()),
            ("tags", pa.string()),
            ("settlement_source", pa.string()),
            ("contract_url", pa.string()),
            ("created_at", pa.timestamp("us", tz="UTC")),
            ("last_updated_at", pa.timestamp("us", tz="UTC")),
            ("removed_at", pa.timestamp("us", tz="UTC")),
        ]
    )
    return pa.Table.from_pydict(column_data, schema=schema)


__all__ = ["SeriesSyncReport", "sync_series"]
