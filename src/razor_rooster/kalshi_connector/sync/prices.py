"""30-min price snapshots (T-KSI-042; design §3.4; REQ-KSI-PRICE-001..004).

For each active binary market in ``kalshi_markets``, snapshot the
top-of-book price fields using ``/markets/{ticker}`` (sufficient for
the bid/ask/last/volume metrics; the orderbook endpoint is reserved
for T-KSI-044). Computes ``mid_price_dollars``, ``spread_bps``, and
``liquidity_warning`` per the design's NULL-preservation rules.

Non-binary markets are persisted by ``sync_markets`` but skipped here
per OQ-KSI-003.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import duckdb
import pyarrow as pa

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.provenance import (
    update_last_successful_fetch,
)
from razor_rooster.data_ingest.persistence.staging_merge import staging_merge
from razor_rooster.kalshi_connector.client.rest import KalshiRESTClient
from razor_rooster.kalshi_connector.persistence.source import (
    KALSHI_LIVE_SOURCE_ID,
)
from razor_rooster.kalshi_connector.sync._common import (
    CONNECTOR_VERSION,
    encode_payload,
)

logger = logging.getLogger(__name__)


# REQ-KSI-PRICE-004: spread above this threshold sets liquidity_warning.
# 200 bps matches the Polymarket connector's default (REQ-PMC-PRICE-004)
# so cross-venue analyses see consistent thresholds. Configurable via
# ``wide_spread_warning_bps``.
DEFAULT_WIDE_SPREAD_BPS: Final[int] = 200

# Snapshot source tag used in kalshi_price_snapshots.snapshot_source.
# v1 only emits 'rest'; future RTDS or websocket work would add new tags.
_SNAPSHOT_SOURCE_REST: Final[str] = "rest"


@dataclass(slots=True)
class PriceSnapshotReport:
    """Outcome of one ``snapshot_prices`` invocation."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    markets_evaluated: int = 0
    markets_skipped_non_binary: int = 0
    snapshots_inserted: int = 0
    snapshots_unchanged: int = 0
    snapshots_thin_book: int = 0
    market_errors: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def snapshot_prices(
    store: DuckDBStore,
    *,
    client: KalshiRESTClient,
    market_filter: Iterable[str] | None = None,
    wide_spread_warning_bps: int = DEFAULT_WIDE_SPREAD_BPS,
    now: datetime | None = None,
) -> PriceSnapshotReport:
    """Snapshot prices for active binary Kalshi markets.

    Args:
        store: DuckDB store with the Kalshi schemas applied.
        client: Configured Kalshi REST client.
        market_filter: Optional set of tickers; when provided, only
            those markets are snapshotted. Used by the watched-markets
            higher-frequency cadence.
        wide_spread_warning_bps: Spread threshold (in basis points) above
            which ``liquidity_warning`` is set TRUE on a snapshot.
        now: Override timestamp for testing / replay.
    """
    started = now or datetime.now(tz=UTC)
    report = PriceSnapshotReport(started_at=started)

    filter_set: set[str] | None = (
        {str(t) for t in market_filter} if market_filter is not None else None
    )

    with store.connection() as conn:
        active_markets = _load_active_binary_markets(conn, filter_set=filter_set)
    report.markets_evaluated = len(active_markets)

    rows: list[dict[str, Any]] = []
    for ticker, market_type in active_markets:
        if market_type != "binary":
            report.markets_skipped_non_binary += 1
            continue
        try:
            market = client.get_market(ticker)
        except Exception as exc:
            logger.warning(
                "kalshi price snapshot failed for ticker %s: %s",
                ticker,
                exc,
            )
            report.market_errors.append((ticker, f"{type(exc).__name__}: {exc}"))
            continue
        row = _build_snapshot_row(
            ticker=ticker,
            market=market,
            snapshot_ts=started,
            wide_spread_warning_bps=wide_spread_warning_bps,
        )
        rows.append(row)
        if row["liquidity_warning"]:
            report.snapshots_thin_book += 1

    if rows:
        try:
            with store.connection() as conn:
                arrow_batch = _snapshot_rows_to_arrow(rows)
                merge_result = staging_merge(
                    conn,
                    target_table="kalshi_price_snapshots",
                    batch=arrow_batch,
                    dedup_keys=("ticker", "snapshot_ts"),
                )
                report.snapshots_inserted = merge_result.inserted + merge_result.revised
                report.snapshots_unchanged = merge_result.unchanged
                update_last_successful_fetch(conn, KALSHI_LIVE_SOURCE_ID, when=started)
        except Exception as exc:
            logger.exception("kalshi price-snapshot persist failed")
            report.errors.append(f"persist: {type(exc).__name__}: {exc}")

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "kalshi price snapshot: evaluated=%d binary_processed=%d inserted=%d "
        "thin_book=%d errors=%d",
        report.markets_evaluated,
        report.markets_evaluated - report.markets_skipped_non_binary,
        report.snapshots_inserted,
        report.snapshots_thin_book,
        len(report.market_errors),
    )
    return report


# -- internals --------------------------------------------------------------


def _load_active_binary_markets(
    conn: duckdb.DuckDBPyConnection,
    *,
    filter_set: set[str] | None,
) -> list[tuple[str, str]]:
    """Return ``(ticker, market_type)`` pairs to consider for snapshotting."""
    if filter_set is not None and not filter_set:
        return []
    base_query = (
        "SELECT ticker, market_type FROM kalshi_markets "
        "WHERE superseded_at IS NULL AND removed_at IS NULL "
        "AND status = 'open'"
    )
    if filter_set is not None:
        placeholders = ", ".join(["?"] * len(filter_set))
        rows = conn.execute(
            f"{base_query} AND ticker IN ({placeholders})",
            list(filter_set),
        ).fetchall()
    else:
        rows = conn.execute(base_query).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _build_snapshot_row(
    *,
    ticker: str,
    market: Any,
    snapshot_ts: datetime,
    wide_spread_warning_bps: int,
) -> dict[str, Any]:
    """Construct one ``kalshi_price_snapshots`` row dict.

    NULL preservation is strict: if the upstream payload doesn't carry
    a field, it stays NULL rather than being imputed.
    ``liquidity_warning`` is TRUE iff:

    - either side of the orderbook is unavailable, OR
    - the bid/ask spread is greater than ``wide_spread_warning_bps``.
    """
    yes_bid = market.previous_yes_bid_dollars
    yes_ask = market.previous_yes_ask_dollars
    last_trade_price = market.last_price_dollars
    mid_price: float | None = None
    spread_bps: int | None = None
    liquidity_warning = False
    if yes_bid is not None and yes_ask is not None:
        mid_price = (yes_bid + yes_ask) / 2.0
        if mid_price > 0:
            spread = yes_ask - yes_bid
            spread_bps = round(spread / mid_price * 10_000)
            if spread_bps >= wide_spread_warning_bps:
                liquidity_warning = True
    else:
        liquidity_warning = True

    source_record_id = f"{ticker}:{snapshot_ts.isoformat()}"
    payload = {
        "ticker": ticker,
        "previous_yes_bid": yes_bid,
        "previous_yes_ask": yes_ask,
        "last_price": last_trade_price,
        "volume_24h": market.volume_24h,
        "volume": market.volume,
        "liquidity": market.liquidity,
        "open_interest": market.open_interest,
    }

    return {
        "source_id": KALSHI_LIVE_SOURCE_ID,
        "source_record_id": source_record_id,
        "source_publication_ts": snapshot_ts,
        "fetch_ts": snapshot_ts,
        "connector_version": CONNECTOR_VERSION,
        "source_payload_json": encode_payload(payload),
        "superseded_at": None,
        "ticker": ticker,
        "snapshot_ts": snapshot_ts,
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": yes_ask,
        "mid_price_dollars": mid_price,
        "last_trade_price_dollars": last_trade_price,
        "last_trade_ts": None,
        "volume_24h": market.volume_24h,
        "volume_total": market.volume,
        "open_interest": market.open_interest,
        "liquidity": market.liquidity,
        "liquidity_warning": liquidity_warning,
        "spread_bps": spread_bps,
        "snapshot_source": _SNAPSHOT_SOURCE_REST,
    }


def _snapshot_rows_to_arrow(rows: list[dict[str, Any]]) -> pa.Table:
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
            ("yes_bid_dollars", pa.float64()),
            ("yes_ask_dollars", pa.float64()),
            ("mid_price_dollars", pa.float64()),
            ("last_trade_price_dollars", pa.float64()),
            ("last_trade_ts", pa.timestamp("us", tz="UTC")),
            ("volume_24h", pa.float64()),
            ("volume_total", pa.float64()),
            ("open_interest", pa.float64()),
            ("liquidity", pa.float64()),
            ("liquidity_warning", pa.bool_()),
            ("spread_bps", pa.int32()),
            ("snapshot_source", pa.string()),
        ]
    )
    return pa.Table.from_pydict(column_data, schema=schema)


__all__ = [
    "DEFAULT_WIDE_SPREAD_BPS",
    "PriceSnapshotReport",
    "snapshot_prices",
]
