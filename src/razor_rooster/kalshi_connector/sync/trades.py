"""Trades pull (T-KSI-045; design §3.4; REQ-KSI-TRADE-001..003).

Pulls trades for the operator's ``watched_markets`` only. For each
ticker, determines the latest persisted ``created_time`` and routes
the read against the cutoff snapshot:

- If ``since >= cutoff.trades_created_ts``: live ``/markets/trades``.
- Else: ``/historical/trades``.

Trades are keyed on ``trade_id`` so re-pulling overlapping ranges is a
no-op via the staging-merge upsert.
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
from razor_rooster.kalshi_connector.client.models import KalshiTrade
from razor_rooster.kalshi_connector.client.rest import KalshiRESTClient
from razor_rooster.kalshi_connector.persistence.source import (
    KALSHI_LIVE_SOURCE_ID,
)
from razor_rooster.kalshi_connector.sync._common import (
    CONNECTOR_VERSION,
    encode_payload,
)
from razor_rooster.kalshi_connector.sync.cutoff import read_cutoff

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TradesSyncReport:
    """Outcome of one trades sync invocation."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    tickers_evaluated: int = 0
    trades_seen: int = 0
    trades_inserted: int = 0
    trades_unchanged: int = 0
    routed_live: int = 0
    routed_historical: int = 0
    market_errors: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def sync_trades(
    store: DuckDBStore,
    *,
    client: KalshiRESTClient,
    watched_markets: Iterable[str],
    page_size: int = 100,
    now: datetime | None = None,
) -> TradesSyncReport:
    """Run the trades sync over a list of watched market tickers.

    The cutoff snapshot must already be persisted by
    :func:`snapshot_cutoff`. If absent, the function records an error
    and returns without making API calls.
    """
    started = now or datetime.now(tz=UTC)
    report = TradesSyncReport(started_at=started)

    cutoff = read_cutoff(store)
    if cutoff is None:
        report.errors.append("no kalshi_historical_cutoff row; call snapshot_cutoff first")
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report

    tickers = sorted({str(t) for t in watched_markets})
    report.tickers_evaluated = len(tickers)

    if not tickers:
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report

    new_trades: list[KalshiTrade] = []

    for ticker in tickers:
        with store.connection() as conn:
            latest = _latest_trade_time(conn, ticker=ticker)
        # Determine live vs historical routing per design §3.4.
        use_live = latest is None or latest >= cutoff.trades_created_ts
        try:
            cursor: str | None = None
            while True:
                if use_live:
                    page = client.get_market_trades(
                        ticker=ticker,
                        cursor=cursor,
                        limit=page_size,
                    )
                    report.routed_live += 1
                else:
                    page = client.get_historical_trades(
                        ticker=ticker,
                        cursor=cursor,
                        limit=page_size,
                    )
                    report.routed_historical += 1
                for entry in page.items:
                    if isinstance(entry, KalshiTrade) and entry.trade_id:
                        # Skip trades we've already persisted by time.
                        if latest is not None and entry.created_time <= latest:
                            continue
                        new_trades.append(entry)
                if page.cursor is None:
                    break
                cursor = page.cursor
        except Exception as exc:
            logger.warning("kalshi trades fetch failed for %s: %s", ticker, exc)
            report.market_errors.append((ticker, f"{type(exc).__name__}: {exc}"))

    # Dedup by trade_id (defensive — the watermark check usually
    # eliminates overlap but pagination boundaries can return the same
    # trade twice on adjacent pages).
    seen: dict[str, KalshiTrade] = {}
    for t in new_trades:
        seen[t.trade_id] = t
    deduped = list(seen.values())
    report.trades_seen = len(deduped)

    if not deduped:
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report

    rows = [_trade_to_row(t, fetch_ts=started) for t in deduped]
    try:
        with store.connection() as conn:
            arrow_batch = _trade_rows_to_arrow(rows)
            merge_result = staging_merge(
                conn,
                target_table="kalshi_trades",
                batch=arrow_batch,
                dedup_keys=("trade_id",),
            )
            report.trades_inserted = merge_result.inserted + merge_result.revised
            report.trades_unchanged = merge_result.unchanged
    except Exception as exc:
        logger.exception("kalshi trades persist failed")
        report.errors.append(f"persist: {type(exc).__name__}: {exc}")

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "kalshi trades sync: tickers=%d trades_seen=%d inserted=%d live=%d historical=%d",
        report.tickers_evaluated,
        report.trades_seen,
        report.trades_inserted,
        report.routed_live,
        report.routed_historical,
    )
    return report


# -- internals --------------------------------------------------------------


def _latest_trade_time(conn: duckdb.DuckDBPyConnection, *, ticker: str) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(created_time) FROM kalshi_trades WHERE ticker = ?",
        [ticker],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    value = row[0]
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return None


def _trade_to_row(trade: KalshiTrade, *, fetch_ts: datetime) -> dict[str, Any]:
    payload = {
        "trade_id": trade.trade_id,
        "ticker": trade.ticker,
        "created_time": trade.created_time.isoformat(),
        "yes_price": trade.yes_price_dollars,
        "no_price": trade.no_price_dollars,
        "count": trade.count,
        "taker_side": trade.taker_side,
    }
    return {
        "source_id": KALSHI_LIVE_SOURCE_ID,
        "source_record_id": trade.trade_id,
        "source_publication_ts": trade.created_time,
        "fetch_ts": fetch_ts,
        "connector_version": CONNECTOR_VERSION,
        "source_payload_json": encode_payload(payload),
        "superseded_at": None,
        "trade_id": trade.trade_id,
        "ticker": trade.ticker,
        "created_time": trade.created_time,
        "yes_price_dollars": trade.yes_price_dollars,
        "no_price_dollars": trade.no_price_dollars,
        "count": trade.count,
        "taker_side": trade.taker_side,
    }


def _trade_rows_to_arrow(rows: list[dict[str, Any]]) -> pa.Table:
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
            ("trade_id", pa.string()),
            ("ticker", pa.string()),
            ("created_time", pa.timestamp("us", tz="UTC")),
            ("yes_price_dollars", pa.float64()),
            ("no_price_dollars", pa.float64()),
            ("count", pa.float64()),
            ("taker_side", pa.string()),
        ]
    )
    return pa.Table.from_pydict(column_data, schema=schema)


__all__ = ["TradesSyncReport", "sync_trades"]
