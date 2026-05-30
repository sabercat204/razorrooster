"""Markets daily sync (T-PMC-040; design §3.4).

Reconciles ``polymarket_markets`` with Polymarket's current Gamma state:

1. Fetch every active market from ``GET /markets?active=true&closed=false``.
2. Fetch every closed-but-not-yet-resolved market the same way; we need
   them to detect resolution transitions on the next cycle.
3. Diff against the local table:
   - Market in response, absent locally → insert.
   - Market in response with changed metadata → upsert (stages new row,
     supersedes prior active row via the staging-merge contract).
   - Market in local table but absent from response → set
     ``removed_at = now()``. Rows are never deleted; the trace is
     preserved per design rule.
4. Update ``sources.last_successful_fetch`` for the ``polymarket`` source.

Sector mapping (T-PMC-050) consumes the inserted/updated set as a side
effect — wired in T-PMC-051.

Concurrency: this sync writes only to ``polymarket_markets`` and the
``sources`` table. It holds a single DuckDB connection from the pool for
the duration of its merge; concurrent reads from other subsystems remain
safe (DuckDB serializes around its WAL).
"""

from __future__ import annotations

import json
import logging
import uuid
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
from razor_rooster.polymarket_connector.client.gamma import (
    GammaClient,
    GammaMarket,
)
from razor_rooster.polymarket_connector.config.loader import (
    SectorKeywordsConfig,
)
from razor_rooster.polymarket_connector.mapping.sector_heuristic import (
    map_sector,
)
from razor_rooster.polymarket_connector.mapping.sector_overrides import (
    upsert_inferred_mapping,
)
from razor_rooster.polymarket_connector.persistence.source import (
    POLYMARKET_LIVE_SOURCE_ID,
)

logger = logging.getLogger(__name__)


# Connector version recorded on every persisted row. Bump this constant
# (and add a connector-version row in the per-source state) when the
# parsing logic changes in a way downstream consumers need to detect.
CONNECTOR_VERSION: Final[str] = "polymarket@0.1.0"

# Markets with > 2 outcomes are skipped from active price-snapshot
# processing per OQ-PMC-004; they are still recorded here so future
# multi-outcome support has the metadata. The constant is exported so
# the price-snapshot sync (T-PMC-041) can use the same definition.
BINARY_MARKET_TYPE: Final[str] = "binary"


@dataclass(slots=True)
class MarketSyncReport:
    """Outcome of one ``sync_markets`` invocation.

    ``inserted`` / ``updated`` / ``unchanged`` mirror the staging-merge
    bucket counts. ``removed`` is the count of markets that disappeared
    between cycles. ``skipped_non_binary`` records markets that were
    persisted but flagged for skip in price-snapshot processing.
    ``mappings_upserted`` / ``mappings_skipped_manual`` track the
    sector-heuristic side effect (T-PMC-051). When ``keywords`` is not
    supplied, both are 0.
    """

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    markets_total_seen: int = 0
    markets_inserted: int = 0
    markets_updated: int = 0
    markets_unchanged: int = 0
    markets_removed: int = 0
    skipped_non_binary: int = 0
    mappings_upserted: int = 0
    mappings_skipped_manual: int = 0
    errors: list[str] = field(default_factory=list)


def sync_markets(
    store: DuckDBStore,
    *,
    client: GammaClient,
    page_size: int = 100,
    now: datetime | None = None,
    sector_keywords: SectorKeywordsConfig | None = None,
) -> MarketSyncReport:
    """Run one Markets daily sync. See module docstring for details.

    When ``sector_keywords`` is provided, every upstream market also gets
    a heuristic sector mapping written to ``polymarket_sector_mapping``
    via :func:`upsert_inferred_mapping`. Existing manual overrides are
    preserved (T-PMC-051).

    Returns:
        :class:`MarketSyncReport` populated with per-bucket counts.
    """
    started = now or datetime.now(tz=UTC)
    report = MarketSyncReport(started_at=started)

    try:
        # Pull both active and closed-not-resolved markets so the diff
        # detects status transitions reliably. Active markets dominate
        # volume so they come first.
        active_markets = list(client.iter_markets(active=True, closed=False, page_size=page_size))
        closed_markets = list(client.iter_markets(active=False, closed=True, page_size=page_size))
    except Exception as exc:
        logger.exception("Polymarket markets fetch failed")
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.completed_at = datetime.now(tz=UTC)
        report.duration_seconds = (report.completed_at - started).total_seconds()
        return report

    upstream_markets = list(active_markets) + list(closed_markets)
    report.markets_total_seen = len(upstream_markets)

    # Some Polymarket payloads carry duplicates across the active+closed
    # union (rare but observed in operations). Dedup by condition_id and
    # keep the first occurrence (active wins over closed).
    seen_ids: set[str] = set()
    deduped: list[GammaMarket] = []
    for market in upstream_markets:
        if not market.condition_id or market.condition_id in seen_ids:
            continue
        seen_ids.add(market.condition_id)
        deduped.append(market)

    # Count non-binary markets so the price snapshot sync can warn on a
    # mismatch later. The market type is derived heuristically from the
    # outcomes array length.
    for market in deduped:
        if not _is_binary(market):
            report.skipped_non_binary += 1

    with store.connection() as conn:
        existing_ids = _existing_active_market_ids(conn)
        upstream_ids = {m.condition_id for m in deduped}
        removed_ids = existing_ids - upstream_ids

        # Stage and merge upstream markets.
        if deduped:
            arrow_batch = _markets_to_arrow(deduped, fetch_ts=started)
            merge_result = staging_merge(
                conn,
                target_table="polymarket_markets",
                batch=arrow_batch,
            )
            report.markets_inserted = merge_result.inserted
            report.markets_updated = merge_result.revised
            report.markets_unchanged = merge_result.unchanged

        # Mark missing markets as removed (REQ-PMC-MARKET-003 — never delete).
        if removed_ids:
            removed_at_ts = started
            for condition_id in sorted(removed_ids):
                conn.execute(
                    "UPDATE polymarket_markets "
                    "SET removed_at = ? "
                    "WHERE condition_id = ? AND removed_at IS NULL",
                    [removed_at_ts, condition_id],
                )
            report.markets_removed = len(removed_ids)

        # Record the successful fetch on the polymarket source row.
        update_last_successful_fetch(conn, POLYMARKET_LIVE_SOURCE_ID, when=started)

        # Sector heuristic side effect (T-PMC-051). When the operator
        # has supplied a keyword catalogue, every upstream market gets a
        # mapping refresh; existing manual overrides are preserved.
        if sector_keywords is not None:
            for market in deduped:
                mapping = map_sector(market, keywords=sector_keywords)
                if upsert_inferred_mapping(
                    conn,
                    condition_id=market.condition_id,
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
        "Polymarket markets sync done: seen=%d inserted=%d updated=%d "
        "unchanged=%d removed=%d skipped_non_binary=%d",
        report.markets_total_seen,
        report.markets_inserted,
        report.markets_updated,
        report.markets_unchanged,
        report.markets_removed,
        report.skipped_non_binary,
    )
    return report


# -- internals --------------------------------------------------------------


def _existing_active_market_ids(conn: duckdb.DuckDBPyConnection) -> set[str]:
    """Return condition_ids currently active in polymarket_markets.

    "Active" here means the row has not been superseded *and* has no
    removed_at marker. This is the set we diff against the upstream
    response to identify removals.
    """
    rows = conn.execute(
        "SELECT DISTINCT condition_id FROM polymarket_markets "
        "WHERE superseded_at IS NULL AND removed_at IS NULL"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _is_binary(market: GammaMarket) -> bool:
    """Heuristic: a market is binary iff it exposes exactly two outcomes."""
    outcomes = market.raw.get("outcomes")
    if isinstance(outcomes, list):
        return len(outcomes) == 2
    # Fall back to clobTokenIds which is the canonical token-id list.
    token_ids = market.raw.get("clobTokenIds")
    if isinstance(token_ids, list):
        return len(token_ids) == 2
    if isinstance(token_ids, str):
        # Some responses pack the JSON-stringified array.
        try:
            decoded = json.loads(token_ids)
        except json.JSONDecodeError:
            return False
        return isinstance(decoded, list) and len(decoded) == 2
    return False


def _market_type(market: GammaMarket) -> str:
    """Tag the market type per OQ-PMC-004 ('binary' | 'multi' | 'negrisk')."""
    if market.raw.get("negRisk") is True or market.raw.get("neg_risk") is True:
        return "negrisk"
    if _is_binary(market):
        return BINARY_MARKET_TYPE
    return "multi"


def _coerce_timestamp(value: object) -> datetime | None:
    """Best-effort ISO-8601 / epoch-seconds → datetime conversion."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _coerce_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _outcome_tokens_for(market: GammaMarket) -> list[dict[str, str]]:
    """Build the outcome_tokens JSON array used by the schema."""
    outcomes = market.raw.get("outcomes")
    token_ids = market.raw.get("clobTokenIds")
    # token_ids can arrive as JSON-stringified array.
    if isinstance(token_ids, str):
        try:
            decoded = json.loads(token_ids)
            if isinstance(decoded, list):
                token_ids = decoded
        except json.JSONDecodeError:
            token_ids = None
    if not isinstance(outcomes, list) or not isinstance(token_ids, list):
        return []
    pairs: list[dict[str, str]] = []
    for outcome_label, token_id in zip(outcomes, token_ids, strict=False):
        pairs.append(
            {
                "outcome_label": str(outcome_label),
                "token_id": str(token_id),
            }
        )
    return pairs


def _markets_to_arrow(
    markets: Iterable[GammaMarket],
    *,
    fetch_ts: datetime,
) -> pa.Table:
    """Build the Arrow batch the staging-merge expects.

    Column order matches ``polymarket_markets`` DDL exactly so the
    staging-merge insert path stays straightforward. The provenance
    prefix (source_id, source_record_id, etc.) is filled per-row.
    """
    column_data: dict[str, list[Any]] = {
        "source_id": [],
        "source_record_id": [],
        "source_publication_ts": [],
        "fetch_ts": [],
        "connector_version": [],
        "source_payload_json": [],
        "superseded_at": [],
        "condition_id": [],
        "slug": [],
        "question": [],
        "description": [],
        "category": [],
        "subcategory": [],
        "tags": [],
        "event_id": [],
        "market_type": [],
        "outcome_tokens": [],
        "end_date": [],
        "active": [],
        "closed": [],
        "resolved": [],
        "volume_lifetime": [],
        "created_at_polymarket": [],
        "last_updated_polymarket": [],
        "removed_at": [],
    }

    for market in markets:
        condition_id = market.condition_id
        # Polymarket's last_update / created_at fields. Names vary slightly
        # across endpoints; we accept either.
        created_at = _coerce_timestamp(market.raw.get("createdAt") or market.raw.get("created_at"))
        last_updated = _coerce_timestamp(
            market.raw.get("updatedAt") or market.raw.get("updated_at")
        )
        publication_ts = last_updated or created_at or fetch_ts
        end_date = _coerce_timestamp(market.raw.get("endDate") or market.raw.get("end_date"))
        column_data["source_id"].append(POLYMARKET_LIVE_SOURCE_ID)
        column_data["source_record_id"].append(condition_id)
        column_data["source_publication_ts"].append(publication_ts)
        column_data["fetch_ts"].append(fetch_ts)
        column_data["connector_version"].append(CONNECTOR_VERSION)
        column_data["source_payload_json"].append(json.dumps(market.raw, default=str))
        column_data["superseded_at"].append(None)
        column_data["condition_id"].append(condition_id)
        column_data["slug"].append(market.slug)
        column_data["question"].append(market.question)
        column_data["description"].append(
            str(market.raw.get("description")) if market.raw.get("description") else None
        )
        column_data["category"].append(
            str(market.raw.get("category")) if market.raw.get("category") else None
        )
        column_data["subcategory"].append(
            str(market.raw.get("subcategory")) if market.raw.get("subcategory") else None
        )
        tags_value = market.raw.get("tags")
        column_data["tags"].append(json.dumps(tags_value) if tags_value is not None else None)
        column_data["event_id"].append(
            str(market.raw.get("eventId") or market.raw.get("event_id") or "") or None
        )
        column_data["market_type"].append(_market_type(market))
        column_data["outcome_tokens"].append(json.dumps(_outcome_tokens_for(market)))
        column_data["end_date"].append(end_date)
        column_data["active"].append(market.active)
        column_data["closed"].append(market.closed)
        column_data["resolved"].append(bool(market.raw.get("resolved", False)))
        column_data["volume_lifetime"].append(
            _coerce_float(market.raw.get("volume") or market.raw.get("volume_lifetime"))
        )
        column_data["created_at_polymarket"].append(created_at)
        column_data["last_updated_polymarket"].append(last_updated)
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
            ("condition_id", pa.string()),
            ("slug", pa.string()),
            ("question", pa.string()),
            ("description", pa.string()),
            ("category", pa.string()),
            ("subcategory", pa.string()),
            ("tags", pa.string()),
            ("event_id", pa.string()),
            ("market_type", pa.string()),
            ("outcome_tokens", pa.string()),
            ("end_date", pa.timestamp("us", tz="UTC")),
            ("active", pa.bool_()),
            ("closed", pa.bool_()),
            ("resolved", pa.bool_()),
            ("volume_lifetime", pa.float64()),
            ("created_at_polymarket", pa.timestamp("us", tz="UTC")),
            ("last_updated_polymarket", pa.timestamp("us", tz="UTC")),
            ("removed_at", pa.timestamp("us", tz="UTC")),
        ]
    )
    return pa.Table.from_pydict(column_data, schema=schema)


# Suppress the unused-uuid import warning by exposing an internal helper.
def _generate_sync_id() -> str:  # pragma: no cover - reserved for future use
    return uuid.uuid4().hex
