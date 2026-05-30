"""Cycle scheduler — composes the connector framework into the daily cycle (T-033).

Reads the schedule config (T-022), evaluates which connectors are due via
``sources.last_successful_fetch`` and the ``freshness`` view, runs them with
bounded concurrency, and assembles a :class:`CycleReport`. Failures in one
connector are isolated from others (REQ-SRC-004).

Persistence wiring:
- The scheduler does not normalize or persist records itself. Each connector
  yields :class:`RawRecord` instances from ``fetch_incremental``; the
  scheduler's per-connector ``persister`` callable normalizes via
  ``connector.normalize`` and writes via the staging-merge pattern.
- Each batch acquires its own DuckDB connection from the store's pool so
  the bounded thread pool doesn't deadlock against the connection pool.

Concurrency:
- ``run_cycle`` uses a :class:`concurrent.futures.ThreadPoolExecutor` with
  ``max_workers`` from the schedule config's ``defaults.max_workers``
  (default 4). Each worker handles one connector at a time.
- Within a single connector run, batches are processed serially to keep the
  staging-merge pattern straightforward.

The per-connector persistence path also updates ``sources.last_successful_fetch``
(via :func:`provenance.update_last_successful_fetch`) on success or
``last_failed_fetch`` on failure, so the freshness view reflects current
state at all times.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa

from razor_rooster.data_ingest.config.loader import IngestScheduleConfig, SourceSchedule
from razor_rooster.data_ingest.connectors.base import (
    Connector,
    ConnectorOutcome,
    run_incremental,
)
from razor_rooster.data_ingest.credentials import load_credentials_for
from razor_rooster.data_ingest.normalization.base import NormalizedRecord
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.provenance import (
    update_last_failed_fetch,
    update_last_successful_fetch,
)
from razor_rooster.data_ingest.persistence.staging_merge import staging_merge
from razor_rooster.data_ingest.registry import get as registry_get
from razor_rooster.data_ingest.registry import known_source_ids

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DueDecision:
    """Whether a single source is due for an incremental run.

    Attributes:
        source_id: the source under consideration.
        is_due: whether the scheduler will run the source this cycle.
        reason: human-readable explanation when ``is_due`` is False (e.g.
            "no last_successful_fetch yet → first run", "last fetched
            12 minutes ago → not due yet").
        last_successful_fetch: the value pulled from ``sources``; ``None``
            when the source has never been fetched.
    """

    source_id: str
    is_due: bool
    reason: str
    last_successful_fetch: datetime | None


@dataclass(slots=True)
class CycleReport:
    """The result of one cycle run.

    Mirrors the structured cycle log from design §6.1 plus a few
    operational fields the orchestrator tracks. Mutable so the scheduler
    can append outcomes as connectors complete.
    """

    cycle_id: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    outcomes: list[ConnectorOutcome] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


def evaluate_due(
    schedule: IngestScheduleConfig,
    *,
    last_fetch_lookup: dict[str, datetime | None],
    now: datetime | None = None,
    only: Iterable[str] | None = None,
) -> tuple[DueDecision, ...]:
    """Decide which sources are due for an incremental run this cycle.

    A source is due if:

    1. It has never been successfully fetched (``last_successful_fetch is
       None``); first runs always go.
    2. The time since its last successful fetch is at least the cadence
       window (daily = 24h, weekly = 7d, monthly = 30d, annual = 365d).

    The cycle scheduler does not enforce ``time_of_day`` or ``day_of_week``
    — those are advisory hints for the operator's cron / launchd, not
    runtime gates. If the operator wants daily-but-only-after-08:00, that
    discipline lives outside the scheduler.

    The ``only`` filter restricts evaluation to a subset of source ids
    (used by ``--source <id>`` ad-hoc invocations).
    """
    when = now or datetime.now(tz=UTC)
    decisions: list[DueDecision] = []
    only_set = set(only) if only is not None else None

    for source_id, src_schedule in schedule.sources.items():
        if only_set is not None and source_id not in only_set:
            continue
        last = last_fetch_lookup.get(source_id)
        if last is None:
            decisions.append(
                DueDecision(
                    source_id=source_id,
                    is_due=True,
                    reason="no last_successful_fetch (first run)",
                    last_successful_fetch=None,
                )
            )
            continue
        cadence_seconds = _cadence_seconds(src_schedule)
        elapsed = (when - last).total_seconds()
        if elapsed >= cadence_seconds:
            decisions.append(
                DueDecision(
                    source_id=source_id,
                    is_due=True,
                    reason=f"last fetched {int(elapsed)}s ago (cadence {cadence_seconds}s)",
                    last_successful_fetch=last,
                )
            )
        else:
            decisions.append(
                DueDecision(
                    source_id=source_id,
                    is_due=False,
                    reason=f"last fetched {int(elapsed)}s ago "
                    f"(cadence {cadence_seconds}s) — not due yet",
                    last_successful_fetch=last,
                )
            )
    return tuple(decisions)


def _cadence_seconds(src_schedule: SourceSchedule) -> int:
    """Convert a cadence literal to seconds.

    These are nominal windows; ``time_of_day`` / ``day_of_week`` are not
    enforced here.
    """
    cadence = src_schedule.cadence
    if cadence == "daily":
        return 24 * 60 * 60
    if cadence == "weekly":
        return 7 * 24 * 60 * 60
    if cadence == "monthly":
        return 30 * 24 * 60 * 60
    if cadence == "annual":
        return 365 * 24 * 60 * 60
    raise ValueError(f"unknown cadence {cadence!r}")  # pragma: no cover


def fetch_last_successful_lookup(store: DuckDBStore) -> dict[str, datetime | None]:
    """Read the ``sources`` table into a ``{source_id: last_successful_fetch}`` map."""
    with store.connection() as conn:
        rows = conn.execute("SELECT source_id, last_successful_fetch FROM sources").fetchall()
    return {str(r[0]): r[1] for r in rows}


def build_persister(
    connector: Connector,
    store: DuckDBStore,
    *,
    batch_size: int = 10_000,
) -> _BatchedPersister:
    """Construct a per-connector persister bound to the connector's schema.

    The persister consumes :class:`NormalizedRecord` instances and writes
    them to the connector's canonical table via the staging-merge pattern,
    in batches of ``batch_size``. Returns the total number of records
    *ingested* (i.e., successfully passed through the merge — equal to the
    number passed in if the merge classifies them as either inserts or
    revisions; ``unchanged`` rows count as "no-op ingested").

    The persister opens one DuckDB connection per call (acquired from the
    store's pool) and holds it for the entire stream.
    """
    return _BatchedPersister(connector=connector, store=store, batch_size=batch_size)


class _BatchedPersister:
    """Callable bridge from a NormalizedRecord stream to staging-merge writes."""

    def __init__(
        self,
        connector: Connector,
        store: DuckDBStore,
        *,
        batch_size: int,
    ) -> None:
        self._connector = connector
        self._store = store
        self._batch_size = batch_size

    def __call__(self, records: Iterator[NormalizedRecord]) -> int:
        batch: list[NormalizedRecord] = []
        total_ingested = 0
        with self._store.connection() as conn:
            for record in records:
                batch.append(record)
                if len(batch) >= self._batch_size:
                    total_ingested += self._flush(batch, conn)
                    batch = []
            if batch:
                total_ingested += self._flush(batch, conn)
        return total_ingested

    def _flush(
        self,
        batch: list[NormalizedRecord],
        conn: Any,
    ) -> int:
        arrow_batch = self._records_to_arrow(batch)
        result = staging_merge(
            conn,
            self._connector.canonical_schema.value,
            arrow_batch,
        )
        return result.inserted + result.revised + result.unchanged

    def _records_to_arrow(self, batch: list[NormalizedRecord]) -> pa.Table:
        """Build an Arrow table from a list of NormalizedRecord instances.

        Each canonical schema has a fixed column set. We rely on the
        record's dataclass fields aligning with the table's column names;
        the per-canonical-schema mapping is encoded inline.
        """
        from razor_rooster.data_ingest.persistence.schemas import SchemaType

        schema_type = self._connector.canonical_schema
        if schema_type == SchemaType.EVENT_STREAM:
            columns = [
                "source_id",
                "source_record_id",
                "source_publication_ts",
                "fetch_ts",
                "connector_version",
                "superseded_at",
                "source_payload_json",
                "event_ts",
                "country_iso3",
                "actor_primary",
                "actor_secondary",
                "event_class",
                "description",
            ]
        elif schema_type == SchemaType.TIME_SERIES:
            columns = [
                "source_id",
                "source_record_id",
                "source_publication_ts",
                "fetch_ts",
                "connector_version",
                "superseded_at",
                "source_payload_json",
                "series_id",
                "observation_ts",
                "value",
                "unit",
                "frequency",
            ]
        elif schema_type == SchemaType.DOCUMENT_DOCKET:
            columns = [
                "source_id",
                "source_record_id",
                "source_publication_ts",
                "fetch_ts",
                "connector_version",
                "superseded_at",
                "source_payload_json",
                "title",
                "document_type",
                "docket_id",
                "agency",
                "published_date",
                "effective_date",
                "comment_close_date",
                "full_text_uri",
                "full_text_local_path",
            ]
        elif schema_type == SchemaType.GEOSPATIAL_INDICATOR:
            columns = [
                "source_id",
                "source_record_id",
                "source_publication_ts",
                "fetch_ts",
                "connector_version",
                "superseded_at",
                "source_payload_json",
                "indicator_id",
                "observation_ts",
                "country_iso3",
                "region_code",
                "lat",
                "lon",
                "value",
                "unit",
            ]
        else:
            raise ValueError(f"unsupported schema_type {schema_type!r}")

        column_data: dict[str, list[Any]] = {col: [] for col in columns}
        for record in batch:
            for col in columns:
                value = getattr(record, col)
                if col == "source_payload_json":
                    import json

                    if isinstance(value, dict):
                        column_data[col].append(json.dumps(value))
                    else:
                        column_data[col].append(value)
                else:
                    column_data[col].append(value)
        return pa.table(column_data)


def run_cycle(
    store: DuckDBStore,
    schedule: IngestScheduleConfig,
    *,
    cycle_id: str,
    only: Iterable[str] | None = None,
    now: datetime | None = None,
) -> CycleReport:
    """Run one ingest cycle: due-evaluate, dispatch, persist, summarize.

    The scheduler:
    1. Reads ``sources.last_successful_fetch`` for all known sources.
    2. Evaluates which are due (filtering by ``only`` if provided).
    3. Constructs a connector instance for each due source via the
       registry (T-032), passing in any credentials loaded from
       ``.env`` (T-020). Sources not in the registry are skipped with a
       structured note in the report.
    4. Runs each connector via ``run_incremental`` (T-030) on a worker
       thread, with concurrency capped at ``schedule.defaults.max_workers``.
    5. Updates ``sources.last_successful_fetch`` on success or
       ``last_failed_fetch`` on failure.
    6. Returns a :class:`CycleReport` with per-connector outcomes.

    Failures isolate per-connector: one source's exception does not stop
    the cycle. The report's ``errors`` list is reserved for cycle-level
    issues that are not attributable to a specific connector.
    """
    started_at = now or datetime.now(tz=UTC)
    report = CycleReport(cycle_id=cycle_id, started_at=started_at)

    last_fetch_lookup = fetch_last_successful_lookup(store)
    decisions = evaluate_due(
        schedule, last_fetch_lookup=last_fetch_lookup, now=started_at, only=only
    )

    due_decisions = tuple(d for d in decisions if d.is_due)
    not_due_decisions = tuple(d for d in decisions if not d.is_due)

    for nd in not_due_decisions:
        report.skipped.append((nd.source_id, nd.reason))

    # Resolve connector classes from the registry, skipping sources that
    # don't have one (e.g., the schedule configures a source whose
    # connector hasn't been built yet).
    runnable: list[tuple[DueDecision, type[Connector]]] = []
    for d in due_decisions:
        try:
            connector_class = registry_get(d.source_id)
        except Exception:
            report.skipped.append(
                (
                    d.source_id,
                    "no connector registered for this source_id",
                )
            )
            continue
        runnable.append((d, connector_class))

    max_workers = schedule.defaults.max_workers
    batch_size = schedule.defaults.batch_size

    def run_one(decision: DueDecision, connector_class: type[Connector]) -> ConnectorOutcome:
        credentials = load_credentials_for(decision.source_id)
        connector = connector_class(store, credentials=credentials)
        persister = build_persister(connector, store, batch_size=batch_size)
        outcome = run_incremental(
            connector,
            since=decision.last_successful_fetch or datetime(1970, 1, 1, tzinfo=UTC),
            persister=persister,
        )
        # Update sources table to reflect current fetch status. The cycle's
        # ``now`` is used (not the wall clock) so callers that pass an
        # explicit ``now`` for testing or replay get a coherent timeline
        # across the schedule, the cycle, and the freshness view.
        with store.connection() as conn:
            if outcome.status in ("ok", "partial"):
                update_last_successful_fetch(conn, decision.source_id, when=started_at)
            elif outcome.status == "failed":
                summary = outcome.errors[0]["message"] if outcome.errors else "unknown failure"
                update_last_failed_fetch(
                    conn, decision.source_id, error_summary=summary, when=started_at
                )
        return outcome

    if max_workers <= 1 or len(runnable) <= 1:
        # Single-threaded fast path when not worth spawning a pool.
        for decision, connector_class in runnable:
            outcome = run_one(decision, connector_class)
            report.outcomes.append(outcome)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(run_one, decision, cls): decision for decision, cls in runnable
            }
            for fut in as_completed(futures):
                decision = futures[fut]
                try:
                    outcome = fut.result()
                except Exception as exc:
                    logger.exception(
                        "scheduler internal failure for source %s",
                        decision.source_id,
                    )
                    report.errors.append(
                        {
                            "source_id": decision.source_id,
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
                    continue
                report.outcomes.append(outcome)

    completed_at = datetime.now(tz=UTC)
    report.completed_at = completed_at
    report.duration_seconds = (completed_at - started_at).total_seconds()
    return report


def all_known_source_ids() -> tuple[str, ...]:
    """Helper that returns all source_ids the registry knows about."""
    return known_source_ids()
