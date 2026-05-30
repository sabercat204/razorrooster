"""``razor-rooster mispricing`` CLI (T-MD-001 / T-MD-050; design §3.9).

Operator-facing commands for the mispricing detector. Subcommands fill
in across T-MD-020 (mapping CLI), T-MD-040 (cycle), T-MD-041 (linkage),
T-MD-050 (final cycle CLI surface).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import click

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.mapping.operator_overrides import (
    MappingExistsError,
    list_active_operator_mappings,
    register_operator_mapping,
    remove_operator_mapping,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
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
    return store


@click.group(name="mispricing")
def mispricing() -> None:
    """The Liver — model-vs-market comparison layer.

    Comparisons describe disagreements between the model and the
    market at equal prominence; the operator decides whether the
    model or the market is more likely correct. The system never
    recommends action.
    """


@mispricing.command(name="version")
def version() -> None:
    """Print the mispricing_detector subsystem schema version namespace."""
    click.echo("mispricing_detector schema namespace: 4001+")


@mispricing.command(name="map")
@click.argument("class_id")
@click.argument("condition_id")
@click.option(
    "--venue",
    type=click.Choice(["polymarket", "kalshi"]),
    default="polymarket",
    help="Prediction-market venue. 'polymarket' (default) treats "
    "<condition_id> as a Polymarket condition_id; 'kalshi' treats "
    "it as a Kalshi ticker.",
)
@click.option(
    "--type",
    "mapping_type",
    type=click.Choice(["direct", "proxy", "aggregate"]),
    default="direct",
    help="Type of mapping. 'direct' = 1:1, 'proxy' = same domain, "
    "'aggregate' = composite of multiple markets.",
)
@click.option(
    "--polarity",
    type=click.Choice(["aligned", "inverted"]),
    default="aligned",
    help="'aligned' (default) when YES outcome means event happens; "
    "'inverted' when YES means event does NOT happen (e.g. 'will X NOT happen' markets).",
)
@click.option(
    "--notes",
    type=str,
    default=None,
    help="Operator notes recorded with the mapping.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
)
def map_command(
    class_id: str,
    condition_id: str,
    venue: str,
    mapping_type: str,
    polarity: str,
    notes: str | None,
    db_path_opt: str | None,
) -> None:
    """Register an operator-curated class-to-market mapping.

    For Kalshi mappings (--venue kalshi), the supplied <condition_id>
    must resolve to a binary market currently present in
    ``kalshi_markets``. Non-binary Kalshi markets (scalar /
    categorical) are deferred to v1.2 per OQ-KSI-003.
    """
    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        if venue == "kalshi":
            _verify_kalshi_market_is_binary(store, ticker=condition_id)
        with store.connection() as conn:
            try:
                m = register_operator_mapping(
                    conn,
                    class_id=class_id,
                    condition_id=condition_id,
                    mapping_type=mapping_type,  # type: ignore[arg-type]
                    polarity=polarity,  # type: ignore[arg-type]
                    notes=notes,
                    venue=venue,
                )
            except MappingExistsError as exc:
                click.echo(str(exc), err=True)
                raise click.exceptions.Exit(code=2) from exc
    finally:
        store.close()
    click.echo(f"mapping_id:        {m.mapping_id}")
    click.echo(f"class_id:          {m.class_id}")
    click.echo(f"condition_id:      {m.condition_id}")
    click.echo(f"venue:             {m.venue}")
    click.echo(f"type:              {m.mapping_type}")
    click.echo(f"polarity:          {m.polarity}")
    click.echo(f"confidence:        {m.mapping_confidence}")


def _verify_kalshi_market_is_binary(store: DuckDBStore, *, ticker: str) -> None:
    """Refuse non-binary Kalshi mappings (T-KSI-061; OQ-KSI-003).

    Reads ``kalshi_markets`` and exits with a clear error if the row
    is missing or has ``market_type != 'binary'``. Scalar and
    categorical Kalshi markets are deferred to v1.2.
    """
    with store.connection() as conn:
        try:
            row = conn.execute(
                "SELECT market_type FROM kalshi_markets "
                "WHERE ticker = ? AND superseded_at IS NULL "
                "ORDER BY fetch_ts DESC LIMIT 1",
                [ticker],
            ).fetchone()
        except Exception as exc:
            click.echo(
                f"could not query kalshi_markets for ticker {ticker!r}: {exc}",
                err=True,
            )
            raise click.exceptions.Exit(code=4) from exc

    if row is None:
        click.echo(
            f"Kalshi ticker {ticker!r} is not present in kalshi_markets. "
            "Run `razor-rooster kalshi sync` first to populate the catalogue.",
            err=True,
        )
        raise click.exceptions.Exit(code=3)

    market_type = str(row[0]).lower()
    if market_type != "binary":
        click.echo(
            f"Kalshi ticker {ticker!r} is a {market_type!r} market; "
            "non-binary markets are not yet supported in v1. "
            "Widening the surface to scalar / categorical markets "
            "is deferred to v1.2 (see OQ-KSI-003).",
            err=True,
        )
        raise click.exceptions.Exit(code=5)


@mispricing.command(name="unmap")
@click.argument("mapping_id")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def unmap_command(mapping_id: str, db_path_opt: str | None) -> None:
    """Soft-delete an operator-curated mapping by id."""
    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            ok = remove_operator_mapping(conn, mapping_id=mapping_id)
    finally:
        store.close()
    if ok:
        click.echo(f"unmapped: {mapping_id}")
    else:
        click.echo(f"mapping_id {mapping_id!r} not found", err=True)
        raise click.exceptions.Exit(code=1)


@mispricing.command(name="list-mappings")
@click.option(
    "--class",
    "class_id_filter",
    type=str,
    default=None,
    help="Filter by class_id.",
)
@click.option(
    "--confidence",
    type=click.Choice(["exact", "inferred", "low"]),
    default=None,
    help="Filter by mapping_confidence.",
)
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def list_mappings_command(
    class_id_filter: str | None,
    confidence: str | None,
    db_path_opt: str | None,
) -> None:
    """List active class-to-market mappings."""
    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            mappings = list_active_operator_mappings(
                conn,
                class_id=class_id_filter,
                confidence=confidence,  # type: ignore[arg-type]
            )
    finally:
        store.close()
    if not mappings:
        click.echo("(no active mappings matching filter)")
        return
    click.echo(
        f"{'mapping_id':<38} {'class_id':<32} {'condition_id':<22} "
        f"{'type':<10} {'polarity':<10} {'confidence':<10} {'mapped_by':<10}"
    )
    click.echo("-" * 130)
    for m in mappings:
        click.echo(
            f"{m.mapping_id:<38} "
            f"{m.class_id:<32} "
            f"{m.condition_id:<22} "
            f"{m.mapping_type:<10} "
            f"{m.polarity:<10} "
            f"{m.mapping_confidence:<10} "
            f"{m.mapped_by:<10}"
        )


# -- T-MD-050: cycle-running CLI commands ----------------------------------


@mispricing.command(name="run")
@click.option(
    "--class",
    "class_id_filter",
    type=str,
    default=None,
    help="Run for a single class instead of every active mapping.",
)
@click.option(
    "--liquidity-floor",
    type=float,
    default=10000.0,
    help="Minimum 24h volume (USD) below which comparisons are flagged "
    "low_liquidity and surfacing is suppressed.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
)
def run_command(
    class_id_filter: str | None,
    liquidity_floor: float,
    db_path_opt: str | None,
) -> None:
    """Run one comparison cycle over active mappings + auto-derived pairs."""
    from razor_rooster.mispricing_detector.engines.comparator import (
        NoScanAvailableError,
        run_cycle,
    )

    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        try:
            report = run_cycle(
                store, class_id_filter=class_id_filter, liquidity_floor=liquidity_floor
            )
        except NoScanAvailableError as exc:
            click.echo(str(exc), err=True)
            raise click.exceptions.Exit(code=1) from exc
    finally:
        store.close()

    click.echo(f"cycle_id:           {report.cycle_id}")
    click.echo(f"library_version:    {report.library_version}")
    click.echo(f"scan_id:            {report.scan_id}")
    click.echo(f"comparisons:        {len(report.comparisons)}")
    click.echo(f"surfaced:           {report.surfaced}")
    if report.duration_seconds is not None:
        click.echo(f"duration:           {report.duration_seconds:.2f}s")
    if report.suppressed_breakdown:
        click.echo("suppression breakdown:")
        for reason, count in sorted(report.suppressed_breakdown.items()):
            click.echo(f"  {reason}: {count}")
    if report.errors:
        click.echo("cycle-level errors:", err=True)
        for err in report.errors:
            click.echo(f"  - {err}", err=True)
        raise click.exceptions.Exit(code=2)
    for comparison in report.comparisons:
        marker = "*" if comparison.surfaced else " "
        market_str = (
            f"{comparison.market_probability:.4f}"
            if comparison.market_probability is not None
            else "?"
        )
        delta_str = f"{comparison.delta:+.4f}" if comparison.delta is not None else "?"
        click.echo(
            f"  {marker} {comparison.class_id:<32} "
            f"vs {comparison.condition_id:<24} "
            f"model={comparison.model_probability:.4f} "
            f"market={market_str:<8} "
            f"delta={delta_str}"
        )


@mispricing.command(name="show")
@click.argument("comparison_id")
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
def show_command(comparison_id: str, as_json: bool, db_path_opt: str | None) -> None:
    """Render the reasoning trace for a comparison."""
    import json as _json

    from razor_rooster.mispricing_detector.engines.trace import render_trace_text
    from razor_rooster.mispricing_detector.persistence.operations import (
        get_comparison,
        query_trace,
    )

    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            comparison = get_comparison(conn, comparison_id=comparison_id)
            trace = query_trace(conn, comparison_id=comparison_id)
    finally:
        store.close()
    if comparison is None or trace is None:
        click.echo(f"comparison_id {comparison_id!r} not found", err=True)
        raise click.exceptions.Exit(code=1)
    if as_json:
        click.echo(_json.dumps(dict(trace.payload), indent=2))
    else:
        click.echo(render_trace_text(trace.payload))


@mispricing.command(name="list-comparisons")
@click.option(
    "--surfaced-only",
    is_flag=True,
    default=False,
)
@click.option(
    "--since",
    "since_iso",
    type=str,
    default=None,
    help="ISO-8601 timestamp; only return comparisons computed at or after.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
)
def list_comparisons_command(
    surfaced_only: bool,
    since_iso: str | None,
    db_path_opt: str | None,
) -> None:
    """List comparisons with optional surfaced-only and since filters."""
    from datetime import datetime as _datetime

    from razor_rooster.mispricing_detector.persistence.operations import (
        query_comparisons,
    )

    since: _datetime | None = _datetime.fromisoformat(since_iso) if since_iso else None
    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            comparisons = query_comparisons(conn, surfaced_only=surfaced_only, since=since)
    finally:
        store.close()
    if not comparisons:
        click.echo("(no comparisons matching filter)")
        return
    click.echo(f"{'comparison_id':<38} {'class_id':<28} {'market':<22} {'delta':>10}  surfaced")
    click.echo("-" * 110)
    for c in comparisons:
        delta_str = f"{c.delta:+.4f}" if c.delta is not None else "?"
        click.echo(
            f"{c.comparison_id:<38} "
            f"{c.class_id:<28} "
            f"{c.condition_id:<22} "
            f"{delta_str:>10}  "
            f"{'YES' if c.surfaced else 'no'}"
        )


@mispricing.command(name="relink")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def relink_command(db_path_opt: str | None) -> None:
    """Run the linkage pass on demand to catch up on resolutions."""
    from razor_rooster.mispricing_detector.engines.linkage import run_linkage_pass

    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        report = run_linkage_pass(store)
    finally:
        store.close()
    click.echo(f"resolutions processed:  {report.new_resolutions_processed}")
    click.echo(f"new links written:      {report.new_links_written}")
    if report.last_linkage_ts is not None:
        click.echo(f"last_linkage_ts:        {report.last_linkage_ts.isoformat()}")
    if report.errors:
        click.echo("errors:", err=True)
        for err in report.errors:
            click.echo(f"  - {err}", err=True)
        raise click.exceptions.Exit(code=2)
