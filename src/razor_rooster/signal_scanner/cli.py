"""``razor-rooster scan`` CLI (T-SCAN-001 / T-SCAN-040; design §3.8).

Operator-facing commands for the signal scanner. Each command opens
a DuckDB store, applies pending migrations across all three
namespaces (data_ingest, polymarket_connector, pattern_library,
signal_scanner), and runs the relevant operation.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import click

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.signal_scanner.engines.scanner import (
    DEFAULT_MAX_WORKERS,
    LibraryVersionChangeError,
    StrictDriftAbort,
    run_scan,
)
from razor_rooster.signal_scanner.engines.trace import render_trace_text
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)
from razor_rooster.signal_scanner.persistence.operations import (
    PruneConfirmationError,
    prune_before,
    query_recent_candidates,
    query_scan_records,
    query_scan_summary,
    query_trace,
)

logger = logging.getLogger(__name__)


_DEFAULT_DB_PATH_ENV = "RAZOR_ROOSTER_DB"
_DEFAULT_DB_PATH = Path("data") / "trough.duckdb"


def _resolve_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env_path = os.environ.get(_DEFAULT_DB_PATH_ENV)
    if env_path:
        return Path(env_path)
    return _DEFAULT_DB_PATH


def _open_store(db_path: Path, *, require_exists: bool = True) -> DuckDBStore:
    if require_exists and not db_path.exists():
        click.echo(
            f"DuckDB store not found at {db_path}; run `razor-rooster ingest init` first.",
            err=True,
        )
        raise click.exceptions.Exit(code=1)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = DuckDBStore(db_path)
    with store.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
    return store


@click.group(name="scan")
def scan() -> None:
    """The Nose — live evaluation against historical patterns."""


@scan.command(name="version")
def version() -> None:
    """Print the signal_scanner subsystem schema version namespace."""
    click.echo("signal_scanner schema namespace: 3001+")


@scan.command(name="run")
@click.option(
    "--class",
    "only_class_id",
    type=str,
    default=None,
    help="Scan only this class. Other classes are skipped.",
)
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Abort on definition-version drift (otherwise drift is flagged but scan continues).",
)
@click.option(
    "--max-workers",
    type=int,
    default=DEFAULT_MAX_WORKERS,
    help=f"Bound on per-class parallelism. Default: {DEFAULT_MAX_WORKERS}.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path. Default: data/trough.duckdb (or $RAZOR_ROOSTER_DB).",
)
def run_command(
    only_class_id: str | None,
    strict: bool,
    max_workers: int,
    db_path_opt: str | None,
) -> None:
    """Run one full library-wide (or single-class) scan."""
    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        report = run_scan(
            store,
            only_class_id=only_class_id,
            strict=strict,
            max_workers=max_workers,
        )
    except StrictDriftAbort as exc:
        click.echo(f"strict-drift abort: {exc}", err=True)
        raise click.exceptions.Exit(code=2) from exc
    except LibraryVersionChangeError as exc:
        click.echo(f"library version changed mid-scan: {exc}", err=True)
        raise click.exceptions.Exit(code=2) from exc
    finally:
        store.close()

    click.echo(f"scan_id:           {report.scan_id}")
    click.echo(f"library_version:   {report.pattern_library_version}")
    click.echo(f"classes processed: {len(report.classes)}")
    click.echo(
        f"  succeeded:       {report.succeeded}    "
        f"failed: {report.failed}    "
        f"candidates: {report.candidates}"
    )
    if report.duration_seconds is not None:
        click.echo(f"duration:          {report.duration_seconds:.2f}s")
    for record in report.classes:
        marker = "*" if record.is_candidate else " "
        warnings_summary: list[str] = []
        if record.source_stale_warning:
            warnings_summary.append("source_stale")
        if record.library_stale_warning:
            warnings_summary.append("library_stale")
        if record.definition_drift_warning:
            warnings_summary.append("drift")
        if record.no_update_applied:
            warnings_summary.append("no_update")
        if record.error:
            warnings_summary.append(f"error={record.error}")
        warn_str = f" warnings={','.join(warnings_summary)}" if warnings_summary else ""
        click.echo(
            f"  {marker} {record.class_id:<40} "
            f"prior={record.base_rate:.4f} -> posterior={record.posterior:.4f}  "
            f"shift={record.log_odds_shift:+.3f}"
            f"{warn_str}"
        )
    if report.errors:
        click.echo("scan-level errors:", err=True)
        for err in report.errors:
            click.echo(f"  - {err}", err=True)
        raise click.exceptions.Exit(code=2)
    if report.failed:
        raise click.exceptions.Exit(code=2)


@scan.command(name="show")
@click.argument("scan_id")
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
)
def show(scan_id: str, db_path_opt: str | None) -> None:
    """Print one scan's summary plus per-class records."""
    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            summary = query_scan_summary(conn, scan_id=scan_id)
            records = query_scan_records(conn, scan_id=scan_id)
    finally:
        store.close()
    if summary is None:
        click.echo(f"scan_id {scan_id!r} not found", err=True)
        raise click.exceptions.Exit(code=1)
    click.echo(f"scan_id:               {summary.scan_id}")
    click.echo(f"started_at:            {summary.scan_started_at.isoformat()}")
    if summary.scan_completed_at is not None:
        click.echo(f"completed_at:          {summary.scan_completed_at.isoformat()}")
    click.echo(f"library_version:       {summary.pattern_library_version}")
    click.echo(
        f"classes:               {summary.classes_total} total, "
        f"{summary.classes_succeeded} succeeded, "
        f"{summary.classes_failed} failed, "
        f"{summary.classes_skipped} skipped"
    )
    click.echo(f"candidates:            {summary.candidates_count}")
    if summary.library_stale_warning:
        click.echo("library_stale:         YES")
    click.echo("---")
    for record in records:
        marker = "*" if record.is_candidate else " "
        click.echo(
            f"  {marker} {record.class_id:<40} "
            f"posterior={record.posterior:.4f}  "
            f"shift={record.log_odds_shift:+.3f}  "
            f"candidate={record.is_candidate}"
        )


@scan.command(name="show-trace")
@click.argument("scan_id")
@click.argument("class_id")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Print the raw trace JSON instead of the rendered text form.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
)
def show_trace(scan_id: str, class_id: str, as_json: bool, db_path_opt: str | None) -> None:
    """Print the reasoning trace for one (scan, class) pair."""
    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            trace = query_trace(conn, scan_id=scan_id, class_id=class_id)
    finally:
        store.close()
    if trace is None:
        click.echo(
            f"trace for scan_id={scan_id!r} class_id={class_id!r} not found",
            err=True,
        )
        raise click.exceptions.Exit(code=1)
    if as_json:
        click.echo(json.dumps(dict(trace.payload), indent=2))
    else:
        click.echo(render_trace_text(trace.payload))


@scan.command(name="list-candidates")
@click.option(
    "--since",
    "since_iso",
    type=str,
    default=None,
    help="ISO-8601 timestamp; only return candidates from scans started after.",
)
@click.option(
    "--sector",
    type=str,
    default=None,
    help="Filter by class domain sector.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
)
def list_candidates(since_iso: str | None, sector: str | None, db_path_opt: str | None) -> None:
    """List recent candidate situations."""
    since: datetime | None = None
    if since_iso:
        since = datetime.fromisoformat(since_iso)
    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            records = query_recent_candidates(conn, since=since, sector=sector)
    finally:
        store.close()
    if not records:
        click.echo("(no candidate situations matching filter)")
        return
    click.echo(f"{'scan_id':<38} {'class_id':<35} {'shift':>10}  direction")
    click.echo("-" * 100)
    for record in records:
        click.echo(
            f"{record.scan_id:<38} "
            f"{record.class_id:<35} "
            f"{record.log_odds_shift:>+10.3f}  "
            f"{record.candidate_direction or '?'}"
        )


@scan.command(name="prune")
@click.option(
    "--before",
    "before_iso",
    type=str,
    required=True,
    help="ISO-8601 timestamp; delete scans started before this point.",
)
@click.option(
    "--confirm",
    is_flag=True,
    default=False,
    help="Required to actually delete; without it the command refuses.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
)
def prune(before_iso: str, confirm: bool, db_path_opt: str | None) -> None:
    """Delete scan records older than the given timestamp.

    Refuses without --confirm. Pruning is recommended only after
    operator review of disk usage; per REQ-SCAN-PERSIST-002 the
    default is unbounded retention.
    """
    if not confirm:
        click.echo("refusing to prune without --confirm", err=True)
        raise click.exceptions.Exit(code=2)
    cutoff = datetime.fromisoformat(before_iso)
    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            try:
                deleted = prune_before(conn, before=cutoff, confirm=True)
            except PruneConfirmationError as exc:
                click.echo(str(exc), err=True)
                raise click.exceptions.Exit(code=2) from exc
    finally:
        store.close()
    click.echo(f"pruned {deleted} scan(s) older than {cutoff.isoformat()}")
