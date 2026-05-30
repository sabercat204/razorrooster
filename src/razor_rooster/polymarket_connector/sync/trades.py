"""Watched-markets trade pull (T-PMC-044; design §3.4; REQ-PMC-TRADE-001..003).

For each market in the operator's watched list, fetch trade history
from the CLOB ``/trades`` endpoint and persist into
``polymarket_trades``. Dedup is by the schema's primary key
``(tx_hash, outcome_token_id)`` so re-pulls are idempotent.

Watched markets are resolved from the loaded :class:`PolymarketConfig`
(``sync.prices.watched_markets``). Markets in the watched list that the
local store doesn't know about yet are skipped with a warning — they'll
be picked up by the next markets sync.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import duckdb
import pyarrow as pa

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.staging_merge import staging_merge
from razor_rooster.polymarket_connector.client.clob_public import (
    ClobPublicClient,
    Trade,
)
from razor_rooster.polymarket_connector.persistence.source import (
    POLYMARKET_LIVE_SOURCE_ID,
)
from razor_rooster.polymarket_connector.sync.markets import CONNECTOR_VERSION

logger = logging.getLogger(__name__)


# Soft cap on the number of trades pulled per market per run. Once the
# operator's watched-markets become liquid this is the lever that keeps
# any one market from monopolising the rate budget. v1 default is 5,000
# (enough for any plausible 24h trade volume on a watched market).
DEFAULT_TRADES_PER_MARKET: Final[int] = 5_000


@dataclass(slots=True)
class TradePullReport:
    """Outcome of one ``pull_watched_trades`` invocation."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    markets_evaluated: int = 0
    markets_skipped_unknown: int = 0
    markets_skipped_resolved: int = 0
    trades_inserted: int = 0
    trades_unchanged: int = 0
    trades_capped_at_limit: int = 0  # number of markets that hit the per-market cap
    market_errors: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def pull_watched_trades(
    store: DuckDBStore,
    *,
    client: ClobPublicClient,
    watched_markets: Iterable[str],
    trades_per_market: int = DEFAULT_TRADES_PER_MARKET,
    now: datetime | None = None,
) -> TradePullReport:
    """Pull trade history for every watched market.

    The ``watched_markets`` argument is a flat iterable of condition_ids;
    callers source it from ``polymarket.yaml.sync.prices.watched_markets``.
    An empty list is a clean no-op (REQ-PMC-TRADE-002).
    """
    started = now or datetime.now(tz=UTC)
    report = TradePullReport(started_at=started)

    watched = list(watched_markets)
    if not watched:
        completed = datetime.now(tz=UTC)
        report.completed_at = completed
        report.duration_seconds = (completed - started).total_seconds()
        logger.info("Polymarket watched-trades pull: no watched markets")
        return report

    with store.connection() as conn:
        known_markets = _load_known_market_metadata(conn, watched)

    for condition_id in watched:
        report.markets_evaluated += 1
        meta = known_markets.get(condition_id)
        if meta is None:
            logger.warning(
                "Polymarket watched-trades: skipping unknown market %s "
                "(run sync_markets first to register it)",
                condition_id,
            )
            report.markets_skipped_unknown += 1
            continue
        if meta.resolved:
            report.markets_skipped_resolved += 1
            # Resolved markets typically don't get new trades, but we
            # still pull once to capture the resolution-edge trades.
        try:
            collected, hit_cap = _collect_trades(
                client, market=condition_id, limit=trades_per_market
            )
        except Exception as exc:
            logger.warning("Polymarket trade pull failed for market %s: %s", condition_id, exc)
            report.market_errors.append((condition_id, f"{type(exc).__name__}: {exc}"))
            continue

        if hit_cap:
            report.trades_capped_at_limit += 1

        if not collected:
            continue

        try:
            with store.connection() as conn:
                arrow_batch = _trades_to_arrow(
                    collected, condition_id=condition_id, fetch_ts=started
                )
                merge_result = staging_merge(
                    conn,
                    target_table="polymarket_trades",
                    batch=arrow_batch,
                    dedup_keys=("tx_hash", "outcome_token_id"),
                )
                report.trades_inserted += merge_result.inserted + merge_result.revised
                report.trades_unchanged += merge_result.unchanged
        except Exception as exc:
            logger.exception("Polymarket watched-trades persist failed for market %s", condition_id)
            report.market_errors.append((condition_id, f"persist: {type(exc).__name__}: {exc}"))

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "Polymarket watched-trades pull done: evaluated=%d unknown=%d resolved=%d "
        "inserted=%d unchanged=%d errors=%d",
        report.markets_evaluated,
        report.markets_skipped_unknown,
        report.markets_skipped_resolved,
        report.trades_inserted,
        report.trades_unchanged,
        len(report.market_errors),
    )
    return report


# -- internals --------------------------------------------------------------


@dataclass(slots=True)
class _MarketMeta:
    """The active-row metadata we need from polymarket_markets."""

    condition_id: str
    resolved: bool


def _load_known_market_metadata(
    conn: duckdb.DuckDBPyConnection,
    watched: list[str],
) -> dict[str, _MarketMeta]:
    if not watched:
        return {}
    placeholders = ", ".join(["?"] * len(watched))
    rows = conn.execute(
        f"SELECT condition_id, resolved FROM polymarket_markets "
        f"WHERE superseded_at IS NULL AND condition_id IN ({placeholders})",
        list(watched),
    ).fetchall()
    return {str(r[0]): _MarketMeta(condition_id=str(r[0]), resolved=bool(r[1])) for r in rows}


def _collect_trades(
    client: ClobPublicClient,
    *,
    market: str,
    limit: int,
) -> tuple[list[Trade], bool]:
    """Drain trades for a market up to ``limit``. Returns (trades, hit_cap)."""
    collected: list[Trade] = []
    for trade in client.iter_trades(market=market):
        collected.append(trade)
        if len(collected) >= limit:
            return collected, True
    return collected, False


def _trades_to_arrow(
    trades: list[Trade],
    *,
    condition_id: str,
    fetch_ts: datetime,
) -> pa.Table:
    column_data: dict[str, list[Any]] = {
        "source_id": [],
        "source_record_id": [],
        "source_publication_ts": [],
        "fetch_ts": [],
        "connector_version": [],
        "source_payload_json": [],
        "superseded_at": [],
        "condition_id": [],
        "outcome_token_id": [],
        "trade_ts": [],
        "price": [],
        "size": [],
        "side": [],
        "tx_hash": [],
    }

    for trade in trades:
        if not trade.tx_hash or not trade.asset_id:
            # Trades missing the dedup key are unsalvageable.
            continue
        trade_ts = (
            datetime.fromtimestamp(trade.trade_ts_seconds, tz=UTC)
            if trade.trade_ts_seconds is not None
            else fetch_ts
        )
        column_data["source_id"].append(POLYMARKET_LIVE_SOURCE_ID)
        column_data["source_record_id"].append(f"{trade.tx_hash}:{trade.asset_id}")
        column_data["source_publication_ts"].append(trade_ts)
        column_data["fetch_ts"].append(fetch_ts)
        column_data["connector_version"].append(CONNECTOR_VERSION)
        column_data["source_payload_json"].append(json.dumps(trade.raw, default=str))
        column_data["superseded_at"].append(None)
        column_data["condition_id"].append(condition_id)
        column_data["outcome_token_id"].append(trade.asset_id)
        column_data["trade_ts"].append(trade_ts)
        column_data["price"].append(trade.price)
        column_data["size"].append(trade.size)
        column_data["side"].append(trade.side)
        column_data["tx_hash"].append(trade.tx_hash)

    schema = pa.schema(
        [
            ("source_id", pa.string()),
            ("source_record_id", pa.string()),
            ("source_publication_ts", pa.timestamp("us", tz="UTC")),
            ("fetch_ts", pa.timestamp("us", tz="UTC")),
            ("connector_version", pa.string()),
            ("source_payload_json", pa.string()),
            ("superseded_at", pa.timestamp("us", tz="UTC")),
            ("condition_id", pa.string()),
            ("outcome_token_id", pa.string()),
            ("trade_ts", pa.timestamp("us", tz="UTC")),
            ("price", pa.float64()),
            ("size", pa.float64()),
            ("side", pa.string()),
            ("tx_hash", pa.string()),
        ]
    )
    return pa.Table.from_pydict(column_data, schema=schema)
