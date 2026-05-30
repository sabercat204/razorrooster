"""Cutoff snapshot (T-KSI-040; design §3.4 / OQ-KSI-004 resolution).

Snapshot ``/historical/cutoff`` once at the start of each cycle and
persist to ``kalshi_historical_cutoff`` (single-row replace). Subsequent
sync operations within the cycle consult the persisted row rather than
re-fetching, so their routing decisions stay consistent even if the
real cutoff advances mid-cycle.

Idempotency: running ``snapshot_cutoff`` twice within the same second
replaces the row twice; both writes stamp ``fetched_at`` with the
current time so the most recent always wins.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.kalshi_connector.client.models import KalshiHistoricalCutoff
from razor_rooster.kalshi_connector.client.rest import KalshiRESTClient

logger = logging.getLogger(__name__)


def snapshot_cutoff(
    store: DuckDBStore,
    *,
    client: KalshiRESTClient,
    now: datetime | None = None,
) -> KalshiHistoricalCutoff:
    """Fetch ``/historical/cutoff`` and replace the single state row.

    Returns the parsed cutoff for use by sibling sync operations within
    the same cycle. The persisted row's ``fetched_at`` is the moment
    this function received the response.
    """
    when = now or datetime.now(tz=UTC)
    cutoff = client.get_historical_cutoff()
    # Override the model's ``fetched_at`` with the caller's ``now`` if
    # supplied so tests can assert on a deterministic timestamp.
    cutoff = KalshiHistoricalCutoff(
        market_settled_ts=cutoff.market_settled_ts,
        trades_created_ts=cutoff.trades_created_ts,
        orders_updated_ts=cutoff.orders_updated_ts,
        fetched_at=when,
    )
    with store.connection() as conn:
        # Single-row replace: delete-then-insert keeps the table at one
        # row regardless of how many cycles have run.
        conn.execute("DELETE FROM kalshi_historical_cutoff")
        conn.execute(
            "INSERT INTO kalshi_historical_cutoff "
            "(market_settled_ts, trades_created_ts, orders_updated_ts, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            [
                cutoff.market_settled_ts,
                cutoff.trades_created_ts,
                cutoff.orders_updated_ts,
                cutoff.fetched_at,
            ],
        )
    logger.info(
        "kalshi cutoff snapshot: market_settled_ts=%s trades_created_ts=%s",
        cutoff.market_settled_ts.isoformat(),
        cutoff.trades_created_ts.isoformat(),
    )
    return cutoff


def read_cutoff(store: DuckDBStore) -> KalshiHistoricalCutoff | None:
    """Return the persisted cutoff row, or None if no snapshot exists yet."""
    with store.connection() as conn:
        row = conn.execute(
            "SELECT market_settled_ts, trades_created_ts, orders_updated_ts, fetched_at "
            "FROM kalshi_historical_cutoff LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return KalshiHistoricalCutoff(
        market_settled_ts=row[0],
        trades_created_ts=row[1],
        orders_updated_ts=row[2],
        fetched_at=row[3],
    )


__all__ = [
    "read_cutoff",
    "snapshot_cutoff",
]
