"""``razor-rooster polymarket`` CLI (T-PMC-060; design §8).

Operator-facing commands for the Polymarket connector. Every subcommand
runs the geo and ToS gates before any API call. A startup refusal exits
the process with a non-zero status; the gate's typed exception attaches
the actionable message verbatim.

Subcommands:

- ``ack-tos``               Record acknowledgement of the current ToS hash.
- ``status``                Show freshness + sync state for the source rows.
- ``sync``                  Run markets + prices + resolutions delta.
- ``snapshot [--watched]``  Run price snapshots only.
- ``backfill-resolutions``  One-time historical pull (resumable).
- ``watch / unwatch / list-watched``    Manage the watched-markets list.
- ``fetch-orderbook``       Ad-hoc orderbook display (no persist).
- ``map / needs-review / mapping-stats``    Sector mapping triage.

The watched-markets list is kept in ``config/polymarket.yaml`` under
``sync.prices.watched_markets``. The CLI edits the YAML in place and
preserves ordering.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import click
import yaml

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.polymarket_connector.client.clob_public import (
    ClobPublicClient,
)
from razor_rooster.polymarket_connector.client.gamma import GammaClient
from razor_rooster.polymarket_connector.config.loader import (
    PolymarketConfig,
    load_polymarket_config,
    load_sector_keywords,
)
from razor_rooster.polymarket_connector.gates.geo import (
    StartupRefusal,
    check_jurisdiction,
)
from razor_rooster.polymarket_connector.gates.tos import (
    POLYMARKET_TOS_URL,
    ToSAcknowledgementRequired,
    ToSGateError,
    check_tos_acknowledged,
    fetch_current_tos_hash,
    record_acknowledgement,
)
from razor_rooster.polymarket_connector.mapping.sector_overrides import (
    mapping_stats,
    needs_review,
    set_override,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.source import (
    POLYMARKET_LIVE_SOURCE_ID,
    POLYMARKET_RESOLUTIONS_SOURCE_ID,
    register_polymarket_sources,
)
from razor_rooster.polymarket_connector.sync.markets import sync_markets
from razor_rooster.polymarket_connector.sync.orderbook import fetch_orderbook
from razor_rooster.polymarket_connector.sync.prices import snapshot_prices
from razor_rooster.polymarket_connector.sync.resolutions import (
    backfill_resolutions,
    sync_recent_resolutions,
)

logger = logging.getLogger(__name__)


_DEFAULT_DB_PATH_ENV = "RAZOR_ROOSTER_DB"
_DEFAULT_DB_PATH = Path("data") / "trough.duckdb"
_DEFAULT_POLYMARKET_CONFIG = Path("config") / "polymarket.yaml"


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
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        register_polymarket_sources(conn)
    return store


def _gates_and_config(db_path: Path, *, config_path: Path) -> tuple[DuckDBStore, PolymarketConfig]:
    """Run both startup gates and return the open store + loaded config.

    Any gate failure raises ``click.exceptions.Exit`` after printing an
    actionable message — the goal is to surface refusals to the operator
    in the same shape regardless of subcommand.
    """
    if not db_path.exists():
        click.echo(
            f"DuckDB store not found at {db_path}; run `razor-rooster ingest init` first.",
            err=True,
        )
        raise click.exceptions.Exit(code=1)

    try:
        check_jurisdiction()
    except StartupRefusal as exc:
        click.echo(f"polymarket geo gate refused: {exc}", err=True)
        raise click.exceptions.Exit(code=2) from exc

    store = _open_store_with_migrations(db_path)
    try:
        with store.connection() as conn:
            check_tos_acknowledged(conn)
    except ToSAcknowledgementRequired as exc:
        click.echo(f"polymarket ToS gate refused: {exc}", err=True)
        store.close()
        raise click.exceptions.Exit(code=3) from exc
    except ToSGateError as exc:
        click.echo(f"polymarket ToS gate error: {exc}", err=True)
        store.close()
        raise click.exceptions.Exit(code=3) from exc

    config = load_polymarket_config(config_path)
    return store, config


@click.group()
def polymarket() -> None:
    """Polymarket public-data connector (The Wire)."""


# -- ack-tos --------------------------------------------------------------
@polymarket.command(name="ack-tos")
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path. Default: data/trough.duckdb (or $RAZOR_ROOSTER_DB).",
)
@click.option(
    "--yes",
    "skip_prompt",
    is_flag=True,
    default=False,
    help="Skip the interactive prompt (acknowledge non-interactively).",
)
def ack_tos(db_path_opt: str | None, skip_prompt: bool) -> None:
    """Record acknowledgement of the current Polymarket Terms of Service.

    Fetches the live ToS, hashes it, displays the URL, and records the
    acknowledgement. Re-prompts if the hash has changed.
    """
    db_path = _resolve_db_path(db_path_opt)

    try:
        check_jurisdiction()
    except StartupRefusal as exc:
        click.echo(f"polymarket geo gate refused: {exc}", err=True)
        raise click.exceptions.Exit(code=2) from exc

    if not db_path.exists():
        click.echo(
            f"DuckDB store not found at {db_path}; run `razor-rooster ingest init` first.",
            err=True,
        )
        raise click.exceptions.Exit(code=1)

    store = _open_store_with_migrations(db_path)
    try:
        try:
            current_hash = fetch_current_tos_hash()
        except Exception as exc:
            click.echo(
                f"could not fetch current Polymarket ToS from {POLYMARKET_TOS_URL}: {exc}",
                err=True,
            )
            raise click.exceptions.Exit(code=4) from exc

        click.echo(f"Polymarket ToS URL:  {POLYMARKET_TOS_URL}")
        click.echo(f"Current ToS hash:    {current_hash}")
        if not skip_prompt:
            click.confirm(
                "Have you reviewed the current Polymarket Terms of Service "
                "and do you accept them? This acknowledgement is recorded "
                "with your DuckDB store.",
                abort=True,
            )
        with store.connection() as conn:
            record_acknowledgement(conn, tos_version_hash=current_hash)
        click.echo(f"Recorded acknowledgement for hash {current_hash}.")
    finally:
        store.close()


# -- status ---------------------------------------------------------------
@polymarket.command(name="status")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def status(db_path_opt: str | None) -> None:
    """Print Polymarket source freshness and sync state."""
    db_path = _resolve_db_path(db_path_opt)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}.", err=True)
        raise click.exceptions.Exit(code=1)

    store = _open_store_with_migrations(db_path)
    try:
        with store.connection() as conn:
            rows = conn.execute(
                "SELECT source_id, last_successful_fetch, last_failed_fetch, "
                "license_terms_hash, license_acknowledged_at "
                "FROM sources WHERE source_id LIKE 'polymarket%' ORDER BY source_id"
            ).fetchall()
    finally:
        store.close()

    if not rows:
        click.echo("No Polymarket sources registered yet.")
        return

    click.echo(f"{'source_id':<28} {'last_success':<28} {'tos_acknowledged':<22} {'tos_hash':<16}")
    click.echo("-" * 96)
    for row in rows:
        last_ok = row[1].isoformat() if row[1] is not None else "(never)"
        ack_at = row[4].isoformat() if row[4] is not None else "(never)"
        tos_hash_short = (row[3] or "")[:12]
        click.echo(f"{row[0]:<28} {last_ok:<28} {ack_at:<22} {tos_hash_short:<16}")


# -- sync -----------------------------------------------------------------
@polymarket.command(name="sync")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=str(_DEFAULT_POLYMARKET_CONFIG),
)
def sync(db_path_opt: str | None, config_path: str) -> None:
    """Run a full Polymarket sync: markets + prices + resolutions delta + watched trades."""
    db_path = _resolve_db_path(db_path_opt)
    store, config = _gates_and_config(db_path, config_path=Path(config_path))

    try:
        keywords = load_sector_keywords(config.sector_mapping.keywords_file)
        with GammaClient() as gamma_client:
            markets_report = sync_markets(store, client=gamma_client, sector_keywords=keywords)
            click.echo(
                f"markets: seen={markets_report.markets_total_seen} "
                f"inserted={markets_report.markets_inserted} "
                f"updated={markets_report.markets_updated} "
                f"removed={markets_report.markets_removed} "
                f"mappings_upserted={markets_report.mappings_upserted}"
            )

        with ClobPublicClient() as clob_client:
            prices_report = snapshot_prices(store, client=clob_client)
            click.echo(
                f"prices:  evaluated={prices_report.markets_evaluated} "
                f"snapshots={prices_report.snapshots_inserted} "
                f"thin_book={prices_report.snapshots_thin_book}"
            )

        with GammaClient() as gamma_client:
            res_report = sync_recent_resolutions(store, client=gamma_client)
            click.echo(
                f"resolutions: pages={res_report.pages_fetched} "
                f"inserted={res_report.resolutions_inserted}"
            )

        watched = list(config.sync.prices.watched_markets)
        if watched:
            from razor_rooster.polymarket_connector.sync.trades import (
                pull_watched_trades,
            )

            with ClobPublicClient() as clob_client:
                trades_report = pull_watched_trades(
                    store,
                    client=clob_client,
                    watched_markets=watched,
                )
                click.echo(
                    f"trades:  evaluated={trades_report.markets_evaluated} "
                    f"inserted={trades_report.trades_inserted}"
                )
        else:
            click.echo("trades:  (no watched markets)")
    finally:
        store.close()


# -- snapshot -------------------------------------------------------------
@polymarket.command(name="snapshot")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=str(_DEFAULT_POLYMARKET_CONFIG),
)
@click.option(
    "--watched/--all",
    "watched_only",
    default=False,
    help="If --watched, snapshot only watched markets.",
)
def snapshot(
    db_path_opt: str | None,
    config_path: str,
    watched_only: bool,
) -> None:
    """Run price snapshots only (no markets sync, no resolutions)."""
    db_path = _resolve_db_path(db_path_opt)
    store, config = _gates_and_config(db_path, config_path=Path(config_path))
    try:
        market_filter: list[str] | None = None
        if watched_only:
            market_filter = list(config.sync.prices.watched_markets)
            if not market_filter:
                click.echo("No watched markets configured; nothing to snapshot.")
                return
        with ClobPublicClient() as clob_client:
            report = snapshot_prices(store, client=clob_client, market_filter=market_filter)
        click.echo(
            f"snapshots: evaluated={report.markets_evaluated} "
            f"inserted={report.snapshots_inserted} "
            f"thin_book={report.snapshots_thin_book}"
        )
    finally:
        store.close()


# -- backfill-resolutions -------------------------------------------------
@polymarket.command(name="backfill-resolutions")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
@click.option("--restart", is_flag=True, default=False)
@click.option("--page-size", type=int, default=100, show_default=True)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=str(_DEFAULT_POLYMARKET_CONFIG),
)
def backfill_resolutions_cmd(
    db_path_opt: str | None,
    restart: bool,
    page_size: int,
    config_path: str,
) -> None:
    """Run a resumable historical pull of resolved markets."""
    db_path = _resolve_db_path(db_path_opt)
    store, _config = _gates_and_config(db_path, config_path=Path(config_path))
    try:
        with GammaClient() as client:
            report = backfill_resolutions(
                store, client=client, page_size=page_size, restart=restart
            )
        click.echo(
            f"backfill: status={report.status} pages={report.pages_fetched} "
            f"inserted={report.resolutions_inserted} "
            f"updated={report.resolutions_updated} "
            f"unchanged={report.resolutions_unchanged} "
            f"next_offset={report.next_offset}"
        )
        if report.status == "failed":
            raise click.exceptions.Exit(code=2)
    finally:
        store.close()


# -- watched-markets management -------------------------------------------
def _read_polymarket_config_yaml(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise click.UsageError(
            f"{config_path} top-level must be a mapping, got {type(raw).__name__}"
        )
    return raw


def _write_polymarket_config_yaml(config_path: Path, data: dict[str, Any]) -> None:
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)


def _watched_markets_list(data: dict[str, Any]) -> list[str]:
    return list(data.get("sync", {}).get("prices", {}).get("watched_markets", []) or [])


def _set_watched_markets_list(data: dict[str, Any], watched: list[str]) -> None:
    sync_section = data.setdefault("sync", {})
    prices_section = sync_section.setdefault("prices", {})
    prices_section["watched_markets"] = watched


@polymarket.command(name="watch")
@click.argument("condition_id")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=str(_DEFAULT_POLYMARKET_CONFIG),
)
def watch(condition_id: str, config_path: str) -> None:
    """Add a market to the watched-markets list in the config file."""
    path = Path(config_path)
    data = _read_polymarket_config_yaml(path)
    watched = _watched_markets_list(data)
    if condition_id in watched:
        click.echo(f"{condition_id} is already on the watch list.")
        return
    watched.append(condition_id)
    _set_watched_markets_list(data, watched)
    _write_polymarket_config_yaml(path, data)
    click.echo(f"Added {condition_id} to the watch list.")


@polymarket.command(name="unwatch")
@click.argument("condition_id")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=str(_DEFAULT_POLYMARKET_CONFIG),
)
def unwatch(condition_id: str, config_path: str) -> None:
    """Remove a market from the watched-markets list."""
    path = Path(config_path)
    data = _read_polymarket_config_yaml(path)
    watched = _watched_markets_list(data)
    if condition_id not in watched:
        click.echo(f"{condition_id} is not on the watch list.")
        return
    watched = [m for m in watched if m != condition_id]
    _set_watched_markets_list(data, watched)
    _write_polymarket_config_yaml(path, data)
    click.echo(f"Removed {condition_id} from the watch list.")


@polymarket.command(name="list-watched")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=str(_DEFAULT_POLYMARKET_CONFIG),
)
def list_watched(config_path: str) -> None:
    """List the currently watched markets."""
    path = Path(config_path)
    data = _read_polymarket_config_yaml(path)
    watched = _watched_markets_list(data)
    if not watched:
        click.echo("(no watched markets)")
        return
    for m in watched:
        click.echo(m)


# -- fetch-orderbook ------------------------------------------------------
@polymarket.command(name="fetch-orderbook")
@click.argument("condition_id")
@click.option("--token-id", "outcome_token_id", required=True)
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=str(_DEFAULT_POLYMARKET_CONFIG),
)
@click.option(
    "--persist",
    is_flag=True,
    default=False,
    help="Persist the orderbook snapshot to polymarket_orderbook_snapshots.",
)
def fetch_orderbook_cmd(
    condition_id: str,
    outcome_token_id: str,
    db_path_opt: str | None,
    config_path: str,
    persist: bool,
) -> None:
    """Fetch one orderbook on demand. By default the result is printed only."""
    db_path = _resolve_db_path(db_path_opt)
    store, _config = _gates_and_config(db_path, config_path=Path(config_path))
    try:
        with ClobPublicClient() as client:
            report = fetch_orderbook(
                client=client,
                condition_id=condition_id,
                outcome_token_id=outcome_token_id,
                persist=persist,
                store=store if persist else None,
            )
        if report.orderbook is None:
            click.echo(f"orderbook unavailable for token {outcome_token_id}")
            return
        ob = report.orderbook
        click.echo(f"market={ob.market} asset_id={ob.asset_id} ts={ob.timestamp}")
        click.echo(f"tick_size={ob.tick_size} neg_risk={ob.neg_risk}")
        click.echo("-- bids --")
        for level in ob.bids:
            click.echo(f"  {level.price:.4f}  size={level.size:.2f}")
        click.echo("-- asks --")
        for level in ob.asks:
            click.echo(f"  {level.price:.4f}  size={level.size:.2f}")
        if persist:
            click.echo(f"persisted {report.persisted_levels} levels")
    finally:
        store.close()


# -- sector mapping triage ------------------------------------------------
@polymarket.command(name="map")
@click.argument("condition_id")
@click.argument("sector")
@click.option(
    "--secondary",
    multiple=True,
    help="Optional secondary sectors. Repeatable: --secondary climate --secondary commodity.",
)
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def map_command(
    condition_id: str,
    sector: str,
    secondary: tuple[str, ...],
    db_path_opt: str | None,
) -> None:
    """Record a manual operator override for a market's sector."""
    db_path = _resolve_db_path(db_path_opt)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}.", err=True)
        raise click.exceptions.Exit(code=1)
    store = _open_store_with_migrations(db_path)
    try:
        sector_value: str | None = None if sector.lower() == "none" else sector
        with store.connection() as conn:
            set_override(
                conn,
                condition_id=condition_id,
                razor_sector=sector_value,
                secondary=list(secondary) if secondary else None,
            )
        click.echo(
            f"Set manual override for {condition_id} -> "
            f"{sector_value or '(no sector)'}"
            + (f" (secondary: {', '.join(secondary)})" if secondary else "")
        )
    finally:
        store.close()


@polymarket.command(name="needs-review")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
@click.option("--limit", type=int, default=None)
def needs_review_cmd(db_path_opt: str | None, limit: int | None) -> None:
    """List markets whose heuristic mapping needs operator review."""
    db_path = _resolve_db_path(db_path_opt)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}.", err=True)
        raise click.exceptions.Exit(code=1)
    store = _open_store_with_migrations(db_path)
    try:
        with store.connection() as conn:
            rows = needs_review(conn, limit=limit)
    finally:
        store.close()

    if not rows:
        click.echo("(no markets pending review)")
        return
    click.echo(f"{'condition_id':<48} {'mapped_at':<28}")
    click.echo("-" * 76)
    for row in rows:
        click.echo(f"{row.condition_id:<48} {row.mapped_at.isoformat():<28}")


@polymarket.command(name="mapping-stats")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def mapping_stats_cmd(db_path_opt: str | None) -> None:
    """Show counts by sector and confidence."""
    db_path = _resolve_db_path(db_path_opt)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}.", err=True)
        raise click.exceptions.Exit(code=1)
    store = _open_store_with_migrations(db_path)
    try:
        with store.connection() as conn:
            stats = mapping_stats(conn)
    finally:
        store.close()
    click.echo("By sector:")
    for sector, count in sorted(stats.by_sector.items()):
        click.echo(f"  {sector:<24} {count}")
    click.echo(f"  (unmapped)               {stats.unmapped}")
    click.echo("By confidence:")
    for conf, count in sorted(stats.by_confidence.items()):
        click.echo(f"  {conf:<24} {count}")


# Re-export for unit-test discoverability without polluting the click group.
__all__ = [
    "POLYMARKET_LIVE_SOURCE_ID",
    "POLYMARKET_RESOLUTIONS_SOURCE_ID",
    "polymarket",
]
