"""ACLED deletions reconciliation (T-064, REQ-ACLED-DELETED-001..002).

Per the v0.12.0 amendment, ACLED publishes retracted event IDs through
its ``/api/deleted/read`` endpoint. Without this reconciliation, the
local store drifts incorrect over time as ACLED removes events from
its authoritative dataset.

This module is invoked by the ACLED ingest cycle *before* the events
fetch (REQ-ACLED-DELETED-002). It:

1. Reads the per-source watermark
   ``sources.last_acled_deleted_reconciliation_ts`` (a synthetic field
   we keep in the operational schema; for now we use a small dedicated
   table ``acled_deleted_state`` since adding columns to ``sources``
   would require a schema migration).
2. Fetches all deletions with ``deleted_timestamp >= watermark``.
3. For each ``event_id_cnty``, sets ``superseded_at`` on the matching
   active ``event_stream`` rows with a deletion-reason annotation.
4. Advances the watermark to the maximum ``deleted_timestamp`` seen.

The reconciliation is idempotent: running it twice with the same
upstream state is a no-op.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import duckdb
import httpx

from razor_rooster.data_ingest.connectors.acled import (
    _ACLED_BASE_URL,
    _ACLED_MAX_RETRIES,
    _ACLED_PAGE_SIZE,
    _ACLED_TIMEOUT_SECONDS,
    AcledConnector,
)
from razor_rooster.data_ingest.connectors.base import (
    ConnectorError,
    RateLimitedError,
    exponential_backoff_with_jitter,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore

logger = logging.getLogger(__name__)


_ACLED_DELETED_PATH: Final[str] = "/api/deleted/read"
_ACLED_DELETED_STATE_DDL: Final[str] = """\
CREATE TABLE IF NOT EXISTS acled_deleted_state (
    source_id              VARCHAR     PRIMARY KEY,
    last_reconciliation_ts TIMESTAMPTZ NOT NULL,
    last_run_at            TIMESTAMPTZ NOT NULL,
    notes                  TEXT        NULL
);
"""

_DELETION_REASON: Final[str] = "acled_deleted_endpoint"


@dataclass(slots=True)
class DeletionReconciliationReport:
    """Per-run summary of the deletions reconciliation."""

    source_id: str
    started_at: datetime
    completed_at: datetime | None = None
    deleted_ids_seen: int = 0
    rows_superseded: int = 0
    new_watermark: datetime | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


def ensure_acled_deleted_state_table(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the bookkeeping table if missing.

    We use a side-table rather than adding a column to ``sources`` so
    this can ship without a schema migration. If that becomes awkward
    later, a follow-up migration can fold it into ``sources``.
    """
    conn.execute(_ACLED_DELETED_STATE_DDL)


def get_last_reconciliation_ts(
    conn: duckdb.DuckDBPyConnection, source_id: str = "acled"
) -> datetime | None:
    ensure_acled_deleted_state_table(conn)
    row = conn.execute(
        "SELECT last_reconciliation_ts FROM acled_deleted_state WHERE source_id = ?",
        [source_id],
    ).fetchone()
    if row is None:
        return None
    value = row[0]
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raise TypeError(f"unexpected last_reconciliation_ts type: {type(value).__name__}")


def upsert_reconciliation_state(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str,
    last_reconciliation_ts: datetime,
    notes: str | None = None,
) -> None:
    ensure_acled_deleted_state_table(conn)
    now = datetime.now(tz=UTC)
    existing = conn.execute(
        "SELECT 1 FROM acled_deleted_state WHERE source_id = ?", [source_id]
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO acled_deleted_state "
            "(source_id, last_reconciliation_ts, last_run_at, notes) VALUES (?, ?, ?, ?)",
            [source_id, last_reconciliation_ts, now, notes],
        )
    else:
        conn.execute(
            "UPDATE acled_deleted_state SET last_reconciliation_ts = ?, "
            "last_run_at = ?, notes = COALESCE(?, notes) WHERE source_id = ?",
            [last_reconciliation_ts, now, notes, source_id],
        )


def _fetch_deleted_pages(
    connector: AcledConnector,
    *,
    deleted_since: datetime,
) -> Iterator[list[dict[str, Any]]]:
    """Yield successive pages of deletion records as parsed lists."""
    creds = connector._require_password_credentials()
    access_token = connector._ensure_access_token(creds)
    deleted_ts_unix = int(deleted_since.timestamp())
    page = 1
    while True:
        params: list[tuple[str, str | int | float | bool | None]] = [
            ("_format", "json"),
            ("limit", str(_ACLED_PAGE_SIZE)),
            ("page", str(page)),
            ("deleted_timestamp", str(deleted_ts_unix)),
            ("deleted_timestamp_where", ">="),
        ]
        body = _authed_get(connector, _ACLED_DELETED_PATH, params, access_token)
        data = body.get("data", []) if isinstance(body, dict) else []
        if not isinstance(data, list):
            return
        if not data:
            return
        yield data
        if len(data) < _ACLED_PAGE_SIZE:
            return
        page += 1


def _authed_get(
    connector: AcledConnector,
    path: str,
    params: list[tuple[str, str | int | float | bool | None]],
    access_token: str,
) -> dict[str, Any]:
    """Perform an authenticated GET against the connector's HTTP client."""
    url = f"{_ACLED_BASE_URL}{path}"
    last_exc: Exception | None = None
    creds = connector._require_password_credentials()
    for attempt in range(_ACLED_MAX_RETRIES + 1):
        connector._rate_limit()
        try:
            response = connector._client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=_ACLED_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as exc:
            last_exc = exc
            logger.warning(
                "ACLED deletions request error attempt=%d error=%s",
                attempt,
                type(exc).__name__,
            )
        else:
            if response.status_code == 200:
                parsed = response.json()
                if not isinstance(parsed, dict):
                    return {}
                return parsed
            if response.status_code == 401:
                connector._token_cache = connector._password_grant(creds)
                access_token = connector._token_cache.access_token
                continue
            if response.status_code == 429 or response.status_code >= 500:
                logger.warning(
                    "ACLED deletions transient error attempt=%d status=%d",
                    attempt,
                    response.status_code,
                )
            else:
                response.raise_for_status()
            last_exc = httpx.HTTPStatusError(
                f"ACLED deletions returned {response.status_code}",
                request=response.request,
                response=response,
            )
        if attempt < _ACLED_MAX_RETRIES:
            time.sleep(exponential_backoff_with_jitter(attempt, base_seconds=1.0, max_seconds=60.0))
    raise RateLimitedError(
        f"ACLED deletions rate-limited or transient-failed past "
        f"{_ACLED_MAX_RETRIES} retries: {last_exc}"
    )


def _supersede_event_rows(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str,
    event_id_cnty: str,
    deletion_reason: str,
    deletion_ts: datetime,
) -> int:
    """Mark active rows superseded for one ``event_id_cnty``.

    Returns the number of rows updated. The deletion-reason annotation
    is stored in the source payload's ``_razor_deletion_reason`` field;
    the original ``superseded_at`` column gets the deletion timestamp
    so the audit trail aligns with the row's actual retraction time
    rather than the time we noticed it.
    """
    rows = conn.execute(
        "SELECT source_payload_json FROM event_stream "
        "WHERE source_id = ? AND source_record_id = ? AND superseded_at IS NULL",
        [source_id, event_id_cnty],
    ).fetchall()
    if not rows:
        return 0

    updated = 0
    for (payload_text,) in rows:
        try:
            payload = json.loads(payload_text) if isinstance(payload_text, str) else payload_text
        except json.JSONDecodeError:
            payload = {"_razor_unparsed_original": payload_text}
        payload["_razor_deletion_reason"] = deletion_reason
        new_payload = json.dumps(payload, sort_keys=True, default=str)
        conn.execute(
            "UPDATE event_stream SET superseded_at = ?, source_payload_json = ? "
            "WHERE source_id = ? AND source_record_id = ? AND superseded_at IS NULL",
            [deletion_ts, new_payload, source_id, event_id_cnty],
        )
        updated += 1
    return updated


def reconcile_deletions(
    store: DuckDBStore,
    connector: AcledConnector,
    *,
    source_id: str = "acled",
) -> DeletionReconciliationReport:
    """Run the deletions reconciliation pass for ACLED.

    Reads deletions since the recorded watermark, supersedes affected
    rows in ``event_stream``, and advances the watermark.

    The connector parameter must already be configured with valid
    credentials; the Terms gate will be enforced on the connector's
    next call (the Terms gate is an event-fetch concern, not a
    deletion-fetch concern, but we still require an authenticated
    connector).
    """
    started_at = datetime.now(tz=UTC)
    report = DeletionReconciliationReport(source_id=source_id, started_at=started_at)

    with store.connection() as conn:
        ensure_acled_deleted_state_table(conn)
        last_ts = get_last_reconciliation_ts(conn, source_id)
    deleted_since = last_ts or datetime(1970, 1, 1, tzinfo=UTC)

    new_watermark: datetime | None = None
    try:
        for page in _fetch_deleted_pages(connector, deleted_since=deleted_since):
            with store.connection() as conn:
                for entry in page:
                    if not isinstance(entry, dict):
                        continue
                    event_id = entry.get("event_id_cnty")
                    deleted_ts_raw = entry.get("deleted_timestamp")
                    if not event_id or deleted_ts_raw is None:
                        continue
                    try:
                        deletion_ts = datetime.fromtimestamp(int(deleted_ts_raw), tz=UTC)
                    except (TypeError, ValueError, OSError):
                        continue
                    report.deleted_ids_seen += 1
                    superseded = _supersede_event_rows(
                        conn,
                        source_id=source_id,
                        event_id_cnty=str(event_id),
                        deletion_reason=_DELETION_REASON,
                        deletion_ts=deletion_ts,
                    )
                    report.rows_superseded += superseded
                    if new_watermark is None or deletion_ts > new_watermark:
                        new_watermark = deletion_ts
    except (ConnectorError, RateLimitedError, httpx.HTTPError) as exc:
        report.errors.append({"type": type(exc).__name__, "message": str(exc)})
        report.completed_at = datetime.now(tz=UTC)
        raise

    if new_watermark is not None:
        with store.connection() as conn:
            upsert_reconciliation_state(
                conn,
                source_id=source_id,
                last_reconciliation_ts=new_watermark,
                notes=f"reconciled {report.deleted_ids_seen} ids; superseded "
                f"{report.rows_superseded} rows",
            )
            report.new_watermark = new_watermark
    report.completed_at = datetime.now(tz=UTC)
    return report
