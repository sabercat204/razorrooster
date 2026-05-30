"""Hourly price snapshot sync (T-PMC-041; design §3.4; REQ-PMC-PRICE-001..004).

For each active binary market in ``polymarket_markets``, fetch the
orderbook via the CLOB public client and write one
``polymarket_price_snapshots`` row per outcome token. Computes
``spread_bps`` and ``liquidity_warning`` per REQ-PMC-PRICE-004. NULL
preservation is strict — missing fields stay NULL rather than being
imputed.

Multi-outcome and negRisk markets are skipped per OQ-PMC-004 (deferred
to v1.1).

Concurrency: this sync runs sequentially across markets because the
shared rate-limit token bucket already serializes the network side.
Future v1.5 work could parallelize the inner loop using the same bucket
without changing the contract here.
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
from razor_rooster.data_ingest.persistence.provenance import (
    update_last_successful_fetch,
)
from razor_rooster.data_ingest.persistence.staging_merge import staging_merge
from razor_rooster.polymarket_connector.client.clob_public import (
    ClobPublicClient,
    Orderbook,
)
from razor_rooster.polymarket_connector.persistence.source import (
    POLYMARKET_LIVE_SOURCE_ID,
)
from razor_rooster.polymarket_connector.sync.markets import CONNECTOR_VERSION

logger = logging.getLogger(__name__)


# REQ-PMC-PRICE-004 — markets with sparse orderbooks (no bid or no ask)
# get liquidity_warning=TRUE. The threshold below covers the case where
# both sides exist but the spread is wide enough to suggest the
# orderbook is too thin to trust the midpoint as a reasonable price.
# 200 bps (2 percentage points) is the v1 default. Configurable via the
# ``wide_spread_warning_bps`` argument to ``snapshot_prices``.
DEFAULT_WIDE_SPREAD_BPS: Final[int] = 200

# Snapshot source tag used in polymarket_price_snapshots.snapshot_source.
# v1 only emits 'rest'; OQ-PMC-002 reserves 'rtds' for v2+.
_SNAPSHOT_SOURCE_REST: Final[str] = "rest"


@dataclass(slots=True)
class _ActiveMarket:
    """One active binary market with its outcome tokens."""

    condition_id: str
    outcome_tokens: list[tuple[str, str]]  # (token_id, outcome_label)
    market_type: str


@dataclass(slots=True)
class PriceSnapshotReport:
    """Outcome of one ``snapshot_prices`` invocation."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    markets_evaluated: int = 0
    markets_skipped_non_binary: int = 0
    markets_skipped_no_tokens: int = 0
    snapshots_inserted: int = 0
    snapshots_unchanged: int = 0
    snapshots_thin_book: int = 0  # liquidity_warning = TRUE
    market_errors: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def snapshot_prices(
    store: DuckDBStore,
    *,
    client: ClobPublicClient,
    market_filter: Iterable[str] | None = None,
    wide_spread_warning_bps: int = DEFAULT_WIDE_SPREAD_BPS,
    now: datetime | None = None,
) -> PriceSnapshotReport:
    """Snapshot prices for the active binary markets.

    Args:
        store: DuckDB store with the Polymarket schemas applied.
        client: Configured CLOB public client.
        market_filter: Optional set of condition_ids; if provided, only
            those markets are snapshotted. Used by the watched-markets
            higher-frequency cadence.
        wide_spread_warning_bps: Spread threshold (in basis points) above
            which ``liquidity_warning`` is set to TRUE on a snapshot.
            Default 200 bps.
        now: Override timestamp for testing / replay.

    Returns:
        :class:`PriceSnapshotReport` with per-bucket counts.
    """
    started = now or datetime.now(tz=UTC)
    report = PriceSnapshotReport(started_at=started)

    filter_set: set[str] | None = (
        {str(m) for m in market_filter} if market_filter is not None else None
    )

    with store.connection() as conn:
        markets = _load_active_binary_markets(conn, filter_set=filter_set)

    report.markets_evaluated = len(markets)

    snapshots_to_persist: list[dict[str, Any]] = []
    for market in markets:
        if market.market_type != "binary":
            report.markets_skipped_non_binary += 1
            continue
        if not market.outcome_tokens:
            report.markets_skipped_no_tokens += 1
            continue
        try:
            for token_id, _outcome_label in market.outcome_tokens:
                orderbook = client.get_orderbook(token_id)
                row = _build_snapshot_row(
                    market=market,
                    token_id=token_id,
                    orderbook=orderbook,
                    snapshot_ts=started,
                    wide_spread_warning_bps=wide_spread_warning_bps,
                )
                snapshots_to_persist.append(row)
                if row["liquidity_warning"]:
                    report.snapshots_thin_book += 1
        except Exception as exc:
            logger.warning(
                "Polymarket price snapshot failed for market %s: %s",
                market.condition_id,
                exc,
            )
            report.market_errors.append((market.condition_id, f"{type(exc).__name__}: {exc}"))

    if snapshots_to_persist:
        try:
            with store.connection() as conn:
                arrow_batch = _snapshots_to_arrow(snapshots_to_persist, fetch_ts=started)
                merge_result = staging_merge(
                    conn,
                    target_table="polymarket_price_snapshots",
                    batch=arrow_batch,
                    dedup_keys=("condition_id", "outcome_token_id", "snapshot_ts"),
                )
                report.snapshots_inserted = merge_result.inserted + merge_result.revised
                report.snapshots_unchanged = merge_result.unchanged
                update_last_successful_fetch(conn, POLYMARKET_LIVE_SOURCE_ID, when=started)
        except Exception as exc:
            logger.exception("Polymarket price-snapshot persist failed")
            report.errors.append(f"persist: {type(exc).__name__}: {exc}")

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "Polymarket price snapshot done: evaluated=%d binary_markets_processed=%d "
        "snapshots_inserted=%d thin_book=%d market_errors=%d",
        report.markets_evaluated,
        report.markets_evaluated
        - report.markets_skipped_non_binary
        - report.markets_skipped_no_tokens,
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
) -> list[_ActiveMarket]:
    """Read the current active markets the price sync should snapshot.

    "Active" here means the row is current (``superseded_at IS NULL``)
    and not removed (``removed_at IS NULL``). The market's
    ``market_type`` is preserved on the result so the snapshot loop can
    skip multi-outcome markets per OQ-PMC-004 without an extra DB call.
    """
    if filter_set is not None and not filter_set:
        return []

    base_query = (
        "SELECT condition_id, market_type, outcome_tokens "
        "FROM polymarket_markets "
        "WHERE superseded_at IS NULL AND removed_at IS NULL AND active = TRUE"
    )
    if filter_set is not None:
        placeholders = ", ".join(["?"] * len(filter_set))
        rows = conn.execute(
            f"{base_query} AND condition_id IN ({placeholders})",
            list(filter_set),
        ).fetchall()
    else:
        rows = conn.execute(base_query).fetchall()

    results: list[_ActiveMarket] = []
    for condition_id, market_type, outcome_tokens_json in rows:
        try:
            tokens_payload = json.loads(outcome_tokens_json) if outcome_tokens_json else []
        except json.JSONDecodeError:
            tokens_payload = []
        outcome_tokens: list[tuple[str, str]] = []
        if isinstance(tokens_payload, list):
            for entry in tokens_payload:
                if not isinstance(entry, dict):
                    continue
                token_id = entry.get("token_id")
                outcome_label = entry.get("outcome_label", "")
                if token_id:
                    outcome_tokens.append((str(token_id), str(outcome_label)))
        results.append(
            _ActiveMarket(
                condition_id=str(condition_id),
                market_type=str(market_type),
                outcome_tokens=outcome_tokens,
            )
        )
    return results


def _build_snapshot_row(
    *,
    market: _ActiveMarket,
    token_id: str,
    orderbook: Orderbook | None,
    snapshot_ts: datetime,
    wide_spread_warning_bps: int,
) -> dict[str, Any]:
    """Construct one ``polymarket_price_snapshots`` row dict.

    NULL preservation per REQ-PMC-PRICE-004: any field whose source is
    absent stays NULL. ``liquidity_warning`` is TRUE iff:

    - either side of the orderbook is empty, OR
    - the bid/ask spread is greater than ``wide_spread_warning_bps``.
    """
    best_bid_price: float | None = None
    best_ask_price: float | None = None
    last_trade_price: float | None = None
    mid_price: float | None = None
    spread_bps: int | None = None
    liquidity_warning = False

    if orderbook is None:
        liquidity_warning = True
    else:
        if orderbook.best_bid is not None:
            best_bid_price = orderbook.best_bid.price
        if orderbook.best_ask is not None:
            best_ask_price = orderbook.best_ask.price
        last_trade_price = orderbook.last_trade_price

        if best_bid_price is not None and best_ask_price is not None:
            mid_price = (best_bid_price + best_ask_price) / 2.0
            if mid_price > 0:
                spread = best_ask_price - best_bid_price
                spread_bps = round(spread / mid_price * 10_000)
                if spread_bps >= wide_spread_warning_bps:
                    liquidity_warning = True
        else:
            liquidity_warning = True

    raw_payload: dict[str, Any] = (
        orderbook.raw if orderbook is not None else {"orderbook_unavailable": True}
    )

    # source_record_id encodes (condition_id, token_id, snapshot_ts) so
    # the staging-merge dedup key remains unique across re-runs.
    source_record_id = f"{market.condition_id}:{token_id}:{snapshot_ts.isoformat()}"

    return {
        "source_id": POLYMARKET_LIVE_SOURCE_ID,
        "source_record_id": source_record_id,
        "source_publication_ts": snapshot_ts,
        "fetch_ts": snapshot_ts,
        "connector_version": CONNECTOR_VERSION,
        "source_payload_json": json.dumps(raw_payload, default=str),
        "superseded_at": None,
        "condition_id": market.condition_id,
        "outcome_token_id": token_id,
        "snapshot_ts": snapshot_ts,
        "mid_price": mid_price,
        "best_bid": best_bid_price,
        "best_ask": best_ask_price,
        "last_trade_price": last_trade_price,
        "last_trade_ts": None,
        "volume_24h": None,
        "liquidity_warning": liquidity_warning,
        "spread_bps": spread_bps,
        "snapshot_source": _SNAPSHOT_SOURCE_REST,
    }


def _snapshots_to_arrow(
    rows: list[dict[str, Any]],
    *,
    fetch_ts: datetime,
) -> pa.Table:
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
            ("condition_id", pa.string()),
            ("outcome_token_id", pa.string()),
            ("snapshot_ts", pa.timestamp("us", tz="UTC")),
            ("mid_price", pa.float64()),
            ("best_bid", pa.float64()),
            ("best_ask", pa.float64()),
            ("last_trade_price", pa.float64()),
            ("last_trade_ts", pa.timestamp("us", tz="UTC")),
            ("volume_24h", pa.float64()),
            ("liquidity_warning", pa.bool_()),
            ("spread_bps", pa.int32()),
            ("snapshot_source", pa.string()),
        ]
    )
    return pa.Table.from_pydict(column_data, schema=schema)
