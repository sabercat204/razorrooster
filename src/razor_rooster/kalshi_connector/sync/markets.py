"""Markets daily sync (T-KSI-041 step 3; design §3.4).

Reconciles ``kalshi_markets`` with Kalshi's current ``/markets`` listing:

1. Iterate every active series' open and closed-recently events.
2. For each event, paginate ``/markets?event_ticker=...`` (open status).
3. Diff against ``kalshi_markets``: insert new, upsert revised,
   ``removed_at`` for missing.
4. Persist via staging-merge.
5. (T-KSI-051 side effect) When ``sector_keywords`` is supplied, run
   the heuristic mapper for every upstream market and upsert into
   ``kalshi_sector_mapping``. Existing manual overrides are preserved.

All four market types (binary, scalar, categorical) and all four
strike-variants persist through the same path. Non-binary markets are
flagged at downstream filtering time (the comparator wiring in
T-KSI-061), not here.
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
from razor_rooster.kalshi_connector.client.models import KalshiMarket
from razor_rooster.kalshi_connector.client.rest import KalshiRESTClient
from razor_rooster.kalshi_connector.config.loader import (
    KalshiSectorKeywordsConfig,
)
from razor_rooster.kalshi_connector.mapping.sector_heuristic import map_sector
from razor_rooster.kalshi_connector.mapping.sector_overrides import (
    upsert_inferred_mapping,
)
from razor_rooster.kalshi_connector.persistence.source import (
    KALSHI_LIVE_SOURCE_ID,
)
from razor_rooster.kalshi_connector.sync._common import (
    CONNECTOR_VERSION,
    encode_payload,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MarketsSyncReport:
    """Outcome of one ``sync_markets`` invocation."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    markets_total_seen: int = 0
    markets_inserted: int = 0
    markets_updated: int = 0
    markets_unchanged: int = 0
    markets_removed: int = 0
    market_type_counts: dict[str, int] = field(default_factory=dict)
    mappings_upserted: int = 0
    mappings_skipped_manual: int = 0
    errors: list[str] = field(default_factory=list)


def sync_markets(
    store: DuckDBStore,
    *,
    client: KalshiRESTClient,
    event_tickers: Iterable[str] | None = None,
    statuses: Iterable[str] = ("open",),
    page_size: int = 100,
    sector_keywords: KalshiSectorKeywordsConfig | None = None,
    now: datetime | None = None,
) -> MarketsSyncReport:
    """Run one Kalshi markets daily sync.

    When ``sector_keywords`` is supplied, the function also runs the
    heuristic mapper against every upstream market and persists the
    results to ``kalshi_sector_mapping``. Existing manual overrides are
    preserved (T-KSI-051 contract).
    """
    started = now or datetime.now(tz=UTC)
    report = MarketsSyncReport(started_at=started)

    if event_tickers is None:
        with store.connection() as conn:
            tickers = sorted(_active_event_tickers(conn))
    else:
        tickers = sorted(set(event_tickers))

    if not tickers:
        logger.info("kalshi markets sync: no active events to scan")
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report

    upstream: list[KalshiMarket] = []
    try:
        for ticker in tickers:
            for status in statuses:
                cursor: str | None = None
                while True:
                    page = client.list_markets(
                        event_ticker=ticker,
                        status=status,
                        cursor=cursor,
                        limit=page_size,
                    )
                    for entry in page.items:
                        if isinstance(entry, KalshiMarket) and entry.ticker:
                            upstream.append(entry)
                    if page.cursor is None:
                        break
                    cursor = page.cursor
    except Exception as exc:
        logger.exception("Kalshi markets fetch failed")
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report

    seen: set[str] = set()
    deduped: list[KalshiMarket] = []
    for m in upstream:
        if m.ticker in seen:
            continue
        seen.add(m.ticker)
        deduped.append(m)
    report.markets_total_seen = len(deduped)
    for m in deduped:
        report.market_type_counts[m.market_type] = (
            report.market_type_counts.get(m.market_type, 0) + 1
        )

    with store.connection() as conn:
        existing_tickers = _existing_active_market_tickers(conn, event_tickers=tickers)
        upstream_tickers = {m.ticker for m in deduped}
        removed = existing_tickers - upstream_tickers

        if deduped:
            arrow_batch = _markets_to_arrow(deduped, fetch_ts=started)
            merge_result = staging_merge(
                conn,
                target_table="kalshi_markets",
                batch=arrow_batch,
            )
            report.markets_inserted = merge_result.inserted
            report.markets_updated = merge_result.revised
            report.markets_unchanged = merge_result.unchanged

        if removed:
            for ticker in sorted(removed):
                conn.execute(
                    "UPDATE kalshi_markets "
                    "SET removed_at = ? "
                    "WHERE ticker = ? AND removed_at IS NULL",
                    [started, ticker],
                )
            report.markets_removed = len(removed)

        # Sector heuristic side effect (T-KSI-051): every upstream market
        # gets a mapping refresh when the operator has supplied a
        # keyword catalogue. Existing manual overrides are preserved.
        if sector_keywords is not None and deduped:
            for m in deduped:
                mapping = map_sector(m, keywords=sector_keywords)
                if upsert_inferred_mapping(
                    conn,
                    ticker=m.ticker,
                    mapping=mapping,
                    when=started,
                ):
                    report.mappings_upserted += 1
                else:
                    report.mappings_skipped_manual += 1

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "kalshi markets sync: seen=%d inserted=%d updated=%d unchanged=%d removed=%d types=%s",
        report.markets_total_seen,
        report.markets_inserted,
        report.markets_updated,
        report.markets_unchanged,
        report.markets_removed,
        report.market_type_counts,
    )
    return report


# -- internals --------------------------------------------------------------


def _active_event_tickers(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT event_ticker FROM kalshi_events "
        "WHERE superseded_at IS NULL AND removed_at IS NULL"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _existing_active_market_tickers(
    conn: duckdb.DuckDBPyConnection, *, event_tickers: list[str]
) -> set[str]:
    if not event_tickers:
        return set()
    placeholders = ", ".join(["?"] * len(event_tickers))
    rows = conn.execute(
        f"SELECT DISTINCT ticker FROM kalshi_markets "
        f"WHERE superseded_at IS NULL AND removed_at IS NULL "
        f"AND event_ticker IN ({placeholders})",
        list(event_tickers),
    ).fetchall()
    return {str(r[0]) for r in rows}


def _markets_to_arrow(items: list[KalshiMarket], *, fetch_ts: datetime) -> pa.Table:
    column_data: dict[str, list[Any]] = {
        "source_id": [],
        "source_record_id": [],
        "source_publication_ts": [],
        "fetch_ts": [],
        "connector_version": [],
        "source_payload_json": [],
        "superseded_at": [],
        "ticker": [],
        "event_ticker": [],
        "series_ticker": [],
        "title": [],
        "sub_title": [],
        "market_type": [],
        "strike_type": [],
        "floor_strike": [],
        "cap_strike": [],
        "open_time": [],
        "close_time": [],
        "expiration_time": [],
        "expected_expiration_time": [],
        "latest_expiration_time": [],
        "settlement_timer_seconds": [],
        "status": [],
        "yes_sub_title": [],
        "no_sub_title": [],
        "result": [],
        "can_close_early": [],
        "expiration_value": [],
        "category": [],
        "risk_limit_cents": [],
        "notional_value": [],
        "tick_size": [],
        "last_price_dollars": [],
        "previous_yes_bid_dollars": [],
        "previous_yes_ask_dollars": [],
        "previous_price_dollars": [],
        "volume_24h": [],
        "volume": [],
        "liquidity": [],
        "open_interest": [],
        "created_at": [],
        "last_updated_at": [],
        "removed_at": [],
    }
    for m in items:
        publication_ts = m.last_updated_at or m.created_at or fetch_ts
        column_data["source_id"].append(KALSHI_LIVE_SOURCE_ID)
        column_data["source_record_id"].append(m.ticker)
        column_data["source_publication_ts"].append(publication_ts)
        column_data["fetch_ts"].append(fetch_ts)
        column_data["connector_version"].append(CONNECTOR_VERSION)
        column_data["source_payload_json"].append(encode_payload(dict(m.raw)))
        column_data["superseded_at"].append(None)
        column_data["ticker"].append(m.ticker)
        column_data["event_ticker"].append(m.event_ticker)
        column_data["series_ticker"].append(m.series_ticker)
        column_data["title"].append(m.title)
        column_data["sub_title"].append(m.sub_title)
        column_data["market_type"].append(m.market_type)
        column_data["strike_type"].append(m.strike_type)
        column_data["floor_strike"].append(m.floor_strike)
        column_data["cap_strike"].append(m.cap_strike)
        column_data["open_time"].append(m.open_time)
        column_data["close_time"].append(m.close_time)
        column_data["expiration_time"].append(m.expiration_time)
        column_data["expected_expiration_time"].append(m.expected_expiration_time)
        column_data["latest_expiration_time"].append(m.latest_expiration_time)
        column_data["settlement_timer_seconds"].append(m.settlement_timer_seconds)
        column_data["status"].append(m.status)
        column_data["yes_sub_title"].append(m.yes_sub_title)
        column_data["no_sub_title"].append(m.no_sub_title)
        column_data["result"].append(m.result)
        column_data["can_close_early"].append(m.can_close_early)
        column_data["expiration_value"].append(m.expiration_value)
        column_data["category"].append(m.category)
        column_data["risk_limit_cents"].append(m.risk_limit_cents)
        column_data["notional_value"].append(m.notional_value)
        column_data["tick_size"].append(m.tick_size)
        column_data["last_price_dollars"].append(m.last_price_dollars)
        column_data["previous_yes_bid_dollars"].append(m.previous_yes_bid_dollars)
        column_data["previous_yes_ask_dollars"].append(m.previous_yes_ask_dollars)
        column_data["previous_price_dollars"].append(m.previous_price_dollars)
        column_data["volume_24h"].append(m.volume_24h)
        column_data["volume"].append(m.volume)
        column_data["liquidity"].append(m.liquidity)
        column_data["open_interest"].append(m.open_interest)
        column_data["created_at"].append(m.created_at)
        column_data["last_updated_at"].append(m.last_updated_at)
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
            ("ticker", pa.string()),
            ("event_ticker", pa.string()),
            ("series_ticker", pa.string()),
            ("title", pa.string()),
            ("sub_title", pa.string()),
            ("market_type", pa.string()),
            ("strike_type", pa.string()),
            ("floor_strike", pa.float64()),
            ("cap_strike", pa.float64()),
            ("open_time", pa.timestamp("us", tz="UTC")),
            ("close_time", pa.timestamp("us", tz="UTC")),
            ("expiration_time", pa.timestamp("us", tz="UTC")),
            ("expected_expiration_time", pa.timestamp("us", tz="UTC")),
            ("latest_expiration_time", pa.timestamp("us", tz="UTC")),
            ("settlement_timer_seconds", pa.int32()),
            ("status", pa.string()),
            ("yes_sub_title", pa.string()),
            ("no_sub_title", pa.string()),
            ("result", pa.string()),
            ("can_close_early", pa.bool_()),
            ("expiration_value", pa.float64()),
            ("category", pa.string()),
            ("risk_limit_cents", pa.int32()),
            ("notional_value", pa.float64()),
            ("tick_size", pa.float64()),
            ("last_price_dollars", pa.float64()),
            ("previous_yes_bid_dollars", pa.float64()),
            ("previous_yes_ask_dollars", pa.float64()),
            ("previous_price_dollars", pa.float64()),
            ("volume_24h", pa.float64()),
            ("volume", pa.float64()),
            ("liquidity", pa.float64()),
            ("open_interest", pa.float64()),
            ("created_at", pa.timestamp("us", tz="UTC")),
            ("last_updated_at", pa.timestamp("us", tz="UTC")),
            ("removed_at", pa.timestamp("us", tz="UTC")),
        ]
    )
    return pa.Table.from_pydict(column_data, schema=schema)


__all__ = ["MarketsSyncReport", "sync_markets"]
