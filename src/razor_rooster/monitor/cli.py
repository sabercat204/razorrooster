"""``razor-rooster monitor`` CLI (T-MON-001 / T-MON-040; design §3.9).

Operator commands for the monitor:

- ``version`` — print schema namespace.
- ``run`` — run one cycle over all watched + acted-on analyses.
- ``evaluate <analysis_id>`` — evaluate one analysis ad hoc.
- ``show <follow_up_id>`` — print reasoning + key fields.
- ``list-alerts [--tier --since]`` — alerts ordered by tier priority.
- ``trajectory <analysis_id>`` — chronological view across cycles.
- ``note <follow_up_id> "..."`` — append an operator note.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import click

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.monitor.config.loader import load_config
from razor_rooster.monitor.engines.comb import evaluate_analysis, run_cycle
from razor_rooster.monitor.persistence.migrations import (
    run_pending_monitor_migrations,
)
from razor_rooster.monitor.persistence.operations import (
    add_note,
    get_follow_up,
    persist_follow_up,
    query_alerts,
    query_notes,
    query_trajectory,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import get_analysis
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
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
        run_pending_polymarket_migrations(conn)
        run_pending_pattern_library_migrations(conn)
        run_pending_signal_scanner_migrations(conn)
        run_pending_mispricing_migrations(conn)
        run_pending_position_engine_migrations(conn)
        run_pending_monitor_migrations(conn)
    return store


@click.group(name="monitor")
def monitor() -> None:
    """The Comb — active-observation layer for watched analyses.

    Daily-cadence cycle that evaluates change since each watched
    analysis was produced and surfaces ranked alerts. Does not
    recompute analyses; that is position_engine's job.
    """


@monitor.command(name="version")
def version() -> None:
    """Print the monitor subsystem schema namespace."""
    click.echo("monitor schema namespace: 6001+")


@monitor.command(name="run")
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store. Defaults to RAZOR_ROOSTER_DB or data/trough.duckdb.",
)
def run(db_path_str: str | None) -> None:
    """Run one monitor cycle over all watched + acted-on analyses."""
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        report = run_cycle(store)
    finally:
        store.close()
    click.echo(f"cycle_id: {report.cycle_id}")
    click.echo(f"follow_ups_total: {report.follow_ups_total}")
    click.echo(f"follow_ups_with_alerts: {report.follow_ups_with_alerts}")
    click.echo(f"resolutions_detected: {report.resolutions_detected}")
    click.echo(f"expirations_written: {report.expirations_written}")
    if report.alerts_by_tier:
        click.echo("alerts_by_tier:")
        for tier, count in report.alerts_by_tier.items():
            click.echo(f"  {tier}: {count}")
    if report.duration_seconds is not None:
        click.echo(f"duration_seconds: {report.duration_seconds:.3f}")
    if report.errors:
        click.echo("errors:", err=True)
        for err in report.errors:
            click.echo(f"  {err}", err=True)


@monitor.command(name="evaluate")
@click.argument("analysis_id")
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def evaluate(analysis_id: str, db_path_str: str | None) -> None:
    """Evaluate a single analysis ad hoc and persist the follow-up."""
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            analysis = get_analysis(conn, analysis_id=analysis_id)
            if analysis is None:
                click.echo(f"No analysis found for id {analysis_id!r}.", err=True)
                raise click.exceptions.Exit(code=1)
            cycle_id = f"adhoc-{uuid.uuid4()}"
            cfg = load_config()
            now = datetime.now(tz=UTC)
            follow_up = evaluate_analysis(
                conn,
                cycle_id=cycle_id,
                analysis=analysis,
                config=cfg,
                now=now,
            )
            persist_follow_up(conn, follow_up)
    finally:
        store.close()
    click.echo(f"follow_up_id: {follow_up.follow_up_id}")
    click.echo(f"cycle_id: {follow_up.cycle_id}")
    click.echo(f"primary_alert_tier: {follow_up.primary_alert_tier}")
    click.echo(f"recommended_review: {follow_up.recommended_review}")
    click.echo(f"resolution_status: {follow_up.resolution_status}")


@monitor.command(name="show")
@click.argument("follow_up_id")
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def show(follow_up_id: str, db_path_str: str | None) -> None:
    """Print a follow-up's reasoning and key fields."""
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            follow_up = get_follow_up(conn, follow_up_id=follow_up_id)
            if follow_up is None:
                click.echo(f"No follow-up found for id {follow_up_id!r}.", err=True)
                raise click.exceptions.Exit(code=1)
            notes = query_notes(conn, follow_up_id=follow_up_id)
    finally:
        store.close()
    click.echo(f"follow_up_id: {follow_up.follow_up_id}")
    click.echo(f"analysis_id: {follow_up.analysis_id}")
    click.echo(f"cycle_id: {follow_up.cycle_id}")
    click.echo(f"computed_at: {follow_up.computed_at}")
    click.echo(f"resolution_status: {follow_up.resolution_status}")
    click.echo(f"primary_alert_tier: {follow_up.primary_alert_tier}")
    click.echo(f"recommended_review: {follow_up.recommended_review}")
    click.echo(f"days_since_analysis: {follow_up.days_since_analysis}")
    click.echo(f"days_to_resolution: {follow_up.days_to_resolution}")
    if follow_up.error:
        click.echo(f"error: {follow_up.error}", err=True)
    click.echo("---")
    click.echo(follow_up.reasoning_text)
    if notes:
        click.echo("---")
        click.echo(f"notes ({len(notes)}):")
        for note in notes:
            click.echo(f"  [{note.set_at.isoformat()}] ({note.set_by}) {note.note_text}")


@monitor.command(name="list-alerts")
@click.option(
    "--tier",
    default=None,
    type=click.Choice(
        [
            "resolution",
            "invalidation_triggered",
            "material_shift",
            "precursor_shift",
            "time_decay",
        ],
        case_sensitive=False,
    ),
    help="Filter to a specific alert tier.",
)
@click.option(
    "--since",
    default=None,
    help="Only show alerts computed at or after this ISO 8601 timestamp.",
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def list_alerts(tier: str | None, since: str | None, db_path_str: str | None) -> None:
    """List follow-ups with alerts ordered by tier priority + recency."""
    since_dt: datetime | None = None
    if since is not None:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            click.echo(
                f"Invalid --since value {since!r}; must be ISO 8601.",
                err=True,
            )
            raise click.exceptions.Exit(code=1) from None
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            alerts = query_alerts(
                conn,
                tier=tier,  # type: ignore[arg-type]
                since=since_dt,
            )
    finally:
        store.close()
    if not alerts:
        click.echo("No alerts.")
        return
    for follow_up in alerts:
        click.echo(
            f"{follow_up.computed_at.isoformat() if follow_up.computed_at else '?'}  "
            f"{follow_up.primary_alert_tier:<24}  "
            f"{follow_up.follow_up_id}  analysis={follow_up.analysis_id}"
        )


@monitor.command(name="trajectory")
@click.argument("analysis_id")
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def trajectory(analysis_id: str, db_path_str: str | None) -> None:
    """Print all follow-ups for an analysis ordered chronologically."""
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            history = query_trajectory(conn, analysis_id=analysis_id)
    finally:
        store.close()
    if not history:
        click.echo(f"No follow-ups found for analysis {analysis_id!r}.")
        return
    for follow_up in history:
        ts = follow_up.computed_at.isoformat() if follow_up.computed_at is not None else "?"
        model_p = (
            f"{follow_up.current_model_p:.4f}" if follow_up.current_model_p is not None else "—"
        )
        market_p = (
            f"{follow_up.current_market_p:.4f}" if follow_up.current_market_p is not None else "—"
        )
        tier = follow_up.primary_alert_tier or "(quiet)"
        click.echo(
            f"{ts}  model={model_p}  market={market_p}  "
            f"resolution={follow_up.resolution_status}  alert={tier}  "
            f"id={follow_up.follow_up_id}"
        )


@monitor.command(name="note")
@click.argument("follow_up_id")
@click.argument("note_text")
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def note(follow_up_id: str, note_text: str, db_path_str: str | None) -> None:
    """Append an operator note to a follow-up."""
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            existing = get_follow_up(conn, follow_up_id=follow_up_id)
            if existing is None:
                click.echo(f"No follow-up found for id {follow_up_id!r}.", err=True)
                raise click.exceptions.Exit(code=1)
            written = add_note(
                conn,
                follow_up_id=follow_up_id,
                note_text=note_text,
            )
    finally:
        store.close()
    click.echo(f"note_id: {written.note_id}")
    click.echo(f"set_at: {written.set_at.isoformat()}")
