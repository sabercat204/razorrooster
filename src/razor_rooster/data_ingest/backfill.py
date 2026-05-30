"""Backfill orchestration with interruption-resume (T-034).

Backfill is a separate top-level command from the regular cycle (per design
§3.4). It runs against one connector at a time, pulls historical records up
to a configurable ``until`` timestamp, and persists a resume token to
``backfill_state`` after each batch commit. If the operator interrupts the
run (Ctrl-C, network drop, OS reboot), re-running picks up from the last
committed token rather than restarting from zero (REQ-BACKFILL-002).

Per-source caps and global corpus caps are enforced before each batch
commit (REQ-BACKFILL-003) — that wiring is added by T-035.

Concurrency: backfill is single-connector at a time. Running multiple
backfills in parallel against the same source is undefined behavior and
not exposed via the CLI.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import duckdb

from razor_rooster.data_ingest.connectors.base import (
    Connector,
    RateLimitedError,
    ResumeToken,
)
from razor_rooster.data_ingest.normalization.base import RawRecord
from razor_rooster.data_ingest.scheduler import build_persister

logger = logging.getLogger(__name__)


class BackfillError(RuntimeError):
    """Base class for backfill orchestration failures."""


class BackfillNotSupportedError(BackfillError):
    """Raised when the operator asks to backfill a connector that doesn't support it."""


@dataclass(slots=True)
class BackfillReport:
    """The result of one backfill run.

    Mutable so the orchestrator can track progress as batches commit.
    """

    source_id: str
    started_at: datetime
    completed_at: datetime | None = None
    records_persisted: int = 0
    batches_committed: int = 0
    last_resume_token: str | None = None
    status: str = "in_progress"  # 'in_progress' | 'completed' | 'failed' | 'interrupted'
    errors: list[dict[str, Any]] = field(default_factory=list)


def get_backfill_state(
    conn: duckdb.DuckDBPyConnection, source_id: str
) -> tuple[str | None, str | None]:
    """Return ``(last_resume_token, status)`` for a source.

    Returns ``(None, None)`` when no backfill state row exists yet (i.e.,
    backfill has never been started for this source).
    """
    row = conn.execute(
        "SELECT last_resume_token, status FROM backfill_state WHERE source_id = ?",
        [source_id],
    ).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def upsert_backfill_state(
    conn: duckdb.DuckDBPyConnection,
    *,
    source_id: str,
    started_at: datetime,
    last_resume_token: str | None,
    records_persisted: int,
    bytes_persisted: int = 0,
    status: str,
    notes: str | None = None,
) -> None:
    """Insert or update the backfill state row for a source."""
    now = datetime.now(tz=UTC)
    existing = conn.execute(
        "SELECT 1 FROM backfill_state WHERE source_id = ?", [source_id]
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO backfill_state (
                source_id, started_at, last_resume_token, records_persisted,
                bytes_persisted, status, last_updated_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                source_id,
                started_at,
                last_resume_token,
                records_persisted,
                bytes_persisted,
                status,
                now,
                notes,
            ],
        )
    else:
        conn.execute(
            """
            UPDATE backfill_state SET
                last_resume_token = ?,
                records_persisted = ?,
                bytes_persisted = ?,
                status = ?,
                last_updated_at = ?,
                notes = COALESCE(?, notes)
            WHERE source_id = ?
            """,
            [
                last_resume_token,
                records_persisted,
                bytes_persisted,
                status,
                now,
                notes,
                source_id,
            ],
        )


def run_backfill(
    connector: Connector,
    *,
    until: datetime | None = None,
    restart: bool = False,
    batch_size: int = 10_000,
    cap_check: BackfillCapCheck | None = None,
) -> BackfillReport:
    """Run a connector's backfill, resumable across interruptions.

    Steps:

    1. Validate ``connector.backfill_supported``.
    2. Read prior ``backfill_state`` for the source (unless ``restart``).
    3. Call ``connector.fetch_backfill(until, resume_token)`` which yields
       ``(record, new_resume_token)`` tuples.
    4. Batch records up to ``batch_size``; on each full batch, normalize,
       commit via the staging-merge, persist the new token to
       ``backfill_state``, and bump counters.
    5. On exception, mark status ``failed`` and re-raise after persisting
       the last good token.
    6. On clean completion, mark status ``completed``.

    ``cap_check`` is the optional gate from T-035 that pauses backfill when
    a per-source or global cap would be exceeded; ``None`` means no
    enforcement.
    """
    if not connector.backfill_supported:
        raise BackfillNotSupportedError(
            f"connector {connector.source_id!r} does not support backfill"
        )

    started_at = datetime.now(tz=UTC)
    until = until or started_at
    report = BackfillReport(source_id=connector.source_id, started_at=started_at)

    persister = build_persister(connector, connector.store, batch_size=batch_size)

    # Pull prior state.
    resume_token: ResumeToken | None = None
    with connector.store.connection() as conn:
        prior_token, prior_status = get_backfill_state(conn, connector.source_id)
        if not restart and prior_token is not None:
            resume_token = ResumeToken(value=prior_token)
            logger.info(
                "resuming backfill source_id=%s from token=%s (prior status=%s)",
                connector.source_id,
                prior_token,
                prior_status,
                extra={"source_id": connector.source_id},
            )
        # Mark as in_progress at start so concurrent readers see fresh status.
        upsert_backfill_state(
            conn,
            source_id=connector.source_id,
            started_at=started_at,
            last_resume_token=prior_token if not restart else None,
            records_persisted=0,
            status="in_progress",
        )

    # Drive the backfill.
    try:
        batches_iter = _batched_records(
            connector.fetch_backfill(until=until, resume_token=resume_token),
            batch_size=batch_size,
        )
        for batch_records, batch_token in batches_iter:
            if cap_check is not None:
                pause = cap_check(connector.source_id)
                if pause is not None:
                    logger.warning(
                        "backfill paused for source_id=%s reason=%s",
                        connector.source_id,
                        pause.reason,
                        extra={"source_id": connector.source_id},
                    )
                    with connector.store.connection() as conn:
                        upsert_backfill_state(
                            conn,
                            source_id=connector.source_id,
                            started_at=started_at,
                            last_resume_token=report.last_resume_token,
                            records_persisted=report.records_persisted,
                            status=pause.status,
                            notes=pause.reason,
                        )
                    report.status = pause.status
                    report.errors.append({"type": "backfill_paused", "reason": pause.reason})
                    return report

            normalized_iter = (connector.normalize(raw) for raw in batch_records)
            ingested = persister(normalized_iter)
            report.records_persisted += ingested
            report.batches_committed += 1
            if batch_token is not None:
                report.last_resume_token = batch_token.value
            with connector.store.connection() as conn:
                upsert_backfill_state(
                    conn,
                    source_id=connector.source_id,
                    started_at=started_at,
                    last_resume_token=report.last_resume_token,
                    records_persisted=report.records_persisted,
                    status="in_progress",
                )

        report.status = "completed"
        report.completed_at = datetime.now(tz=UTC)
        with connector.store.connection() as conn:
            upsert_backfill_state(
                conn,
                source_id=connector.source_id,
                started_at=started_at,
                last_resume_token=report.last_resume_token,
                records_persisted=report.records_persisted,
                status="completed",
            )
    except RateLimitedError as exc:
        logger.exception(
            "backfill rate-limited past retry budget source_id=%s",
            connector.source_id,
            extra={"source_id": connector.source_id},
        )
        report.status = "failed"
        report.errors.append({"type": "rate_limit_exhausted", "message": str(exc)})
        report.completed_at = datetime.now(tz=UTC)
        with connector.store.connection() as conn:
            upsert_backfill_state(
                conn,
                source_id=connector.source_id,
                started_at=started_at,
                last_resume_token=report.last_resume_token,
                records_persisted=report.records_persisted,
                status="failed",
                notes=f"rate_limit_exhausted: {exc}",
            )
        raise
    except Exception as exc:
        logger.exception(
            "backfill failed source_id=%s",
            connector.source_id,
            extra={"source_id": connector.source_id},
        )
        report.status = "failed"
        report.errors.append({"type": type(exc).__name__, "message": str(exc)})
        report.completed_at = datetime.now(tz=UTC)
        with connector.store.connection() as conn:
            upsert_backfill_state(
                conn,
                source_id=connector.source_id,
                started_at=started_at,
                last_resume_token=report.last_resume_token,
                records_persisted=report.records_persisted,
                status="failed",
                notes=f"{type(exc).__name__}: {exc}",
            )
        raise

    return report


@dataclass(frozen=True, slots=True)
class CapCheckResult:
    """Returned by a cap check when backfill should pause."""

    status: str  # 'CAP_REACHED' | 'GLOBAL_CAP_REACHED'
    reason: str


#: Type alias for the cap-check callable.
#:
#: Returns ``None`` when the source is free to keep going, or a
#: :class:`CapCheckResult` with the pause reason.
BackfillCapCheck = Callable[[str], "CapCheckResult | None"]


def _batched_records(
    iterator: Iterator[tuple[RawRecord, ResumeToken | None]],
    *,
    batch_size: int,
) -> Iterator[tuple[list[RawRecord], ResumeToken | None]]:
    """Group an iterator of ``(record, token)`` tuples into batches.

    The yielded batch carries the token from the *last* record in that
    batch — that's the resumption point if execution stops here.
    """
    batch: list[RawRecord] = []
    last_token: ResumeToken | None = None
    for record, token in iterator:
        batch.append(record)
        if token is not None:
            last_token = token
        if len(batch) >= batch_size:
            yield batch, last_token
            batch = []
            # last_token persists across batches so progress isn't lost when
            # the connector emits None tokens between batches.
    if batch:
        yield batch, last_token
