"""Cycle report writer (T-040).

Wires the structured ``cycle_logger`` from T-021 into the scheduler's
:class:`CycleReport` from T-033. Each cycle:

1. Opens a JSONL log file under ``logs/cycles/cycle-<iso8601>-<id>.jsonl``.
2. Writes the per-cycle summary line on completion (success or failure).
3. Inserts a ``cycle_log`` row in DuckDB pointing at the JSONL path.
4. Optionally prints a short stdout summary so operators see the result
   in their terminal.

This module is the seam between scheduler outputs (typed CycleReport with
ConnectorOutcome list) and persisted artifacts (JSONL on disk + cycle_log
table row).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from razor_rooster.data_ingest.logging.structured import (
    ConnectorOutcome as LogConnectorOutcome,
)
from razor_rooster.data_ingest.logging.structured import (
    CycleSummary,
    cycle_logger,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.provenance import query_freshness
from razor_rooster.data_ingest.scheduler import CycleReport

logger = logging.getLogger(__name__)


def write_cycle_report(
    store: DuckDBStore,
    report: CycleReport,
    *,
    log_dir: Path | str | None = None,
    print_summary: bool = True,
    print_fn: Callable[[str], None] = print,
) -> Path:
    """Persist a completed cycle report.

    Returns the path to the JSONL log file.

    The function is idempotent on re-call only by accident: each call
    appends a fresh row to ``cycle_log``. The scheduler is the only
    legitimate caller and calls this exactly once per cycle.
    """
    log_dir_path = Path(log_dir) if log_dir is not None else Path("logs") / "cycles"
    log_dir_path.mkdir(parents=True, exist_ok=True)

    started_at = report.started_at
    log_file = log_dir_path / (
        f"cycle-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{report.cycle_id[:8]}.jsonl"
    )

    # Build the structured summary line.
    summary = CycleSummary(
        cycle_id=report.cycle_id,
        started_at=started_at.isoformat(),
        ended_at=(report.completed_at.isoformat() if report.completed_at else None),
        duration_seconds=report.duration_seconds,
        connectors=[
            LogConnectorOutcome(
                source_id=o.source_id,
                status=o.status,
                records_ingested=o.records_ingested,
                records_skipped_duplicate=o.records_skipped_duplicate,
                duration_seconds=o.duration_seconds,
                errors=list(o.errors),
            )
            for o in report.outcomes
        ],
        stale_sources=list(_stale_source_ids(store)),
        anomalies_detected=[],
    )

    # Append skipped sources as anomalies-flavored entries so they're visible
    # in the cycle log without inflating the connectors list.
    for source_id, reason in report.skipped:
        summary.anomalies_detected.append(
            {"type": "skipped", "source_id": source_id, "reason": reason}
        )
    for err in report.errors:
        summary.anomalies_detected.append({"type": "scheduler_error", **err})

    # Use cycle_logger so the JSON formatter + redaction filter wrap the write.
    with cycle_logger(
        cycle_id=report.cycle_id,
        log_dir=log_dir_path,
        logger_name="razor_rooster.data_ingest.cycles",
    ) as cycle_summary:
        # The cycle_logger context manager will write a summary on exit; we
        # transfer our pre-built summary into it so its formatter writes the
        # canonical record. The dataclass is mutable; set its fields.
        cycle_summary.connectors = summary.connectors
        cycle_summary.stale_sources = summary.stale_sources
        cycle_summary.anomalies_detected = summary.anomalies_detected

    # The cycle_logger uses its own filename pattern; reconcile by also
    # writing our intended path explicitly.
    log_file.write_text(json.dumps(asdict(summary), default=str) + "\n", encoding="utf-8")

    # Insert the cycle_log row pointing at the file.
    with store.connection() as conn:
        conn.execute(
            """
            INSERT INTO cycle_log (cycle_id, started_at, completed_at, log_path, summary_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                report.cycle_id,
                report.started_at,
                report.completed_at,
                str(log_file),
                json.dumps(asdict(summary), default=str),
            ],
        )

    if print_summary:
        _print_summary(summary, print_fn=print_fn)

    return log_file


def _stale_source_ids(store: DuckDBStore) -> Iterable[str]:
    """Return the source_ids currently flagged stale by the freshness view."""
    with store.connection() as conn:
        rows = query_freshness(conn)
    return [r.source_id for r in rows if r.is_stale]


def _print_summary(summary: CycleSummary, *, print_fn: Callable[[str], None]) -> None:
    """Emit a short, human-readable summary to stdout."""
    print_fn("=" * 70)
    print_fn(f"Razor-Rooster cycle {summary.cycle_id}")
    print_fn(f"  started:   {summary.started_at}")
    print_fn(f"  ended:     {summary.ended_at or 'in_progress'}")
    if summary.duration_seconds is not None:
        print_fn(f"  duration:  {summary.duration_seconds:.1f}s")
    print_fn("-" * 70)

    if not summary.connectors:
        print_fn("  no connectors ran this cycle.")
    else:
        print_fn(f"  connectors: {len(summary.connectors)}")
        for outcome in summary.connectors:
            line = (
                f"    {outcome.source_id:<24} "
                f"{outcome.status:<8} "
                f"records={outcome.records_ingested:>6} "
                f"duration={outcome.duration_seconds:.2f}s"
            )
            print_fn(line)

    if summary.stale_sources:
        print_fn(f"  stale sources: {', '.join(summary.stale_sources)}")

    if summary.anomalies_detected:
        print_fn(f"  anomalies: {len(summary.anomalies_detected)}")
        for anomaly in summary.anomalies_detected[:5]:
            line = f"    - {anomaly.get('type', '?')}: {json.dumps(anomaly, default=str)[:100]}"
            print_fn(line)

    print_fn("=" * 70)


def run_and_report(
    store: DuckDBStore,
    *,
    schedule: object,
    cycle_id: str,
    only: Iterable[str] | None = None,
    log_dir: Path | str | None = None,
    print_summary: bool = True,
    now: datetime | None = None,
) -> tuple[CycleReport, Path]:
    """Convenience: run a cycle and write its report in one call.

    Returns ``(report, log_file_path)``.
    """
    from razor_rooster.data_ingest.config.loader import IngestScheduleConfig
    from razor_rooster.data_ingest.scheduler import run_cycle

    if not isinstance(schedule, IngestScheduleConfig):
        raise TypeError(f"schedule must be an IngestScheduleConfig, got {type(schedule).__name__}")

    started = now or datetime.now(tz=UTC)
    report = run_cycle(store, schedule, cycle_id=cycle_id, only=only, now=started)
    log_file = write_cycle_report(store, report, log_dir=log_dir, print_summary=print_summary)
    return report, log_file
