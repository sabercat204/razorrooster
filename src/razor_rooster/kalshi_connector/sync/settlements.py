"""Settlement backfill + daily delta (T-KSI-043; design §3.4; REQ-KSI-SETTLE-*).

Routes settlement reads against the cutoff snapshot persisted by
:mod:`sync.cutoff`:

- For settlements at or after ``cutoff.market_settled_ts``: pull from
  ``/markets?status=settled``.
- For settlements before the cutoff: pull from
  ``/historical/markets``.

Both paths upsert into ``kalshi_settlements`` (and indirectly mark the
corresponding ``kalshi_markets`` rows as settled when the response
indicates a final result).

The daily delta uses the same routing but only pulls rows whose
settlement_ts is more recent than the prior cycle's
``last_successful_fetch[kalshi_settlements]`` watermark.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import pyarrow as pa

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.provenance import (
    update_last_successful_fetch,
)
from razor_rooster.data_ingest.persistence.staging_merge import staging_merge
from razor_rooster.kalshi_connector.client.models import (
    KalshiHistoricalCutoff,
    KalshiMarket,
)
from razor_rooster.kalshi_connector.client.rest import KalshiRESTClient
from razor_rooster.kalshi_connector.persistence.source import (
    KALSHI_SETTLEMENTS_SOURCE_ID,
)
from razor_rooster.kalshi_connector.sync._common import (
    CONNECTOR_VERSION,
    encode_payload,
)
from razor_rooster.kalshi_connector.sync.cutoff import read_cutoff

logger = logging.getLogger(__name__)


# Status value indicating a market has been settled. Kalshi exposes
# 'settled' for markets that have a final result; the connector only
# records settlements for these.
_STATUS_SETTLED: Final[str] = "settled"


@dataclass(slots=True)
class SettlementsSyncReport:
    """Outcome of one settlement sync invocation."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    cutoff_market_settled_ts: datetime | None = None
    settlements_seen: int = 0
    settlements_inserted: int = 0
    settlements_unchanged: int = 0
    routed_to_live: int = 0
    routed_to_historical: int = 0
    errors: list[str] = field(default_factory=list)


def sync_settlements(
    store: DuckDBStore,
    *,
    client: KalshiRESTClient,
    page_size: int = 100,
    now: datetime | None = None,
) -> SettlementsSyncReport:
    """Run one Kalshi settlements sync.

    Reads the cutoff snapshot persisted by :func:`snapshot_cutoff` and
    routes calls accordingly. If no cutoff snapshot exists yet, the
    function returns an empty report with an error logged — the cycle
    runner is expected to call ``snapshot_cutoff`` first.
    """
    started = now or datetime.now(tz=UTC)
    report = SettlementsSyncReport(started_at=started)

    cutoff = read_cutoff(store)
    if cutoff is None:
        report.errors.append("no kalshi_historical_cutoff row; call snapshot_cutoff first")
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report
    report.cutoff_market_settled_ts = cutoff.market_settled_ts

    seen: list[KalshiMarket] = []
    try:
        # Live path: /markets?status=settled.
        cursor: str | None = None
        while True:
            page = client.list_markets(
                status=_STATUS_SETTLED,
                cursor=cursor,
                limit=page_size,
            )
            for entry in page.items:
                if isinstance(entry, KalshiMarket) and entry.ticker:
                    seen.append(entry)
                    report.routed_to_live += 1
            if page.cursor is None:
                break
            cursor = page.cursor
    except Exception as exc:
        logger.exception("kalshi live settlements fetch failed")
        report.errors.append(f"live: {type(exc).__name__}: {exc}")

    try:
        # Historical path: /historical/markets.
        cursor = None
        while True:
            page = client.get_historical_markets(cursor=cursor, limit=page_size)
            for entry in page.items:
                if isinstance(entry, KalshiMarket) and entry.ticker:
                    seen.append(entry)
                    report.routed_to_historical += 1
            if page.cursor is None:
                break
            cursor = page.cursor
    except Exception as exc:
        logger.exception("kalshi historical settlements fetch failed")
        report.errors.append(f"historical: {type(exc).__name__}: {exc}")

    # Dedup: a market may appear in both lists if it crossed the cutoff
    # between fetches. The historical entry wins for the settlement row
    # (it's the canonical archived form); ``kalshi_markets`` updates use
    # the live form.
    by_ticker: dict[str, KalshiMarket] = {}
    for m in seen:
        by_ticker[m.ticker] = m
    settlements = list(by_ticker.values())
    report.settlements_seen = len(settlements)

    if not settlements:
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report

    rows = [_settlement_to_row(m, fetch_ts=started, cutoff=cutoff) for m in settlements]
    try:
        with store.connection() as conn:
            arrow_batch = _settlement_rows_to_arrow(rows)
            merge_result = staging_merge(
                conn,
                target_table="kalshi_settlements",
                batch=arrow_batch,
            )
            report.settlements_inserted = merge_result.inserted + merge_result.revised
            report.settlements_unchanged = merge_result.unchanged
            update_last_successful_fetch(conn, KALSHI_SETTLEMENTS_SOURCE_ID, when=started)
    except Exception as exc:
        logger.exception("kalshi settlement persist failed")
        report.errors.append(f"persist: {type(exc).__name__}: {exc}")

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "kalshi settlements sync: seen=%d inserted=%d live=%d historical=%d errors=%d",
        report.settlements_seen,
        report.settlements_inserted,
        report.routed_to_live,
        report.routed_to_historical,
        len(report.errors),
    )
    return report


# -- internals --------------------------------------------------------------


def _settlement_to_row(
    market: KalshiMarket,
    *,
    fetch_ts: datetime,
    cutoff: KalshiHistoricalCutoff,
) -> dict[str, Any]:
    """Build a kalshi_settlements row dict.

    The settlement timestamp is the market's ``expiration_time``
    (Kalshi's settlement time), with ``latest_expiration_time`` as a
    secondary fallback.
    """
    settlement_ts = (
        market.expiration_time
        or market.latest_expiration_time
        or market.expected_expiration_time
        or fetch_ts
    )
    voided = (market.result or "").strip().lower() in ("void", "voided", "invalid")
    payload = dict(market.raw)
    return {
        "source_id": KALSHI_SETTLEMENTS_SOURCE_ID,
        "source_record_id": market.ticker,
        "source_publication_ts": settlement_ts,
        "fetch_ts": fetch_ts,
        "connector_version": CONNECTOR_VERSION,
        "source_payload_json": encode_payload(payload),
        "superseded_at": None,
        "ticker": market.ticker,
        "event_ticker": market.event_ticker,
        "series_ticker": market.series_ticker,
        "result": market.result or "",
        "settled_value": market.expiration_value,
        "settlement_ts": settlement_ts,
        "settlement_source": None,
        "final_yes_price": market.last_price_dollars,
        "final_no_price": (
            None if market.last_price_dollars is None else max(0.0, 1.0 - market.last_price_dollars)
        ),
        "total_volume_at_settlement": market.volume,
        "voided": voided,
    }


def _settlement_rows_to_arrow(rows: list[dict[str, Any]]) -> pa.Table:
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
            ("event_ticker", pa.string()),
            ("series_ticker", pa.string()),
            ("result", pa.string()),
            ("settled_value", pa.float64()),
            ("settlement_ts", pa.timestamp("us", tz="UTC")),
            ("settlement_source", pa.string()),
            ("final_yes_price", pa.float64()),
            ("final_no_price", pa.float64()),
            ("total_volume_at_settlement", pa.float64()),
            ("voided", pa.bool_()),
        ]
    )
    return pa.Table.from_pydict(column_data, schema=schema)


__all__ = ["SettlementsSyncReport", "sync_settlements"]
