"""On-demand orderbook fetch (T-PMC-045; design §3.4; REQ-PMC-OB-001..002).

Thin wrapper around :class:`ClobPublicClient.get_orderbook`. The default
behavior returns the orderbook in-memory; ``persist=True`` writes a snapshot
into ``polymarket_orderbook_snapshots`` (one row per side+level).

Use cases:

- Operator runs ``razor-rooster polymarket fetch-orderbook <id>`` for ad-hoc
  inspection (no persistence by default).
- ``mispricing_detector`` calls this with ``persist=False`` to get a fresh
  depth read without polluting the historical orderbook table.
- Watched-markets configured with ``orderbook=True`` call this with
  ``persist=True`` once per cadence interval.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import duckdb
import pyarrow as pa

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
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


# Default depth (per side) when the caller doesn't specify. The CLOB
# returns whatever depth it has; this constant is informational.
DEFAULT_DEPTH: Final[int] = 10


@dataclass(slots=True)
class OrderbookFetchReport:
    """Result of one ``fetch_orderbook`` call."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    condition_id: str = ""
    outcome_token_id: str = ""
    persisted: bool = False
    persisted_levels: int = 0
    orderbook: Orderbook | None = None
    errors: list[str] = field(default_factory=list)


def fetch_orderbook(
    *,
    client: ClobPublicClient,
    condition_id: str,
    outcome_token_id: str,
    depth: int = DEFAULT_DEPTH,
    persist: bool = False,
    store: DuckDBStore | None = None,
    now: datetime | None = None,
) -> OrderbookFetchReport:
    """Fetch one orderbook; optionally persist a snapshot.

    Args:
        client: CLOB public client.
        condition_id: The market's condition_id (recorded on persisted rows;
            not used for the API call itself).
        outcome_token_id: Token id passed to ``GET /book?token_id=...``.
        depth: Reserved; the CLOB returns its own depth.
        persist: If True, write a snapshot to
            ``polymarket_orderbook_snapshots``. ``store`` is required when
            ``persist=True``.
        store: DuckDB store handle. Required iff ``persist=True``.
        now: Override timestamp.

    Returns:
        :class:`OrderbookFetchReport` with the in-memory orderbook (or
        ``None`` on 404), per-side level count, and persistence outcome.
    """
    started = now or datetime.now(tz=UTC)
    report = OrderbookFetchReport(
        started_at=started,
        condition_id=condition_id,
        outcome_token_id=outcome_token_id,
    )

    if persist and store is None:
        raise ValueError("persist=True requires a DuckDBStore handle")

    try:
        orderbook = client.get_orderbook(outcome_token_id)
    except Exception as exc:
        logger.exception("Polymarket orderbook fetch failed for token %s", outcome_token_id)
        report.errors.append(f"{type(exc).__name__}: {exc}")
        completed = datetime.now(tz=UTC)
        report.completed_at = completed
        report.duration_seconds = (completed - started).total_seconds()
        return report

    report.orderbook = orderbook

    if persist and orderbook is not None:
        assert store is not None  # narrowed by the guard above
        try:
            with store.connection() as conn:
                rows = _orderbook_to_rows(
                    orderbook,
                    condition_id=condition_id,
                    outcome_token_id=outcome_token_id,
                    snapshot_ts=started,
                )
                if rows:
                    arrow_batch = _rows_to_arrow(rows)
                    staging_merge(
                        conn,
                        target_table="polymarket_orderbook_snapshots",
                        batch=arrow_batch,
                        dedup_keys=(
                            "condition_id",
                            "outcome_token_id",
                            "snapshot_ts",
                            "side",
                            "level",
                        ),
                    )
                    report.persisted = True
                    report.persisted_levels = len(rows)
        except Exception as exc:
            logger.exception("Polymarket orderbook persist failed")
            report.errors.append(f"persist: {type(exc).__name__}: {exc}")

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    return report


# -- internals --------------------------------------------------------------


def _orderbook_to_rows(
    orderbook: Orderbook,
    *,
    condition_id: str,
    outcome_token_id: str,
    snapshot_ts: datetime,
) -> list[dict[str, Any]]:
    """One row per (side, level) for staging-merge."""
    rows: list[dict[str, Any]] = []
    raw_payload = json.dumps(orderbook.raw, default=str)
    record_id_prefix = f"{condition_id}:{outcome_token_id}:{snapshot_ts.isoformat()}"

    def _emit(side: str, levels: tuple[Any, ...]) -> None:
        for level_index, level in enumerate(levels):
            rows.append(
                {
                    "source_id": POLYMARKET_LIVE_SOURCE_ID,
                    "source_record_id": f"{record_id_prefix}:{side}:{level_index}",
                    "source_publication_ts": snapshot_ts,
                    "fetch_ts": snapshot_ts,
                    "connector_version": CONNECTOR_VERSION,
                    "source_payload_json": raw_payload,
                    "superseded_at": None,
                    "condition_id": condition_id,
                    "outcome_token_id": outcome_token_id,
                    "snapshot_ts": snapshot_ts,
                    "side": side,
                    "level": level_index,
                    "price": float(level.price),
                    "size": float(level.size),
                }
            )

    _emit("bid", orderbook.bids)
    _emit("ask", orderbook.asks)
    return rows


def _rows_to_arrow(rows: list[dict[str, Any]]) -> pa.Table:
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
            ("side", pa.string()),
            ("level", pa.int32()),
            ("price", pa.float64()),
            ("size", pa.float64()),
        ]
    )
    return pa.Table.from_pydict(column_data, schema=schema)


# Suppress noqa for the unused conn helper if ever needed later.
_RESERVED_FOR_FUTURE_DUCKDB_HELPER: Final = duckdb
