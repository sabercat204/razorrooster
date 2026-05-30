"""``razor-rooster position-engine`` CLI (T-PE-001 / T-PE-020 / T-PE-070).

Operator commands for the position engine. Subcommands:

- ``version`` — print schema namespace.
- ``config`` — declare/update bankroll configuration (T-PE-020).

Phase-7 subcommands (run, analyze, show, list, watch, acted-on,
dismiss) are added in T-PE-070 once the analyzer is in place.
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
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.position_engine.config.loader import (
    BankrollValidationError,
    load_config,
    validate_bankroll_inputs,
)
from razor_rooster.position_engine.models import BankrollConfig, WatchStateValue
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.position_engine.persistence.operations import (
    latest_bankroll_config,
    write_bankroll_config,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)

logger = logging.getLogger(__name__)


_DEFAULT_DB_PATH_ENV = "RAZOR_ROOSTER_DB"
_DEFAULT_DB_PATH = Path("data") / "trough.duckdb"


_BANKROLL_DISCLAIMER = (
    "This is an analytical bankroll figure for sizing math. The system "
    "does not track real capital; the operator is responsible for any "
    "real-world action."
)


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
    return store


@click.group(name="position-engine")
def position_engine() -> None:
    """The Spur — paper-analysis sizing layer.

    Produces Kelly + half-Kelly sizing analyses for surfaced
    mispricing comparisons. Outputs use conditional language ("if
    the operator chose to act"); the renderer linter rejects
    imperative phrases. The system never places orders.
    """


@position_engine.command(name="version")
def version() -> None:
    """Print the position_engine subsystem schema namespace."""
    click.echo("position_engine schema namespace: 5001+")


@position_engine.command(name="config")
@click.option(
    "--bankroll",
    "analytical_bankroll_usd",
    type=float,
    required=True,
    help="Analytical bankroll figure in USD. Used for sizing math; "
    "the system does NOT track real capital.",
)
@click.option(
    "--max-pct",
    "max_single_position_pct",
    type=float,
    default=None,
    help="Hard cap on single-position size as fraction of bankroll. "
    "Default 0.05; max allowed 0.25.",
)
@click.option(
    "--kelly-fraction",
    "kelly_fraction_default",
    type=float,
    default=None,
    help="How aggressive the suggested fraction is. Default 0.5 (half-Kelly); max allowed 0.5.",
)
@click.option(
    "--min-edge",
    "min_edge_threshold",
    type=float,
    default=None,
    help="Minimum |delta| in probability units below which no analysis is computed. Default 0.03.",
)
@click.option(
    "--no-prompt",
    is_flag=True,
    default=False,
    help="Skip the interactive disclaimer-confirmation prompt. "
    "Requires --acknowledge-analytical for non-interactive use.",
)
@click.option(
    "--acknowledge-analytical",
    is_flag=True,
    default=False,
    help="Acknowledge that the bankroll figure is analytical-only. "
    "Required when using --no-prompt.",
)
@click.option(
    "--notes",
    type=str,
    default=None,
    help="Operator notes recorded with the config snapshot.",
)
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def config_command(
    analytical_bankroll_usd: float,
    max_single_position_pct: float | None,
    kelly_fraction_default: float | None,
    min_edge_threshold: float | None,
    no_prompt: bool,
    acknowledge_analytical: bool,
    notes: str | None,
    db_path_opt: str | None,
) -> None:
    """Declare or update the analytical bankroll configuration."""
    cfg = load_config()
    defaults = cfg.bankroll_defaults
    bounds = cfg.bankroll_validation

    resolved_max_pct = (
        max_single_position_pct
        if max_single_position_pct is not None
        else defaults.max_single_position_pct
    )
    resolved_kelly = (
        kelly_fraction_default
        if kelly_fraction_default is not None
        else defaults.kelly_fraction_default
    )
    resolved_min_edge = (
        min_edge_threshold if min_edge_threshold is not None else defaults.min_edge_threshold
    )

    try:
        validate_bankroll_inputs(
            analytical_bankroll_usd=analytical_bankroll_usd,
            max_single_position_pct=resolved_max_pct,
            kelly_fraction_default=resolved_kelly,
            min_edge_threshold=resolved_min_edge,
            bounds=bounds,
        )
    except BankrollValidationError as exc:
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(code=2) from exc

    click.echo(_BANKROLL_DISCLAIMER)
    if no_prompt:
        if not acknowledge_analytical:
            click.echo(
                "--no-prompt requires --acknowledge-analytical to confirm "
                "the operator understands this is an analytical figure.",
                err=True,
            )
            raise click.exceptions.Exit(code=2)
    else:
        if not click.confirm(
            "Proceed with this analytical bankroll configuration?",
            default=False,
        ):
            click.echo("aborted; no config written")
            raise click.exceptions.Exit(code=1)

    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            previous = latest_bankroll_config(conn)
            new_config = BankrollConfig(
                config_id=str(uuid.uuid4()),
                analytical_bankroll_usd=analytical_bankroll_usd,
                max_single_position_pct=resolved_max_pct,
                kelly_fraction_default=resolved_kelly,
                min_edge_threshold=resolved_min_edge,
                effective_at=datetime.now(tz=UTC),
                updated_by="operator",
                notes=notes,
            )
            write_bankroll_config(conn, new_config)
    finally:
        store.close()

    click.echo(f"config_id:                  {new_config.config_id}")
    click.echo(f"analytical_bankroll_usd:    ${new_config.analytical_bankroll_usd:.2f}")
    click.echo(f"max_single_position_pct:    {new_config.max_single_position_pct:.4f}")
    click.echo(f"kelly_fraction_default:     {new_config.kelly_fraction_default:.4f}")
    click.echo(f"min_edge_threshold:         {new_config.min_edge_threshold:.4f}")
    click.echo(f"effective_at:               {new_config.effective_at.isoformat()}")
    if previous is not None:
        click.echo(
            f"replaces config_id:         {previous.config_id} "
            f"(${previous.analytical_bankroll_usd:.2f})"
        )


# -- T-PE-060 / T-PE-070: watch + analysis subcommands --------------------


@position_engine.command(name="run")
@click.option(
    "--include-suppressed",
    is_flag=True,
    default=False,
    help="Include non-surfaced comparisons in the analysis pass.",
)
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def run_command(include_suppressed: bool, db_path_opt: str | None) -> None:
    """Run one analysis cycle over surfaced comparisons."""
    from razor_rooster.position_engine.engines.analyzer import (
        NoBankrollConfigError,
        run_cycle,
    )

    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        try:
            report = run_cycle(store, include_suppressed=include_suppressed)
        except NoBankrollConfigError as exc:
            click.echo(str(exc), err=True)
            raise click.exceptions.Exit(code=1) from exc
    finally:
        store.close()
    click.echo(f"cycle_id:                       {report.cycle_id}")
    click.echo(f"bankroll_config_id:             {report.bankroll_config_id}")
    click.echo(f"analyses_total:                 {report.analyses_total}")
    click.echo(f"analyses_with_positive_kelly:   {report.analyses_with_positive_kelly}")
    click.echo(f"analyses_clamped_by_cap:        {report.analyses_clamped_by_cap}")
    click.echo(f"analyses_clamped_by_liquidity:  {report.analyses_clamped_by_liquidity}")
    if report.duration_seconds is not None:
        click.echo(f"duration:                       {report.duration_seconds:.2f}s")
    if report.errors:
        click.echo("cycle-level errors:", err=True)
        for err in report.errors:
            click.echo(f"  - {err}", err=True)
        raise click.exceptions.Exit(code=2)
    for analysis in report.analyses:
        marker = "*" if analysis.suggested_fraction > 0 else " "
        sub = " (sub_threshold)" if analysis.sub_threshold else ""
        click.echo(
            f"  {marker} {analysis.class_id:<32} "
            f"vs {analysis.condition_id:<22} "
            f"kelly={analysis.kelly_unclamped:+.4f}  "
            f"suggested={analysis.suggested_fraction:.4f}"
            f"{sub}"
        )


@position_engine.command(name="analyze")
@click.argument("comparison_id")
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def analyze_command(comparison_id: str, db_path_opt: str | None) -> None:
    """Run the analysis pipeline for one comparison ad-hoc."""
    import uuid as _uuid

    from razor_rooster.position_engine.config.loader import load_config
    from razor_rooster.position_engine.engines.analyzer import analyze_comparison
    from razor_rooster.position_engine.models import AnalysisCycle as _AC
    from razor_rooster.position_engine.persistence.operations import (
        latest_bankroll_config,
        persist_analysis,
        persist_analysis_trace,
        write_cycle,
    )

    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            bankroll_cfg = latest_bankroll_config(conn)
        if bankroll_cfg is None:
            click.echo(
                "no bankroll_config; run "
                "`razor-rooster position-engine config --bankroll <usd>` first",
                err=True,
            )
            raise click.exceptions.Exit(code=1)
        cycle_id = str(_uuid.uuid4())
        started = datetime.now(tz=UTC)
        with store.connection() as conn:
            write_cycle(
                conn,
                _AC(
                    cycle_id=cycle_id,
                    started_at=started,
                    completed_at=None,
                    bankroll_config_id=bankroll_cfg.config_id,
                    analyses_total=0,
                    analyses_with_positive_kelly=0,
                    analyses_clamped_by_cap=0,
                    analyses_clamped_by_liquidity=0,
                ),
            )
        result = analyze_comparison(
            store=store,
            cycle_id=cycle_id,
            comparison_id=comparison_id,
            bankroll_config=bankroll_cfg,
            pe_config=load_config(),
            now=started,
        )
        if result is None:
            click.echo(f"comparison_id {comparison_id!r} not found", err=True)
            raise click.exceptions.Exit(code=1)
        analysis, trace = result
        with store.connection() as conn:
            persist_analysis(conn, analysis)
            persist_analysis_trace(conn, trace)
    finally:
        store.close()
    click.echo(f"analysis_id:        {analysis.analysis_id}")
    click.echo(f"class_id:           {analysis.class_id}")
    click.echo(f"comparison_id:      {analysis.comparison_id}")
    click.echo(f"suggested_fraction: {analysis.suggested_fraction:.4f}")
    if analysis.error is not None:
        click.echo(f"error:              {analysis.error}", err=True)


@position_engine.command(name="show")
@click.argument("analysis_id")
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Include the sensitivity-analysis section.",
)
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def show_command(analysis_id: str, verbose: bool, db_path_opt: str | None) -> None:
    """Print a rendered analysis."""
    from razor_rooster.position_engine.frame.renderer import render
    from razor_rooster.position_engine.persistence.operations import (
        get_analysis,
        get_analysis_trace,
    )

    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            analysis = get_analysis(conn, analysis_id=analysis_id)
            trace = get_analysis_trace(conn, analysis_id=analysis_id)
    finally:
        store.close()
    if analysis is None:
        click.echo(f"analysis_id {analysis_id!r} not found", err=True)
        raise click.exceptions.Exit(code=1)
    if verbose and analysis.sensitivity_analysis is not None:
        # Re-render with verbose=True so sensitivity section is included.
        from razor_rooster.position_engine.config.loader import load_config

        cfg = load_config()
        bankroll_usd = (
            cfg.bankroll_defaults.analytical_bankroll_usd
            if trace is None
            else float(trace.structured_dict.get("analytical_bankroll_usd", 1000.0))
        )
        click.echo(
            render(
                analysis,
                bankroll_usd=bankroll_usd,
                class_title=(
                    trace.structured_dict.get("class_title") if trace else analysis.class_id
                ),
                sector=(trace.structured_dict.get("sector") if trace else None),
                market_spread_bps=(
                    trace.structured_dict.get("market_spread_bps") if trace else None
                ),
                log_odds_delta=(trace.structured_dict.get("log_odds_delta") if trace else None),
                verbose=True,
            )
        )
    elif trace is not None:
        click.echo(trace.rendered_text)
    else:
        click.echo("(no rendered trace persisted for this analysis)", err=True)


@position_engine.command(name="watch")
@click.argument("analysis_id")
@click.option("--note", type=str, default=None)
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def watch_command(analysis_id: str, note: str | None, db_path_opt: str | None) -> None:
    """Mark an analysis as 'watching'."""
    _set_state_command(analysis_id, "watching", note=note, db_path_opt=db_path_opt)


@position_engine.command(name="acted-on")
@click.argument("analysis_id")
@click.option("--note", type=str, default=None)
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def acted_on_command(analysis_id: str, note: str | None, db_path_opt: str | None) -> None:
    """Mark an analysis as 'acted_on' (the operator declares they took action)."""
    _set_state_command(analysis_id, "acted_on", note=note, db_path_opt=db_path_opt)


@position_engine.command(name="dismiss")
@click.argument("analysis_id")
@click.option("--reason", type=str, default=None)
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def dismiss_command(analysis_id: str, reason: str | None, db_path_opt: str | None) -> None:
    """Mark an analysis as 'dismissed'."""
    _set_state_command(analysis_id, "dismissed", note=reason, db_path_opt=db_path_opt)


@position_engine.command(name="list")
@click.option(
    "--watched",
    is_flag=True,
    default=False,
    help="List analyses with state='watching'.",
)
@click.option(
    "--acted-on",
    is_flag=True,
    default=False,
    help="List analyses with state='acted_on'.",
)
@click.option(
    "--dismissed",
    is_flag=True,
    default=False,
    help="List analyses with state='dismissed'.",
)
@click.option(
    "--expired",
    is_flag=True,
    default=False,
    help="List analyses with state='expired'.",
)
@click.option("--db", "db_path_opt", type=click.Path(), default=None)
def list_command(
    watched: bool,
    acted_on: bool,
    dismissed: bool,
    expired: bool,
    db_path_opt: str | None,
) -> None:
    """List analyses by watch state. Provide exactly one --state flag."""
    from razor_rooster.position_engine.persistence.operations import list_by_state

    flag_count = sum([watched, acted_on, dismissed, expired])
    if flag_count != 1:
        click.echo(
            "specify exactly one of --watched / --acted-on / --dismissed / --expired",
            err=True,
        )
        raise click.exceptions.Exit(code=2)
    state: WatchStateValue = (
        "watching"
        if watched
        else ("acted_on" if acted_on else ("dismissed" if dismissed else "expired"))
    )
    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            rows = list_by_state(conn, state=state)
    finally:
        store.close()
    if not rows:
        click.echo(f"(no analyses currently in state {state!r})")
        return
    click.echo(f"{'analysis_id':<38} {'set_at':<26} {'set_by':<10}  notes")
    click.echo("-" * 100)
    for ws in rows:
        click.echo(
            f"{ws.analysis_id:<38} {ws.set_at.isoformat():<26} {ws.set_by:<10}  {ws.notes or ''}"
        )


def _set_state_command(
    analysis_id: str, state: str, *, note: str | None, db_path_opt: str | None
) -> None:
    from razor_rooster.position_engine.persistence.operations import (
        append_watch_state,
        get_analysis,
    )

    db_path = _resolve_db_path(db_path_opt)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            analysis = get_analysis(conn, analysis_id=analysis_id)
            if analysis is None:
                click.echo(f"analysis_id {analysis_id!r} not found", err=True)
                raise click.exceptions.Exit(code=1)
            ws = append_watch_state(
                conn,
                analysis_id=analysis_id,
                state=state,  # type: ignore[arg-type]
                notes=note,
                set_by="operator",
            )
    finally:
        store.close()
    click.echo(f"state set: {ws.state} for analysis_id={ws.analysis_id}")
    if note:
        click.echo(f"note:      {note}")
