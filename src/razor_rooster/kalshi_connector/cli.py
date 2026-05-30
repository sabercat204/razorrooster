"""``razor-rooster kalshi`` CLI (T-KSI-001 / T-KSI-021 / T-KSI-051 /
T-KSI-060; design §3.11 & §8).

Subcommands:

- ``version``                Print the kalshi_connector schema namespace.
- ``ack-tos``                Record acknowledgement of the current Kalshi ToS.
- ``status``                 Print Kalshi source freshness and sync state.
- ``sync``                   Run cutoff → series → events → markets → prices
                             → settlements → trades in dependency order.
- ``snapshot-prices``        Run price snapshots only (use ``--watched`` for
                             the watched-markets-only short cycle).
- ``backfill-settlements``   One-shot historical settlement backfill.
- ``watch / unwatch / list-watched``  Watched-market list management.
- ``fetch-orderbook``        On-demand orderbook fetch (depth ≤ 10).
- ``map / needs-review / mapping-stats``  Sector mapping triage.

Every subcommand runs the eligibility allow-list gate and the ToS
acknowledgement gate before touching the API. Refusals exit non-zero
with an actionable message naming the file the operator should edit.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Final

import click
import yaml

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.kalshi_connector.client.rest import KalshiRESTClient
from razor_rooster.kalshi_connector.config.loader import (
    KalshiConfig,
    load_kalshi_config,
    load_kalshi_sector_keywords,
)
from razor_rooster.kalshi_connector.gates.eligibility import (
    EligibilityRefusal,
    check_eligibility,
)
from razor_rooster.kalshi_connector.gates.tos import (
    DEFAULT_KALSHI_TOS_URL,
    ToSGateError,
    check_tos_acknowledged,
    fetch_current_tos_hash,
    record_acknowledgement,
)
from razor_rooster.kalshi_connector.mapping.sector_overrides import (
    mapping_stats as kalshi_mapping_stats,
)
from razor_rooster.kalshi_connector.mapping.sector_overrides import (
    needs_review as kalshi_needs_review,
)
from razor_rooster.kalshi_connector.mapping.sector_overrides import (
    set_override,
)
from razor_rooster.kalshi_connector.persistence.migrations import (
    run_pending_kalshi_migrations,
)
from razor_rooster.kalshi_connector.persistence.source import (
    KALSHI_LIVE_SOURCE_ID,
    KALSHI_SETTLEMENTS_SOURCE_ID,
    register_kalshi_sources,
)
from razor_rooster.kalshi_connector.sync.cutoff import snapshot_cutoff
from razor_rooster.kalshi_connector.sync.events import sync_events
from razor_rooster.kalshi_connector.sync.markets import sync_markets
from razor_rooster.kalshi_connector.sync.orderbook import fetch_orderbook
from razor_rooster.kalshi_connector.sync.prices import snapshot_prices
from razor_rooster.kalshi_connector.sync.series import sync_series
from razor_rooster.kalshi_connector.sync.settlements import sync_settlements
from razor_rooster.kalshi_connector.sync.trades import sync_trades

logger = logging.getLogger(__name__)


_DEFAULT_DB_PATH_ENV: Final[str] = "RAZOR_ROOSTER_DB"
_DEFAULT_DB_PATH: Final[Path] = Path("data") / "trough.duckdb"
_DEFAULT_KALSHI_CONFIG: Final[Path] = Path("config") / "kalshi.yaml"


# Allowed Razor sectors for ``kalshi map`` overrides.
_ALLOWED_SECTORS: Final[tuple[str, ...]] = (
    "public_health",
    "geopolitical",
    "regulatory",
    "commodity",
    "climate",
    "infrastructure_energy",
    "macroeconomic",
    "cross_cutting",
    "out_of_scope",
)


def _resolve_db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env_path = os.environ.get(_DEFAULT_DB_PATH_ENV)
    if env_path:
        return Path(env_path)
    return _DEFAULT_DB_PATH


def _open_store_with_migrations(db_path: Path) -> DuckDBStore:
    """Open the DuckDB store and apply both data_ingest and Kalshi migrations.

    Also registers the ``kalshi`` and ``kalshi_settlements`` source rows
    so the freshness view picks them up.
    """
    store = DuckDBStore(db_path)
    with store.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_kalshi_migrations(conn)
        register_kalshi_sources(conn)
    return store


def _load_kalshi_config(config_path: Path) -> KalshiConfig:
    """Load the Kalshi config file; exit cleanly on failure."""
    if not config_path.exists():
        click.echo(
            f"Kalshi config not found at {config_path}. Copy or create the "
            "file before running this command.",
            err=True,
        )
        raise click.exceptions.Exit(code=3)
    try:
        return load_kalshi_config(config_path)
    except Exception as exc:
        click.echo(f"failed to load Kalshi config from {config_path}: {exc}", err=True)
        raise click.exceptions.Exit(code=3) from exc


def _run_gates(store: DuckDBStore, *, config: KalshiConfig) -> None:
    """Run eligibility + ToS gates; exit non-zero on refusal.

    The ToS gate requires the connector to be in the read-only posture
    per REQ-KSI-TOS-002; v2 trading work will provide a separate flow.
    """
    try:
        check_eligibility()
    except EligibilityRefusal as exc:
        click.echo(f"kalshi eligibility gate refused: {exc}", err=True)
        raise click.exceptions.Exit(code=2) from exc

    try:
        with store.connection() as conn:
            check_tos_acknowledged(conn, url=config.tos_url or DEFAULT_KALSHI_TOS_URL)
    except ToSGateError as exc:
        click.echo(f"kalshi ToS gate refused: {exc}", err=True)
        raise click.exceptions.Exit(code=2) from exc


def _build_rest_client(config: KalshiConfig) -> KalshiRESTClient:
    """Build the REST client with the rate-limit + retry envelope from config."""
    return KalshiRESTClient.from_config(config)


@click.group(name="kalshi")
def kalshi() -> None:
    """The Stamp — read-only Kalshi data ingestion.

    Public market data only in v1. No authenticated endpoints,
    no RSA signing, no order placement. Eligibility allow-list and
    ToS acknowledgement gate every subcommand at startup.
    """


@kalshi.command(name="version")
def version() -> None:
    """Print the kalshi_connector subsystem schema namespace."""
    click.echo("kalshi_connector schema namespace: 8001+")


# -- ack-tos --------------------------------------------------------------
@kalshi.command(name="ack-tos")
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path. Default: data/trough.duckdb (or $RAZOR_ROOSTER_DB).",
)
@click.option(
    "--config",
    "config_path_opt",
    type=click.Path(),
    default=None,
    help=f"Kalshi config path. Default: {_DEFAULT_KALSHI_CONFIG}.",
)
@click.option(
    "--yes",
    "skip_prompt",
    is_flag=True,
    default=False,
    help="Skip the interactive prompt (acknowledge non-interactively).",
)
def ack_tos(
    db_path_opt: str | None,
    config_path_opt: str | None,
    skip_prompt: bool,
) -> None:
    """Record acknowledgement of the current Kalshi Terms of Service.

    Fetches the live ToS, hashes it, displays the URL, prompts for
    confirmation, and records the acknowledgement under the v1
    ``read_only`` posture. The eligibility allow-list gate runs first.
    """
    db_path = _resolve_db_path(db_path_opt)
    config_path = Path(config_path_opt) if config_path_opt else _DEFAULT_KALSHI_CONFIG

    try:
        check_eligibility()
    except EligibilityRefusal as exc:
        click.echo(f"kalshi eligibility gate refused: {exc}", err=True)
        raise click.exceptions.Exit(code=2) from exc

    if not db_path.exists():
        click.echo(
            f"DuckDB store not found at {db_path}; run `razor-rooster ingest init` first.",
            err=True,
        )
        raise click.exceptions.Exit(code=1)

    config = _load_kalshi_config(config_path)
    tos_url = config.tos_url or DEFAULT_KALSHI_TOS_URL

    store = _open_store_with_migrations(db_path)
    try:
        try:
            current_hash = fetch_current_tos_hash(url=tos_url)
        except Exception as exc:
            click.echo(
                f"could not fetch current Kalshi ToS from {tos_url}: {exc}",
                err=True,
            )
            raise click.exceptions.Exit(code=4) from exc

        click.echo(f"Kalshi ToS URL:        {tos_url}")
        click.echo(f"Current ToS hash:      {current_hash}")
        click.echo("Acknowledgement posture: read_only (v1)")
        if not skip_prompt:
            click.confirm(
                "Have you reviewed the current Kalshi Terms of Service "
                "and do you accept them under the read-only posture? "
                "This acknowledgement is recorded with your DuckDB store.",
                abort=True,
            )
        with store.connection() as conn:
            record_acknowledgement(conn, tos_version_hash=current_hash)
        click.echo(f"Recorded acknowledgement for hash {current_hash} (read_only).")
    except ToSGateError as exc:
        click.echo(f"kalshi ToS gate error: {exc}", err=True)
        raise click.exceptions.Exit(code=4) from exc
    finally:
        store.close()


# -- status ---------------------------------------------------------------
@kalshi.command(name="status")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def status(db_path_opt: str | None) -> None:
    """Print Kalshi source freshness and sync state."""
    db_path = _resolve_db_path(db_path_opt)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}.", err=True)
        raise click.exceptions.Exit(code=1)
    store = _open_store_with_migrations(db_path)
    try:
        with store.connection() as conn:
            rows = conn.execute(
                "SELECT source_id, last_successful_fetch, "
                "freshness_threshold_seconds, license_terms_hash, "
                "license_acknowledged_at, acknowledged_posture "
                "FROM sources WHERE source_id IN (?, ?)",
                [KALSHI_LIVE_SOURCE_ID, KALSHI_SETTLEMENTS_SOURCE_ID],
            ).fetchall()
            cutoff_row = conn.execute(
                "SELECT market_settled_ts, trades_created_ts, fetched_at "
                "FROM kalshi_historical_cutoff LIMIT 1"
            ).fetchone()
            mappings_total = conn.execute("SELECT COUNT(*) FROM kalshi_sector_mapping").fetchone()
            unmapped = conn.execute(
                "SELECT COUNT(*) FROM kalshi_sector_mapping WHERE razor_sector IS NULL"
            ).fetchone()
    finally:
        store.close()

    click.echo("Kalshi source state:")
    for row in rows:
        click.echo(f"  source_id:                 {row[0]}")
        click.echo(f"    last_successful_fetch:   {row[1]}")
        click.echo(f"    freshness_threshold_s:   {row[2]}")
        ack = row[4]
        posture = row[5]
        click.echo(f"    acknowledged_at:         {ack}")
        click.echo(f"    acknowledged_posture:    {posture}")
    if cutoff_row is not None:
        click.echo("Cutoff snapshot:")
        click.echo(f"  market_settled_ts:         {cutoff_row[0]}")
        click.echo(f"  trades_created_ts:         {cutoff_row[1]}")
        click.echo(f"  fetched_at:                {cutoff_row[2]}")
    else:
        click.echo("Cutoff snapshot:               (none yet)")
    total = mappings_total[0] if mappings_total else 0
    unmapped_count = unmapped[0] if unmapped else 0
    click.echo(f"Sector mappings:              {total} total ({unmapped_count} unmapped)")


# -- sync -----------------------------------------------------------------
@kalshi.command(name="sync")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
@click.option(
    "--config",
    "config_path_opt",
    type=click.Path(),
    default=None,
    help=f"Kalshi config path. Default: {_DEFAULT_KALSHI_CONFIG}.",
)
@click.option(
    "--skip-trades",
    is_flag=True,
    default=False,
    help="Skip the watched-markets trades pull (still runs settlements).",
)
def sync(
    db_path_opt: str | None,
    config_path_opt: str | None,
    skip_trades: bool,
) -> None:
    """Run cutoff → series → events → markets → prices → settlements → trades."""
    db_path = _resolve_db_path(db_path_opt)
    config_path = Path(config_path_opt) if config_path_opt else _DEFAULT_KALSHI_CONFIG
    config = _load_kalshi_config(config_path)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}.", err=True)
        raise click.exceptions.Exit(code=1)

    store = _open_store_with_migrations(db_path)
    try:
        _run_gates(store, config=config)
        client = _build_rest_client(config)
        try:
            try:
                keywords = load_kalshi_sector_keywords(config.sector_mapping.keywords_file)
            except Exception as exc:
                click.echo(
                    f"warning: could not load sector keywords; sector "
                    f"mapping will be skipped: {exc}",
                    err=True,
                )
                keywords = None

            cutoff = snapshot_cutoff(store, client=client)
            click.echo(f"cutoff: market_settled_ts={cutoff.market_settled_ts.isoformat()}")

            series_report = sync_series(store, client=client)
            click.echo(
                f"series:    seen={series_report.series_total_seen} "
                f"inserted={series_report.series_inserted} "
                f"updated={series_report.series_updated} "
                f"removed={series_report.series_removed}"
            )

            events_report = sync_events(store, client=client)
            click.echo(
                f"events:    seen={events_report.events_total_seen} "
                f"inserted={events_report.events_inserted} "
                f"updated={events_report.events_updated} "
                f"removed={events_report.events_removed}"
            )

            markets_report = sync_markets(store, client=client, sector_keywords=keywords)
            click.echo(
                f"markets:   seen={markets_report.markets_total_seen} "
                f"inserted={markets_report.markets_inserted} "
                f"updated={markets_report.markets_updated} "
                f"removed={markets_report.markets_removed} "
                f"mappings_upserted={markets_report.mappings_upserted}"
            )

            prices_report = snapshot_prices(store, client=client)
            click.echo(
                f"prices:    evaluated={prices_report.markets_evaluated} "
                f"inserted={prices_report.snapshots_inserted} "
                f"thin_book={prices_report.snapshots_thin_book}"
            )

            settlements_report = sync_settlements(store, client=client)
            click.echo(
                f"settlements: seen={settlements_report.settlements_seen} "
                f"inserted={settlements_report.settlements_inserted} "
                f"live={settlements_report.routed_to_live} "
                f"historical={settlements_report.routed_to_historical}"
            )

            if skip_trades:
                click.echo("trades:    skipped (--skip-trades)")
            else:
                trades_report = sync_trades(
                    store,
                    client=client,
                    watched_markets=config.sync.prices.watched_markets,
                )
                click.echo(
                    f"trades:    tickers={trades_report.tickers_evaluated} "
                    f"seen={trades_report.trades_seen} "
                    f"inserted={trades_report.trades_inserted}"
                )
        finally:
            client.close()
    finally:
        store.close()


# -- snapshot-prices ------------------------------------------------------
@kalshi.command(name="snapshot-prices")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
@click.option(
    "--config",
    "config_path_opt",
    type=click.Path(),
    default=None,
)
@click.option(
    "--watched/--all",
    "watched_only",
    default=False,
    help="Restrict to the watched_markets list from kalshi.yaml.",
)
def snapshot_prices_cmd(
    db_path_opt: str | None,
    config_path_opt: str | None,
    watched_only: bool,
) -> None:
    """Run the price snapshot operation only."""
    db_path = _resolve_db_path(db_path_opt)
    config_path = Path(config_path_opt) if config_path_opt else _DEFAULT_KALSHI_CONFIG
    config = _load_kalshi_config(config_path)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}.", err=True)
        raise click.exceptions.Exit(code=1)
    store = _open_store_with_migrations(db_path)
    try:
        _run_gates(store, config=config)
        client = _build_rest_client(config)
        try:
            filter_set = config.sync.prices.watched_markets if watched_only else None
            report = snapshot_prices(store, client=client, market_filter=filter_set)
            click.echo(
                f"prices: evaluated={report.markets_evaluated} "
                f"inserted={report.snapshots_inserted} "
                f"thin_book={report.snapshots_thin_book}"
            )
        finally:
            client.close()
    finally:
        store.close()


# -- backfill-settlements -------------------------------------------------
@kalshi.command(name="backfill-settlements")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
@click.option(
    "--config",
    "config_path_opt",
    type=click.Path(),
    default=None,
)
@click.option(
    "--page-size",
    type=int,
    default=100,
    show_default=True,
)
def backfill_settlements_cmd(
    db_path_opt: str | None,
    config_path_opt: str | None,
    page_size: int,
) -> None:
    """One-shot historical settlement backfill.

    Snapshots the cutoff first, then routes the read across
    /markets?status=settled and /historical/markets per design §3.4.
    """
    db_path = _resolve_db_path(db_path_opt)
    config_path = Path(config_path_opt) if config_path_opt else _DEFAULT_KALSHI_CONFIG
    config = _load_kalshi_config(config_path)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}.", err=True)
        raise click.exceptions.Exit(code=1)
    store = _open_store_with_migrations(db_path)
    try:
        _run_gates(store, config=config)
        client = _build_rest_client(config)
        try:
            snapshot_cutoff(store, client=client)
            report = sync_settlements(store, client=client, page_size=page_size)
            click.echo(
                f"settlements: seen={report.settlements_seen} "
                f"inserted={report.settlements_inserted} "
                f"live={report.routed_to_live} "
                f"historical={report.routed_to_historical}"
            )
            if report.errors:
                click.echo(f"errors: {report.errors}", err=True)
        finally:
            client.close()
    finally:
        store.close()


# -- watched-markets management -------------------------------------------
def _read_kalshi_yaml(config_path: Path) -> dict[str, Any]:
    """Read kalshi.yaml as a dict (preserves comments are NOT supported)."""
    if not config_path.exists():
        click.echo(f"Kalshi config not found at {config_path}.", err=True)
        raise click.exceptions.Exit(code=3)
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        click.echo(f"invalid YAML in {config_path}: {exc}", err=True)
        raise click.exceptions.Exit(code=3) from exc
    if not isinstance(data, dict):
        click.echo(f"{config_path} must contain a top-level mapping", err=True)
        raise click.exceptions.Exit(code=3)
    return data


def _write_kalshi_yaml(config_path: Path, data: dict[str, Any]) -> None:
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False)


def _watched_markets_in_yaml(data: dict[str, Any]) -> list[str]:
    sync_section = data.get("sync") or {}
    prices = sync_section.get("prices") or {}
    listed = prices.get("watched_markets") or []
    return [str(t) for t in listed]


def _write_watched_markets(config_path: Path, data: dict[str, Any], tickers: list[str]) -> None:
    sync_section = data.setdefault("sync", {})
    prices = sync_section.setdefault("prices", {})
    prices["watched_markets"] = tickers
    _write_kalshi_yaml(config_path, data)


@kalshi.command(name="watch")
@click.argument("ticker")
@click.option(
    "--config",
    "config_path_opt",
    type=click.Path(),
    default=None,
)
def watch_cmd(ticker: str, config_path_opt: str | None) -> None:
    """Add a ticker to the watched_markets list in kalshi.yaml."""
    config_path = Path(config_path_opt) if config_path_opt else _DEFAULT_KALSHI_CONFIG
    data = _read_kalshi_yaml(config_path)
    listed = _watched_markets_in_yaml(data)
    if ticker in listed:
        click.echo(f"already watched: {ticker}")
        return
    listed.append(ticker)
    _write_watched_markets(config_path, data, listed)
    click.echo(f"watching: {ticker}")


@kalshi.command(name="unwatch")
@click.argument("ticker")
@click.option(
    "--config",
    "config_path_opt",
    type=click.Path(),
    default=None,
)
def unwatch_cmd(ticker: str, config_path_opt: str | None) -> None:
    """Remove a ticker from the watched_markets list."""
    config_path = Path(config_path_opt) if config_path_opt else _DEFAULT_KALSHI_CONFIG
    data = _read_kalshi_yaml(config_path)
    listed = _watched_markets_in_yaml(data)
    if ticker not in listed:
        click.echo(f"not in watched_markets: {ticker}", err=True)
        raise click.exceptions.Exit(code=1)
    listed.remove(ticker)
    _write_watched_markets(config_path, data, listed)
    click.echo(f"unwatched: {ticker}")


@kalshi.command(name="list-watched")
@click.option(
    "--config",
    "config_path_opt",
    type=click.Path(),
    default=None,
)
def list_watched_cmd(config_path_opt: str | None) -> None:
    """List the watched_markets entries from kalshi.yaml."""
    config_path = Path(config_path_opt) if config_path_opt else _DEFAULT_KALSHI_CONFIG
    data = _read_kalshi_yaml(config_path)
    listed = _watched_markets_in_yaml(data)
    if not listed:
        click.echo("(no watched markets)")
        return
    for ticker in listed:
        click.echo(ticker)


# -- fetch-orderbook ------------------------------------------------------
@kalshi.command(name="fetch-orderbook")
@click.argument("ticker")
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
)
@click.option(
    "--config",
    "config_path_opt",
    type=click.Path(),
    default=None,
)
@click.option(
    "--depth",
    type=int,
    default=10,
    show_default=True,
)
@click.option(
    "--persist/--no-persist",
    default=True,
    help="When true (default), the orderbook is written to kalshi_orderbook_snapshots.",
)
def fetch_orderbook_cmd(
    ticker: str,
    db_path_opt: str | None,
    config_path_opt: str | None,
    depth: int,
    persist: bool,
) -> None:
    """Fetch a single orderbook snapshot for ``ticker`` (depth ≤ 10)."""
    db_path = _resolve_db_path(db_path_opt)
    config_path = Path(config_path_opt) if config_path_opt else _DEFAULT_KALSHI_CONFIG
    config = _load_kalshi_config(config_path)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}.", err=True)
        raise click.exceptions.Exit(code=1)
    store = _open_store_with_migrations(db_path)
    try:
        _run_gates(store, config=config)
        client = _build_rest_client(config)
        try:
            if persist:
                report = fetch_orderbook(store, client=client, ticker=ticker, depth=depth)
                click.echo(
                    f"orderbook: ticker={ticker} yes={report.yes_levels} "
                    f"no={report.no_levels} inserted={report.rows_inserted}"
                )
            else:
                ob = client.get_orderbook(ticker, depth=depth)
                click.echo(f"orderbook (in-memory): ticker={ticker}")
                for level in ob.yes_levels:
                    click.echo(f"  yes  ${level.price_dollars:.4f}  x  {level.count}")
                for level in ob.no_levels:
                    click.echo(f"  no   ${level.price_dollars:.4f}  x  {level.count}")
        finally:
            client.close()
    finally:
        store.close()


# -- map / needs-review / mapping-stats -----------------------------------
@kalshi.command(name="map")
@click.argument("ticker")
@click.argument("razor_sector")
@click.option(
    "--secondary",
    multiple=True,
    help="Secondary sector tag; may be repeated.",
)
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def map_cmd(
    ticker: str,
    razor_sector: str,
    secondary: tuple[str, ...],
    db_path_opt: str | None,
) -> None:
    """Manually override the sector mapping for a Kalshi ticker.

    ``razor_sector`` must be one of the eight Razor sectors plus
    ``out_of_scope``, or the literal string ``none`` to record an
    explicit-null mapping.
    """
    sector_value: str | None
    if razor_sector.strip().lower() in ("none", "null", ""):
        sector_value = None
    elif razor_sector in _ALLOWED_SECTORS:
        sector_value = razor_sector
    else:
        click.echo(
            f"unknown razor_sector {razor_sector!r}; allowed: "
            f"{', '.join(_ALLOWED_SECTORS)}, or 'none'.",
            err=True,
        )
        raise click.exceptions.Exit(code=1)

    db_path = _resolve_db_path(db_path_opt)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}.", err=True)
        raise click.exceptions.Exit(code=1)
    store = _open_store_with_migrations(db_path)
    try:
        with store.connection() as conn:
            set_override(
                conn,
                ticker=ticker,
                razor_sector=sector_value,
                secondary=list(secondary) if secondary else None,
            )
        click.echo(f"mapped: {ticker} → {sector_value or '(none)'}")
    finally:
        store.close()


@kalshi.command(name="needs-review")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
@click.option("--limit", type=int, default=20, show_default=True)
def needs_review_cmd(db_path_opt: str | None, limit: int) -> None:
    """List Kalshi tickers the heuristic could not classify.

    Excludes operator-confirmed null mappings (those are explicit
    decisions). Operators triage these and either run
    ``kalshi map <ticker> <sector>`` or ``kalshi map <ticker> none`` to
    confirm.
    """
    db_path = _resolve_db_path(db_path_opt)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}.", err=True)
        raise click.exceptions.Exit(code=1)
    store = _open_store_with_migrations(db_path)
    try:
        with store.connection() as conn:
            rows = kalshi_needs_review(conn, limit=limit)
    finally:
        store.close()
    if not rows:
        click.echo("(no Kalshi tickers awaiting review)")
        return
    for row in rows:
        secondary = ", ".join(row.secondary_sectors) if row.secondary_sectors else "-"
        click.echo(f"{row.ticker:<32} secondary={secondary} mapped_at={row.mapped_at}")


@kalshi.command(name="mapping-stats")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def mapping_stats_cmd(db_path_opt: str | None) -> None:
    """Print Kalshi sector-mapping aggregate counts."""
    db_path = _resolve_db_path(db_path_opt)
    if not db_path.exists():
        click.echo(f"DuckDB store not found at {db_path}.", err=True)
        raise click.exceptions.Exit(code=1)
    store = _open_store_with_migrations(db_path)
    try:
        with store.connection() as conn:
            stats = kalshi_mapping_stats(conn)
    finally:
        store.close()
    click.echo("By sector:")
    for sector, count in sorted(stats.by_sector.items()):
        click.echo(f"  {sector:<24} {count}")
    click.echo(f"  (unmapped):              {stats.unmapped}")
    click.echo("By confidence:")
    for confidence, count in sorted(stats.by_confidence.items()):
        click.echo(f"  {confidence:<24} {count}")
