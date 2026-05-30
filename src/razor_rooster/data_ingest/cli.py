"""``razor-rooster ingest`` CLI.

Operator-facing commands wired to the data_ingest subsystem.

Available subcommands:

- ``razor-rooster ingest status`` — print per-source freshness from the
  ``freshness`` view (T-015).
- ``razor-rooster ingest cycle [--source ID] [--db PATH]`` — run one
  incremental cycle and write its structured report (T-033 + T-040).
- ``razor-rooster ingest backfill --source ID [--restart] [--db PATH]`` —
  run a single source's backfill with resume support (T-034 + T-035).
- ``razor-rooster ingest init [--db PATH]`` — apply migrations to a fresh
  DuckDB store (T-013).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import click

from razor_rooster.data_ingest.config.loader import load_ingest_schedule
from razor_rooster.data_ingest.cycle_report import run_and_report
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.provenance import query_freshness

logger = logging.getLogger(__name__)


_DEFAULT_DB_PATH_ENV = "RAZOR_ROOSTER_DB"
_DEFAULT_DB_PATH = Path("data") / "trough.duckdb"
_DEFAULT_SCHEDULE = Path("config") / "ingest_schedule.yaml"


def _resolve_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env_path = os.environ.get(_DEFAULT_DB_PATH_ENV)
    if env_path:
        return Path(env_path)
    return _DEFAULT_DB_PATH


def _open_store_with_migrations(db_path: Path) -> DuckDBStore:
    store = DuckDBStore(db_path)
    with store.connection() as conn:
        run_pending_migrations(conn)
    return store


def _import_all_connectors() -> None:
    """Trigger every connector's ``@register`` decorator at CLI start."""
    from razor_rooster.data_ingest.connectors import (
        acled,  # noqa: F401
        eia,  # noqa: F401
        federal_register,  # noqa: F401
        fred,  # noqa: F401
        gdelt_events,  # noqa: F401
        noaa,  # noqa: F401
        nrc_adams,  # noqa: F401
        regulations_gov,  # noqa: F401
        usgs_minerals,  # noqa: F401
        who_don,  # noqa: F401
        worldbank,  # noqa: F401
    )


@click.group()
def ingest() -> None:
    """Multi-source public data ingestion (The Trough)."""


@ingest.command(name="status")
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path. Default: data/trough.duckdb (or $RAZOR_ROOSTER_DB).",
)
def status(db_path_opt: str | None) -> None:
    """Print per-source freshness from the ``freshness`` view."""
    db_path = _resolve_db_path(db_path_opt)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}; run `razor-rooster ingest init` first.")
        raise click.exceptions.Exit(code=1)

    store = _open_store_with_migrations(db_path)
    try:
        with store.connection() as conn:
            rows = query_freshness(conn)
    finally:
        store.close()

    if not rows:
        click.echo("No sources registered yet. Run `razor-rooster ingest cycle` to bootstrap.")
        return

    click.echo(f"{'source_id':<24} {'last_success':<24} {'stale?':<8} {'threshold (s)':>14}")
    click.echo("-" * 72)
    for row in rows:
        last = row.last_successful_fetch.isoformat() if row.last_successful_fetch else "(never)"
        click.echo(
            f"{row.source_id:<24} "
            f"{last:<24} "
            f"{'STALE' if row.is_stale else 'fresh':<8} "
            f"{row.freshness_threshold_seconds:>14}"
        )


@ingest.command(name="init")
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path. Default: data/trough.duckdb.",
)
def init(db_path_opt: str | None) -> None:
    """Apply schema migrations to a fresh or existing DuckDB store."""
    db_path = _resolve_db_path(db_path_opt)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = _open_store_with_migrations(db_path)
    store.close()
    click.echo(f"Initialized DuckDB store at {db_path}.")


@ingest.command(name="cycle")
@click.option(
    "--source",
    "source_filter",
    type=str,
    default=None,
    help="Run only this source. Comma-separated for multiple: --source fred,worldbank.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path.",
)
@click.option(
    "--schedule",
    "schedule_path",
    type=click.Path(exists=True),
    default=str(_DEFAULT_SCHEDULE),
    help=f"Schedule YAML path. Default: {_DEFAULT_SCHEDULE}.",
)
@click.option(
    "--quiet/--verbose",
    default=False,
    help="Suppress stdout summary; the JSONL log is still written.",
)
def cycle(
    source_filter: str | None,
    db_path_opt: str | None,
    schedule_path: str,
    quiet: bool,
) -> None:
    """Run one incremental cycle and write a structured report."""
    db_path = _resolve_db_path(db_path_opt)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}; run `razor-rooster ingest init` first.")
        raise click.exceptions.Exit(code=1)

    _import_all_connectors()

    schedule = load_ingest_schedule(Path(schedule_path))
    only = tuple(s.strip() for s in source_filter.split(",")) if source_filter else None

    store = _open_store_with_migrations(db_path)
    try:
        cycle_id = f"cycle-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        report, log_file = run_and_report(
            store,
            schedule=schedule,
            cycle_id=cycle_id,
            only=only,
            print_summary=not quiet,
        )
    finally:
        store.close()

    if not quiet:
        click.echo(f"\nReport written to {log_file}.")

    failed = [o for o in report.outcomes if o.status == "failed"]
    if failed:
        click.echo(f"{len(failed)} connector(s) failed; see {log_file} for detail.", err=True)
        raise click.exceptions.Exit(code=2)


@ingest.command(name="backfill")
@click.option("--source", "source_id", type=str, required=True)
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
@click.option("--restart", is_flag=True, default=False)
@click.option("--batch-size", type=int, default=10_000, show_default=True)
def backfill(
    source_id: str,
    db_path_opt: str | None,
    restart: bool,
    batch_size: int,
) -> None:
    """Run a single source's backfill with resume support and cap enforcement."""
    db_path = _resolve_db_path(db_path_opt)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}; run `razor-rooster ingest init` first.")
        raise click.exceptions.Exit(code=1)

    _import_all_connectors()

    from razor_rooster.data_ingest.backfill import (
        BackfillNotSupportedError,
        run_backfill,
    )
    from razor_rooster.data_ingest.cap_enforcement import build_cap_check
    from razor_rooster.data_ingest.config.loader import load_source_caps
    from razor_rooster.data_ingest.credentials import load_credentials_for
    from razor_rooster.data_ingest.registry import get as registry_get

    store = _open_store_with_migrations(db_path)
    try:
        try:
            connector_class = registry_get(source_id)
        except Exception as exc:
            click.echo(f"No connector registered for source_id {source_id!r}: {exc}", err=True)
            raise click.exceptions.Exit(code=1) from exc

        credentials = load_credentials_for(source_id)
        connector = connector_class(store, credentials=credentials)

        caps = load_source_caps(Path("config") / "source_caps.yaml")
        cap_check = build_cap_check(store, caps=caps, file_path=db_path)

        try:
            report = run_backfill(
                connector,
                restart=restart,
                batch_size=batch_size,
                cap_check=cap_check,
            )
        except BackfillNotSupportedError as exc:
            click.echo(f"backfill not supported for {source_id}: {exc}", err=True)
            raise click.exceptions.Exit(code=2) from exc

    finally:
        store.close()

    click.echo(f"Backfill {report.status} for {source_id}: persisted {report.records_persisted}.")
    if report.status == "failed":
        raise click.exceptions.Exit(code=2)
