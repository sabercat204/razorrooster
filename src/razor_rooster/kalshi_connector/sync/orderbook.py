"""Orderbook sampling (T-KSI-044; design §3.4; REQ-KSI-OB-001..002).

On-demand: callers (the watched-markets path or the operator's
``razor-rooster kalshi fetch-orderbook`` CLI) call
:func:`fetch_orderbook` for one ticker. The result is persisted into
``kalshi_orderbook_snapshots`` with one row per (side, level) pair.

The Kalshi REST endpoint returns YES-side levels only; the REST
client's parser derives NO-side levels via ``no_bid = 1 - yes_ask``.
This module persists both sides faithfully so downstream queries see a
symmetric snapshot.

Orderbook reads are not part of the regular cycle — they cost 10
tokens per call and the price snapshot already captures top-of-book
metrics. Callers invoke this only for analyses that need depth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.staging_merge import staging_merge
from razor_rooster.kalshi_connector.client.models import (
    KalshiOrderbook,
    KalshiOrderbookLevel,
)
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
class OrderbookFetchReport:
    """Outcome of one orderbook fetch."""

    ticker: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    yes_levels: int = 0
    no_levels: int = 0
    rows_inserted: int = 0
    rows_unchanged: int = 0
    errors: list[str] = field(default_factory=list)


def fetch_orderbook(
    store: DuckDBStore,
    *,
    client: KalshiRESTClient,
    ticker: str,
    depth: int = 10,
    now: datetime | None = None,
) -> OrderbookFetchReport:
    """Fetch and persist an orderbook snapshot for ``ticker``."""
    started = now or datetime.now(tz=UTC)
    report = OrderbookFetchReport(ticker=ticker, started_at=started)
    try:
        orderbook = client.get_orderbook(ticker, depth=depth)
    except Exception as exc:
        logger.warning("kalshi orderbook fetch failed for %s: %s", ticker, exc)
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report

    rows = list(_orderbook_to_rows(orderbook, fetch_ts=started))
    report.yes_levels = len(orderbook.yes_levels)
    report.no_levels = len(orderbook.no_levels)
    if not rows:
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report

    try:
        with store.connection() as conn:
            arrow_batch = _orderbook_rows_to_arrow(rows)
            merge_result = staging_merge(
                conn,
                target_table="kalshi_orderbook_snapshots",
                batch=arrow_batch,
                dedup_keys=("ticker", "snapshot_ts", "side", "level"),
            )
            report.rows_inserted = merge_result.inserted + merge_result.revised
            report.rows_unchanged = merge_result.unchanged
    except Exception as exc:
        logger.exception("kalshi orderbook persist failed for %s", ticker)
        report.errors.append(f"persist: {type(exc).__name__}: {exc}")

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "kalshi orderbook fetch: ticker=%s yes_levels=%d no_levels=%d inserted=%d",
        ticker,
        report.yes_levels,
        report.no_levels,
        report.rows_inserted,
    )
    return report


# -- internals --------------------------------------------------------------


def _orderbook_to_rows(
    orderbook: KalshiOrderbook,
    *,
    fetch_ts: datetime,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for level_idx, level in enumerate(orderbook.yes_levels):
        rows.append(
            _level_row(
                ticker=orderbook.ticker,
                snapshot_ts=orderbook.snapshot_ts,
                fetch_ts=fetch_ts,
                side="yes",
                level=level_idx,
                level_data=level,
            )
        )
    for level_idx, level in enumerate(orderbook.no_levels):
        rows.append(
            _level_row(
                ticker=orderbook.ticker,
                snapshot_ts=orderbook.snapshot_ts,
                fetch_ts=fetch_ts,
                side="no",
                level=level_idx,
                level_data=level,
            )
        )
    return rows


def _level_row(
    *,
    ticker: str,
    snapshot_ts: datetime,
    fetch_ts: datetime,
    side: str,
    level: int,
    level_data: KalshiOrderbookLevel,
) -> dict[str, Any]:
    record_id = f"{ticker}:{snapshot_ts.isoformat()}:{side}:{level}"
    payload = {
        "ticker": ticker,
        "side": side,
        "level": level,
        "price_dollars": level_data.price_dollars,
        "count": level_data.count,
    }
    return {
        "source_id": KALSHI_LIVE_SOURCE_ID,
        "source_record_id": record_id,
        "source_publication_ts": snapshot_ts,
        "fetch_ts": fetch_ts,
        "connector_version": CONNECTOR_VERSION,
        "source_payload_json": encode_payload(payload),
        "superseded_at": None,
        "ticker": ticker,
        "snapshot_ts": snapshot_ts,
        "side": side,
        "level": level,
        "price_dollars": level_data.price_dollars,
        "count_fp": level_data.count,
    }


def _orderbook_rows_to_arrow(rows: list[dict[str, Any]]) -> pa.Table:
    column_data: dict[str, list[Any]] = {key: [] for key in rows[0]}
    for row in rows:
        for key, value in row.items():
            column_data[key].append(value)
    schema = pa.schema(
        [
            ("source_id", pa.string()),
            ("source_record_id", pa.string()),
            ("source_publication_ts", pa.timestamp("us", tz="UTC")),
            ("fetch_ts", pa.timestamp("us", tz="UTC")),
            ("connector_version", pa.string()),
            ("source_payload_json", pa.string()),
            ("superseded_at", pa.timestamp("us", tz="UTC")),
            ("ticker", pa.string()),
            ("snapshot_ts", pa.timestamp("us", tz="UTC")),
            ("side", pa.string()),
            ("level", pa.int32()),
            ("price_dollars", pa.float64()),
            ("count_fp", pa.float64()),
        ]
    )
    return pa.Table.from_pydict(column_data, schema=schema)


__all__ = ["OrderbookFetchReport", "fetch_orderbook"]
