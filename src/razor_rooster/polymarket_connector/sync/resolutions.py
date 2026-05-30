"""Resolution backfill + daily delta (T-PMC-042, T-PMC-043; design §3.4).

Two entry points:

- :func:`backfill_resolutions` — one-time historical pull. Walks Gamma's
  ``/markets?closed=true&active=false`` pagination from offset 0,
  upserting every resolved market into both ``polymarket_resolutions``
  and (as a status update) ``polymarket_markets``. Resumable across
  interruptions via the shared ``backfill_state`` table from
  data_ingest's T-034.

- :func:`sync_recent_resolutions` — daily delta. Pulls the most recent
  page(s) of resolved markets and walks until it sees a market that's
  already known locally with the same resolution_ts; further pages are
  skipped on the assumption Gamma orders by recency.

Concurrency: each invocation owns its DuckDB connection for the
duration of the merge it commits. Backfill commits per page to keep
the resume token tight.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
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
from razor_rooster.polymarket_connector.persistence.source import (
    POLYMARKET_RESOLUTIONS_SOURCE_ID,
)
from razor_rooster.polymarket_connector.sync.markets import CONNECTOR_VERSION

logger = logging.getLogger(__name__)


_DEFAULT_PAGE_SIZE: Final[int] = 100

# Gamma's ordering for closed/resolved markets isn't documented as
# strictly recency-first; v1 paginates conservatively from offset 0
# and relies on the upserts to be idempotent.
_BACKFILL_STATE_SOURCE_ID: Final[str] = POLYMARKET_RESOLUTIONS_SOURCE_ID


@dataclass(slots=True)
class ResolutionBackfillReport:
    """Outcome of a resolution backfill pass."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    pages_fetched: int = 0
    resolutions_inserted: int = 0
    resolutions_updated: int = 0
    resolutions_unchanged: int = 0
    next_offset: int | None = None
    status: str = "in_progress"  # 'completed' | 'in_progress' | 'failed'
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ResolutionDeltaReport:
    """Outcome of a daily resolution delta sync."""

    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    pages_fetched: int = 0
    resolutions_inserted: int = 0
    resolutions_updated: int = 0
    resolutions_unchanged: int = 0
    short_circuit_offset: int | None = None
    errors: list[str] = field(default_factory=list)


def backfill_resolutions(
    store: DuckDBStore,
    *,
    client: GammaClient,
    page_size: int = _DEFAULT_PAGE_SIZE,
    max_pages: int | None = None,
    restart: bool = False,
    now: datetime | None = None,
) -> ResolutionBackfillReport:
    """Run a resumable resolution backfill.

    The backfill walks Gamma's ``/markets?active=false&closed=true``
    pagination starting at the persisted resume offset (or 0 if
    ``restart`` is True). Each page is committed to
    ``polymarket_resolutions`` via the staging-merge before the resume
    offset is advanced — so a crash mid-page leaves a clean partial
    state and the next run picks up at the same offset.

    ``max_pages`` is an optional cap for testing / partial runs; in
    production omit it and let the backfill run to exhaustion.
    """
    started = now or datetime.now(tz=UTC)
    report = ResolutionBackfillReport(started_at=started, status="in_progress")

    with store.connection() as conn:
        prior_offset = 0 if restart else _read_backfill_offset(conn)
        if restart:
            _delete_backfill_state(conn)

    offset = prior_offset
    pages_fetched = 0

    try:
        while True:
            page = client.list_resolved(limit=page_size, offset=offset)
            pages_fetched += 1
            if not page:
                report.status = "completed"
                report.next_offset = offset
                break
            with store.connection() as conn:
                inserted, updated, unchanged = _persist_resolutions(conn, page, fetch_ts=started)
                _mark_markets_resolved(conn, page, fetch_ts=started)
                offset += len(page)
                _upsert_backfill_offset(
                    conn,
                    offset=offset,
                    pages_fetched_total=pages_fetched + _read_backfill_pages(conn),
                    started_at=started,
                    status="in_progress",
                )
                update_last_successful_fetch(conn, POLYMARKET_RESOLUTIONS_SOURCE_ID, when=started)
            report.resolutions_inserted += inserted
            report.resolutions_updated += updated
            report.resolutions_unchanged += unchanged
            if len(page) < page_size:
                report.status = "completed"
                report.next_offset = offset
                break
            if max_pages is not None and pages_fetched >= max_pages:
                report.status = "in_progress"
                report.next_offset = offset
                break
    except Exception as exc:
        logger.exception("Polymarket resolution backfill failed")
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.status = "failed"
        with store.connection() as conn:
            _upsert_backfill_offset(
                conn,
                offset=offset,
                pages_fetched_total=pages_fetched + _read_backfill_pages(conn),
                started_at=started,
                status="failed",
                notes=f"{type(exc).__name__}: {exc}",
            )

    if report.status == "completed":
        with store.connection() as conn:
            _upsert_backfill_offset(
                conn,
                offset=offset,
                pages_fetched_total=pages_fetched + _read_backfill_pages(conn),
                started_at=started,
                status="completed",
            )

    report.pages_fetched = pages_fetched
    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "Polymarket resolution backfill: status=%s pages=%d inserted=%d "
        "updated=%d unchanged=%d next_offset=%s",
        report.status,
        report.pages_fetched,
        report.resolutions_inserted,
        report.resolutions_updated,
        report.resolutions_unchanged,
        report.next_offset,
    )
    return report


def sync_recent_resolutions(
    store: DuckDBStore,
    *,
    client: GammaClient,
    page_size: int = _DEFAULT_PAGE_SIZE,
    max_pages: int = 10,
    now: datetime | None = None,
) -> ResolutionDeltaReport:
    """Daily resolution delta.

    Walks the first ``max_pages`` pages (most recent resolutions first
    by Gamma's default ordering) and short-circuits as soon as a page
    contains no new or changed records. ``max_pages`` caps the walk so
    a daily run never accidentally re-walks the whole history.
    """
    started = now or datetime.now(tz=UTC)
    report = ResolutionDeltaReport(started_at=started)

    offset = 0
    try:
        for _ in range(max_pages):
            page = client.list_resolved(limit=page_size, offset=offset)
            report.pages_fetched += 1
            if not page:
                break
            with store.connection() as conn:
                inserted, updated, unchanged = _persist_resolutions(conn, page, fetch_ts=started)
                _mark_markets_resolved(conn, page, fetch_ts=started)
                update_last_successful_fetch(conn, POLYMARKET_RESOLUTIONS_SOURCE_ID, when=started)
            report.resolutions_inserted += inserted
            report.resolutions_updated += updated
            report.resolutions_unchanged += unchanged

            # Short-circuit: a full unchanged page means we've reached
            # the boundary of previously-seen resolutions.
            if inserted == 0 and updated == 0 and unchanged > 0:
                report.short_circuit_offset = offset
                break
            if len(page) < page_size:
                break
            offset += len(page)
    except Exception as exc:
        logger.exception("Polymarket resolution delta failed")
        report.errors.append(f"{type(exc).__name__}: {exc}")

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    return report


# -- internals --------------------------------------------------------------


def _persist_resolutions(
    conn: duckdb.DuckDBPyConnection,
    page: Iterable[GammaMarket],
    *,
    fetch_ts: datetime,
) -> tuple[int, int, int]:
    """Stage and merge a page of resolved markets.

    Returns ``(inserted, updated, unchanged)``.
    """
    arrow_batch = _resolutions_to_arrow(page, fetch_ts=fetch_ts)
    if arrow_batch.num_rows == 0:
        return 0, 0, 0
    result = staging_merge(
        conn,
        target_table="polymarket_resolutions",
        batch=arrow_batch,
        dedup_keys=("source_id", "source_record_id"),
    )
    return result.inserted, result.revised, result.unchanged


def _mark_markets_resolved(
    conn: duckdb.DuckDBPyConnection,
    page: Iterable[GammaMarket],
    *,
    fetch_ts: datetime,
) -> None:
    """Set ``resolved=true, closed=true`` on the active row of each market.

    No-op for markets the local store doesn't have yet (they will be
    picked up by the next markets sync). The query is keyed on the
    active-row predicate (``superseded_at IS NULL``) to avoid touching
    historical / superseded rows.
    """
    for market in page:
        if not market.condition_id:
            continue
        conn.execute(
            "UPDATE polymarket_markets SET resolved = TRUE, closed = TRUE "
            "WHERE condition_id = ? AND superseded_at IS NULL",
            [market.condition_id],
        )


def _resolutions_to_arrow(
    page: Iterable[GammaMarket],
    *,
    fetch_ts: datetime,
) -> pa.Table:
    """Build the Arrow batch for staging-merge into polymarket_resolutions."""
    column_data: dict[str, list[Any]] = {
        "source_id": [],
        "source_record_id": [],
        "source_publication_ts": [],
        "fetch_ts": [],
        "connector_version": [],
        "source_payload_json": [],
        "superseded_at": [],
        "condition_id": [],
        "winning_outcome_token_id": [],
        "winning_outcome_label": [],
        "resolution_ts": [],
        "resolution_source": [],
        "resolution_metadata": [],
        "final_yes_price": [],
        "final_no_price": [],
        "total_volume_at_resolution": [],
        "invalidated": [],
    }

    for market in page:
        if not market.condition_id:
            continue
        winning_token, winning_label = _extract_winning_outcome(market)
        resolution_ts = (
            _coerce_timestamp(
                market.raw.get("resolvedAt")
                or market.raw.get("resolved_at")
                or market.raw.get("endDate")
            )
            or fetch_ts
        )
        column_data["source_id"].append(POLYMARKET_RESOLUTIONS_SOURCE_ID)
        column_data["source_record_id"].append(market.condition_id)
        column_data["source_publication_ts"].append(resolution_ts)
        column_data["fetch_ts"].append(fetch_ts)
        column_data["connector_version"].append(CONNECTOR_VERSION)
        column_data["source_payload_json"].append(json.dumps(market.raw, default=str))
        column_data["superseded_at"].append(None)
        column_data["condition_id"].append(market.condition_id)
        column_data["winning_outcome_token_id"].append(winning_token)
        column_data["winning_outcome_label"].append(winning_label)
        column_data["resolution_ts"].append(resolution_ts)
        column_data["resolution_source"].append(
            str(market.raw.get("resolutionSource") or market.raw.get("resolution_source") or "uma")
        )
        column_data["resolution_metadata"].append(
            json.dumps(market.raw.get("umaResolutionStatuses") or {})
        )
        outcome_prices = market.raw.get("outcomePrices") or []
        final_yes, final_no = _split_outcome_prices(outcome_prices)
        column_data["final_yes_price"].append(final_yes)
        column_data["final_no_price"].append(final_no)
        column_data["total_volume_at_resolution"].append(
            _coerce_float(market.raw.get("volume") or market.raw.get("volume_lifetime"))
        )
        column_data["invalidated"].append(bool(market.raw.get("invalidated", False)))

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
            ("winning_outcome_token_id", pa.string()),
            ("winning_outcome_label", pa.string()),
            ("resolution_ts", pa.timestamp("us", tz="UTC")),
            ("resolution_source", pa.string()),
            ("resolution_metadata", pa.string()),
            ("final_yes_price", pa.float64()),
            ("final_no_price", pa.float64()),
            ("total_volume_at_resolution", pa.float64()),
            ("invalidated", pa.bool_()),
        ]
    )
    return pa.Table.from_pydict(column_data, schema=schema)


def _extract_winning_outcome(market: GammaMarket) -> tuple[str | None, str | None]:
    """Best-effort: identify the winning outcome from the resolved-market payload.

    Polymarket exposes ``outcomePrices`` as a 2-element list; the
    winning outcome corresponds to the index whose price is 1 (or
    closest to 1). Multi-outcome resolution is out of scope per
    OQ-PMC-004 — the function returns ``(None, None)`` for any market
    where the winning index isn't unambiguous.
    """
    outcomes = market.raw.get("outcomes") or []
    prices = market.raw.get("outcomePrices") or []
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            prices = []
    token_ids = market.raw.get("clobTokenIds") or []
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except json.JSONDecodeError:
            token_ids = []
    if not (
        isinstance(outcomes, list) and isinstance(prices, list) and isinstance(token_ids, list)
    ):
        return None, None
    if len(outcomes) != 2 or len(prices) != 2 or len(token_ids) != 2:
        return None, None

    parsed_prices = [_coerce_float(p) for p in prices]
    if any(p is None for p in parsed_prices):
        return None, None

    if parsed_prices[0] == parsed_prices[1]:
        return None, None

    winning_index = 0 if (parsed_prices[0] or 0) > (parsed_prices[1] or 0) else 1
    return str(token_ids[winning_index]), str(outcomes[winning_index])


def _split_outcome_prices(prices: object) -> tuple[float | None, float | None]:
    """Return (final_yes_price, final_no_price) or (None, None) when not parseable."""
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(prices, list) or len(prices) != 2:
        return None, None
    yes = _coerce_float(prices[0])
    no = _coerce_float(prices[1])
    return yes, no


def _coerce_timestamp(value: object) -> datetime | None:
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


# -- backfill_state helpers (delegate to data_ingest's contract) ----------


def _read_backfill_offset(conn: duckdb.DuckDBPyConnection) -> int:
    row = conn.execute(
        "SELECT last_resume_token FROM backfill_state WHERE source_id = ?",
        [_BACKFILL_STATE_SOURCE_ID],
    ).fetchone()
    if row is None or row[0] is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _read_backfill_pages(conn: duckdb.DuckDBPyConnection) -> int:
    row = conn.execute(
        "SELECT records_persisted FROM backfill_state WHERE source_id = ?",
        [_BACKFILL_STATE_SOURCE_ID],
    ).fetchone()
    if row is None or row[0] is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _delete_backfill_state(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        "DELETE FROM backfill_state WHERE source_id = ?",
        [_BACKFILL_STATE_SOURCE_ID],
    )


def _upsert_backfill_offset(
    conn: duckdb.DuckDBPyConnection,
    *,
    offset: int,
    pages_fetched_total: int,
    started_at: datetime,
    status: str,
    notes: str | None = None,
) -> None:
    """Upsert the backfill_state row for the resolutions source.

    The data_ingest backfill harness uses string resume tokens; we
    encode the integer offset as a string so we share the schema
    without changing it.
    """
    existing = conn.execute(
        "SELECT 1 FROM backfill_state WHERE source_id = ?",
        [_BACKFILL_STATE_SOURCE_ID],
    ).fetchone()
    last_updated = datetime.now(tz=UTC)
    if existing is None:
        conn.execute(
            "INSERT INTO backfill_state (source_id, started_at, last_resume_token, "
            "records_persisted, bytes_persisted, status, last_updated_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                _BACKFILL_STATE_SOURCE_ID,
                started_at,
                str(offset),
                pages_fetched_total,
                0,
                status,
                last_updated,
                notes,
            ],
        )
    else:
        conn.execute(
            "UPDATE backfill_state SET last_resume_token = ?, records_persisted = ?, "
            "status = ?, last_updated_at = ?, notes = ? WHERE source_id = ?",
            [
                str(offset),
                pages_fetched_total,
                status,
                last_updated,
                notes,
                _BACKFILL_STATE_SOURCE_ID,
            ],
        )


# A sentinel callable type useful for downstream wiring (T-PMC-061).
ResolutionsBackfillFn = Callable[[DuckDBStore, GammaClient], ResolutionBackfillReport]
