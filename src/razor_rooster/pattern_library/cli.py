"""``razor-rooster pattern-library`` CLI (T-PL-001 / T-PL-031).

Operator commands for the pattern library. Each subcommand reads from
or writes to the shared DuckDB store; a missing store path is the most
common error and yields exit code 1 with an actionable message.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import click

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.pattern_library import registry
from razor_rooster.pattern_library.models.event_class import Sector
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.pattern_library.registry import (
    ClassValidationError,
    sync_to_store,
)
from razor_rooster.pattern_library.version import current_version

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
    return store


@click.group(name="pattern-library")
def pattern_library() -> None:
    """The Bone Pile — historical event-pattern catalogue."""


@pattern_library.command(name="version")
def version() -> None:
    """Print the live LIBRARY_VERSION integer."""
    click.echo(str(current_version()))


@pattern_library.command(name="list")
@click.option(
    "--sector",
    "sector_value",
    type=click.Choice([s.value for s in Sector]),
    default=None,
    help="Filter by domain_sector.",
)
def list_classes(sector_value: str | None) -> None:
    """List registered event classes."""
    sector_filter = Sector(sector_value) if sector_value else None
    classes = registry.get_all(sector=sector_filter)
    if not classes:
        click.echo("(no event classes registered)")
        return
    click.echo(f"{'class_id':<40} {'sector':<22} {'def':>4}  title")
    click.echo("-" * 90)
    for cls in classes:
        click.echo(
            f"{cls.class_id:<40} "
            f"{cls.domain_sector.value:<22} "
            f"{cls.definition_version:>4}  "
            f"{cls.title}"
        )


@pattern_library.command(name="show")
@click.argument("class_id")
def show(class_id: str) -> None:
    """Print one class's metadata, precursors, and analogue features."""
    try:
        cls = registry.get(class_id)
    except KeyError as exc:
        click.echo(f"unknown class_id {class_id!r}", err=True)
        raise click.exceptions.Exit(code=1) from exc
    click.echo(f"class_id:           {cls.class_id}")
    click.echo(f"title:              {cls.title}")
    click.echo(f"description:        {cls.description}")
    click.echo(f"domain_sector:      {cls.domain_sector.value}")
    if cls.secondary_sectors:
        click.echo("secondary_sectors:  " + ", ".join(s.value for s in cls.secondary_sectors))
    click.echo(f"definition_version: {cls.definition_version}")
    click.echo(f"outcome_type:       {cls.outcome_type}")
    click.echo(f"baseline_strategy:  {cls.baseline_strategy.value}")
    click.echo(f"refractory_months:  {cls.refractory_months}")
    click.echo(f"prior_alpha:        {cls.prior_alpha}")
    click.echo(f"prior_beta:         {cls.prior_beta}")
    if cls.precursors:
        click.echo("precursors:")
        for p in cls.precursors:
            click.echo(
                f"  - {p.variable_id} ({p.direction}, "
                f"lead={int(p.lead_time_window.total_seconds() / 86400)}d, "
                f"threshold={p.threshold_method.value})"
            )
    if cls.analogue_features:
        click.echo("analogue_features:")
        for f in cls.analogue_features:
            click.echo(f"  - {f.feature_id} (norm={f.normalization.value}, weight={f.weight})")


@pattern_library.command(name="validate")
@click.argument("class_id")
def validate(class_id: str) -> None:
    """Run registration-time validation for one class without persisting."""
    try:
        cls = registry.get(class_id)
    except KeyError as exc:
        click.echo(f"unknown class_id {class_id!r}", err=True)
        raise click.exceptions.Exit(code=1) from exc
    try:
        registry.register(cls)  # idempotent revalidation
    except ClassValidationError as exc:
        click.echo(f"validation failed for {class_id!r}: {exc}", err=True)
        raise click.exceptions.Exit(code=2) from exc
    click.echo(f"OK: {class_id} validates cleanly")


@pattern_library.command(name="sync-classes")
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path. Default: data/trough.duckdb (or $RAZOR_ROOSTER_DB).",
)
def sync_classes(db_path_opt: str | None) -> None:
    """Reconcile registered classes against ``pl_event_classes``.

    Inserts new classes, updates definition_version on changed classes,
    and stamps removed_at on classes no longer present. Returns a
    summary of the diff.
    """
    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            delta = sync_to_store(conn)
    finally:
        store.close()
    click.echo(f"added:               {len(delta.added)}")
    if delta.added:
        for cid in delta.added:
            click.echo(f"  + {cid}")
    click.echo(f"definition_changed:  {len(delta.definition_changed)}")
    if delta.definition_changed:
        for cid in delta.definition_changed:
            click.echo(f"  ~ {cid}")
    click.echo(f"removed:             {len(delta.removed)}")
    if delta.removed:
        for cid in delta.removed:
            click.echo(f"  - {cid}")
    click.echo(f"unchanged:           {len(delta.unchanged)}")


@pattern_library.command(name="refresh")
@click.option(
    "--class",
    "only_class_id",
    type=str,
    default=None,
    help="Refresh only this class. Other classes are left untouched.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Run even when the registry diff shows no changes; bumps "
    "library_version with bump_reason='code_change'.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path. Default: data/trough.duckdb (or $RAZOR_ROOSTER_DB).",
)
@click.option(
    "--max-workers",
    type=int,
    default=None,
    help="Bound on per-class parallelism. v1 default is 2.",
)
def refresh(
    only_class_id: str | None,
    force: bool,
    db_path_opt: str | None,
    max_workers: int | None,
) -> None:
    """Run a full pattern-library refresh (or one targeted class)."""
    from razor_rooster.pattern_library.engines.refresh import (
        DEFAULT_MAX_WORKERS,
        run_refresh,
    )

    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        report = run_refresh(
            store,
            only_class_id=only_class_id,
            force=force,
            max_workers=max_workers or DEFAULT_MAX_WORKERS,
        )
    finally:
        store.close()

    click.echo(f"refresh_id:        {report.refresh_id}")
    click.echo(f"library_version:   {report.library_version}")
    if report.bump_reason:
        click.echo(f"bump_reason:       {report.bump_reason}")
    if report.class_delta is not None:
        delta = report.class_delta
        click.echo(
            f"registry diff:     +{len(delta.added)} ~{len(delta.definition_changed)} "
            f"-{len(delta.removed)} ={len(delta.unchanged)}"
        )
    click.echo(f"classes processed: {len(report.classes)}")
    for outcome in report.classes:
        warnings_str = f" warnings={','.join(outcome.warnings)}" if outcome.warnings else ""
        errors_str = f" errors={'|'.join(outcome.errors)}" if outcome.errors else ""
        click.echo(
            f"  - {outcome.class_id:<40} {outcome.status:<8} "
            f"occ={outcome.occurrences_persisted:>4} "
            f"sigs={outcome.signatures_computed:>2} "
            f"analogue_pts={outcome.analogue_points_persisted:>4} "
            f"cal={outcome.calibration_status or 'skipped'}"
            f"{warnings_str}{errors_str}"
        )
    if report.errors:
        click.echo("refresh-level errors:", err=True)
        for err in report.errors:
            click.echo(f"  - {err}", err=True)
        raise click.exceptions.Exit(code=2)
    failed = [o for o in report.classes if o.status == "failed"]
    if failed:
        click.echo(f"{len(failed)} class(es) failed", err=True)
        raise click.exceptions.Exit(code=2)


@pattern_library.command(name="eval")
@click.argument("class_id")
@click.option(
    "--window-start",
    "window_start_iso",
    type=str,
    default=None,
    help="ISO-8601 UTC start of the base-rate window.",
)
@click.option(
    "--window-end",
    "window_end_iso",
    type=str,
    default=None,
    help="ISO-8601 UTC end of the base-rate window.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path.",
)
def eval_class(
    class_id: str,
    window_start_iso: str | None,
    window_end_iso: str | None,
    db_path_opt: str | None,
) -> None:
    """Run an ad-hoc base-rate evaluation for one class without persisting.

    Skips signature, analogue, and calibration stages — those write
    side-effects to the store. ``eval`` is the read-only triage tool;
    ``refresh`` is the persisting workflow.
    """
    from razor_rooster.pattern_library.engines.base_rates import compute_base_rate

    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        cls = registry.get(class_id)
        window: tuple[datetime, datetime] | None = None
        if window_start_iso and window_end_iso:
            window = (
                datetime.fromisoformat(window_start_iso),
                datetime.fromisoformat(window_end_iso),
            )
        elif window_start_iso or window_end_iso:
            click.echo(
                "--window-start and --window-end must be provided together",
                err=True,
            )
            raise click.exceptions.Exit(code=2)

        with store.connection() as conn:
            result = compute_base_rate(conn, cls, window=window, library_version=current_version())
    except KeyError as exc:
        click.echo(f"unknown class_id {class_id!r}", err=True)
        raise click.exceptions.Exit(code=1) from exc
    finally:
        store.close()

    click.echo(f"class_id:                 {result.class_id}")
    click.echo(
        f"window:                   {result.window_start.isoformat()} -> "
        f"{result.window_end.isoformat()}"
    )
    click.echo(f"occurrences:              {result.occurrences}")
    click.echo(f"rate_per_year:            {result.rate_per_year:.4f}")
    click.echo(
        f"credible_interval:        "
        f"[{result.credible_interval_lower:.4f}, "
        f"{result.credible_interval_upper:.4f}]"
    )
    click.echo(
        f"prior:                    Beta(alpha={result.prior_alpha}, beta={result.prior_beta})"
    )
    if result.low_sample_warning:
        click.echo("warning:                  low_sample (n < 5)")
    if result.source_stale_warning:
        click.echo("warning:                  source_stale")
