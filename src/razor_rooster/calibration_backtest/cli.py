"""``razor-rooster calibration-backtest`` CLI (T-CB-028, T-CB-031; design §3.9).

Operator-facing commands for the calibration backtest. The Phase-5
scaffold lands the click subgroup, the canonical ``_open_store`` helper
(running every upstream migration in dependency order), and the
``@run`` subcommand that dispatches to
:func:`razor_rooster.calibration_backtest.engines.replay.run_backtest`.

T-CB-031 wires the run command to the real renderers (T-CB-029,
T-CB-030) and adds bare-run defaults so an operator can invoke
``razor-rooster calibration-backtest run`` against a populated DuckDB
without supplying ``--since``, ``--until``, or ``--class-id``. The
defaults come from the persisted resolution table (earliest
``resolution_ts``), the recent-window cutoff (now - 30d), and the
pattern-library class registry. ``polymarket_resolutions`` empty raises
:class:`BacktestConfigError` with an operator-actionable hint instead
of a cryptic NULL trace.

The ``list``/``show``/``compare``/``prune`` subcommands arrive in
T-CB-032.

Exit-code conventions mirror :mod:`razor_rooster.signal_scanner.cli`:

* ``0`` — success.
* ``1`` — not-found / usage error (e.g. unknown ``--format``, missing
  database, empty bare-run corpus).
* ``2`` — hard failure (uncaught exception during the replay loop,
  framing-linter rejection on a rendered output, etc.).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click
import duckdb

from razor_rooster.calibration_backtest.engines.compare import (
    compare_runs,
    rank_compare_cells,
)
from razor_rooster.calibration_backtest.engines.replay import (
    iter_mapped_resolutions,
    run_backtest,
)
from razor_rooster.calibration_backtest.errors import (
    BacktestConfigError,
    BacktestPersistenceError,
    CalibrationBacktestError,
    RecentWindowError,
    RunNotFoundError,
)
from razor_rooster.calibration_backtest.frame import check_cli_framing
from razor_rooster.calibration_backtest.models import (
    BacktestRun,
    CompareCell,
    RunParameters,
)
from razor_rooster.calibration_backtest.persistence import operations as persistence_ops
from razor_rooster.calibration_backtest.persistence.migrations import (
    run_pending_calibration_backtest_migrations,
)
from razor_rooster.calibration_backtest.persistence.schemas import TABLE_RUNS
from razor_rooster.calibration_backtest.renderers import (
    render_html,
    render_json,
    render_markdown,
    render_terminal,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.pattern_library import library as pattern_library_facade
from razor_rooster.pattern_library.persistence.migrations import (
    run_pending_pattern_library_migrations,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.position_engine.frame.linter import ImperativeLanguageDetected
from razor_rooster.position_engine.persistence.migrations import (
    run_pending_position_engine_migrations,
)
from razor_rooster.signal_scanner.persistence.migrations import (
    run_pending_signal_scanner_migrations,
)

logger = logging.getLogger(__name__)


_DEFAULT_DB_PATH_ENV = "RAZOR_ROOSTER_DB"
_DEFAULT_DB_PATH = Path("data") / "trough.duckdb"

_FORMAT_CHOICES = ("terminal", "markdown", "html", "json")


def _resolve_db_path(option_value: str | None) -> Path:
    """Resolve the DuckDB store path from CLI flag, env var, or default.

    Mirrors :func:`razor_rooster.signal_scanner.cli._resolve_db_path`
    verbatim so operators see one consistent precedence chain across
    every subsystem CLI.
    """
    if option_value:
        return Path(option_value)
    env_path = os.environ.get(_DEFAULT_DB_PATH_ENV)
    if env_path:
        return Path(env_path)
    return _DEFAULT_DB_PATH


def _open_store(
    db_path: Path,
    *,
    require_exists: bool = True,
) -> tuple[duckdb.DuckDBPyConnection, DuckDBStore]:
    """Open the DuckDB store and apply every upstream migration in order.

    Returns ``(conn, store)`` so the run command can pass the same
    connection to :func:`run_backtest` for both the read-side queries
    (``conn``) and the persistence wiring (``persistence_conn=conn``).

    Migrations run on a temporary pooled connection acquired via
    :meth:`DuckDBStore.connection`; the long-lived ``conn`` returned to
    the caller is a separate :func:`duckdb.connect` handle on the same
    on-disk database. DuckDB tolerates multiple connections to one
    file in this single-process scenario, and keeping the migration
    connection scoped to the ``with`` block prevents pool exhaustion
    while still surfacing the canonical migration ordering required by
    every subsystem CLI.

    Mirrors :func:`razor_rooster.monitor.cli._open_store` lines 84-90
    for the migration ordering — every subsystem's migration runner is
    invoked once so a fresh DuckDB file lands every table the backtest
    might query.
    """
    if require_exists and not db_path.exists():
        click.echo(
            f"DuckDB store not found at {db_path}; run `razor-rooster ingest init` first.",
            err=True,
        )
        raise click.exceptions.Exit(code=1)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = DuckDBStore(db_path)
    with store.connection() as migration_conn:
        run_pending_data_ingest_migrations(migration_conn)
        run_pending_polymarket_migrations(migration_conn)
        run_pending_pattern_library_migrations(migration_conn)
        run_pending_signal_scanner_migrations(migration_conn)
        run_pending_mispricing_migrations(migration_conn)
        run_pending_position_engine_migrations(migration_conn)
        run_pending_calibration_backtest_migrations(migration_conn)
    conn = duckdb.connect(database=str(db_path), read_only=False)
    return conn, store


def _parse_bin_count_per_sector(values: tuple[str, ...]) -> dict[str, int]:
    """Parse repeated ``--bin-count-per-sector SECTOR=N`` flags.

    Each value must be ``KEY=N`` where ``N`` is an integer ``>= 2``.
    Returns a dict mapping sector name to bin count. Raises
    :class:`click.BadParameter` on malformed input so click renders the
    error with the standard usage banner and exits non-zero.
    """
    out: dict[str, int] = {}
    for raw in values:
        if "=" not in raw:
            raise click.BadParameter(
                f"--bin-count-per-sector expects KEY=N, got {raw!r}",
                param_hint="--bin-count-per-sector",
            )
        key, _, raw_count = raw.partition("=")
        key = key.strip()
        if not key:
            raise click.BadParameter(
                f"--bin-count-per-sector expects a non-empty sector name in {raw!r}",
                param_hint="--bin-count-per-sector",
            )
        try:
            count = int(raw_count.strip())
        except ValueError as exc:
            raise click.BadParameter(
                f"--bin-count-per-sector expects an integer count in {raw!r}",
                param_hint="--bin-count-per-sector",
            ) from exc
        if count < 2:
            raise click.BadParameter(
                f"--bin-count-per-sector count must be >= 2, got {count!r} in {raw!r}",
                param_hint="--bin-count-per-sector",
            )
        out[key] = count
    return out


def _parse_iso_datetime(value: str, *, flag: str) -> datetime:
    """Parse an ISO-8601 timestamp from a CLI flag, raising click errors."""
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise click.BadParameter(
            f"{flag} must be an ISO-8601 timestamp; got {value!r}",
            param_hint=flag,
        ) from exc


# ---------------------------------------------------------------------------
# Renderer dispatch (T-CB-031 wires the real T-CB-029 / T-CB-030 renderers)
# ---------------------------------------------------------------------------


_RENDERERS: Mapping[str, Callable[[BacktestRun], str]] = {
    "terminal": render_terminal,
    "markdown": render_markdown,
    "html": render_html,
    "json": render_json,
}
"""Format → renderer mapping used by the ``@run`` command.

Keys correspond to the lowercase form of every ``--format`` choice; the
CLI normalises the user-supplied value via :meth:`str.lower` before
look-up so case-insensitive flags (``JSON``, ``Json``, ``json``) all
dispatch to the same callable. The HTML, terminal, and markdown
callables return strings already passed through
:func:`razor_rooster.calibration_backtest.frame.check_cli_framing`; the
JSON renderer bypasses the linter because its consumer is a tool, not
an operator (REQ-CB-CLI-003).
"""


# ---------------------------------------------------------------------------
# Bare-run defaults helpers (T-CB-031; design §3.9)
# ---------------------------------------------------------------------------


def _bare_run(
    since_iso: str | None,
    until_iso: str | None,
    class_ids: tuple[str, ...],
    sectors: tuple[str, ...],
    venues: tuple[str, ...],
) -> bool:
    """Return ``True`` when the operator supplied no scope-narrowing flag.

    The bare-run path engages only when **none** of ``--since``,
    ``--until``, ``--class-id``, ``--sector``, or ``--venue`` was
    supplied. As soon as any one is set the operator is responsible for
    the scope and the CLI requires the inputs the Phase-5 scaffold
    asked for (``--since`` + ``--until`` + ``--class-id``).
    """

    return since_iso is None and until_iso is None and not class_ids and not sectors and not venues


def _earliest_resolution_ts(conn: duckdb.DuckDBPyConnection) -> datetime:
    """Return ``MIN(resolution_ts)`` from ``polymarket_resolutions``.

    Raises :class:`BacktestConfigError` with an operator-actionable
    hint when the table is empty (the value is ``NULL`` for an empty
    aggregate) so the bare-run path fails fast with a helpful message
    rather than later when the replay loop receives a naive ``None``.
    """

    row = conn.execute(
        "SELECT MIN(resolution_ts) FROM polymarket_resolutions",
    ).fetchone()
    if row is None or row[0] is None:
        raise BacktestConfigError(
            "polymarket_resolutions is empty; run razor-rooster ingest "
            "to populate the resolution table before running a bare backtest."
        )
    earliest: datetime = row[0]
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=UTC)
    return earliest


def _bare_class_ids(store: DuckDBStore) -> tuple[str, ...]:
    """Return every active pattern-library class id as a tuple.

    The bare-run path defaults the replay scope to "every class". When
    the registry is empty (a fresh DB with no classes seeded) we surface
    a :class:`BacktestConfigError` with an operator hint pointing at the
    pattern-library bootstrap rather than letting
    :class:`RunParameters` reject an empty ``class_ids`` tuple at
    validation time with a less actionable message.
    """

    summaries = pattern_library_facade.list_classes(store)
    if not summaries:
        raise BacktestConfigError(
            "pattern_library has no registered classes; run "
            "`razor-rooster pattern-library sync` to populate the "
            "registry before running a bare backtest."
        )
    return tuple(summary.class_id for summary in summaries)


def _count_mapped_resolutions(
    conn: duckdb.DuckDBPyConnection,
    *,
    since_ts: datetime,
    until_ts: datetime,
    venues: tuple[str, ...],
    class_ids: tuple[str, ...],
) -> int:
    """Count rows the replay loop would visit.

    Issues the same JOIN the replay loop uses (via
    :func:`iter_mapped_resolutions`) so a zero count surfaces the same
    "no work" condition as the inner loop without instantiating a full
    :class:`BacktestRun`. Returning early with a clear error message is
    cheaper than letting the replay loop emit an empty run row.
    """

    count = 0
    for _row in iter_mapped_resolutions(
        conn,
        since_ts,
        until_ts,
        venues,
        class_ids,
    ):
        count += 1
    return count


# ---------------------------------------------------------------------------
# CLI group + run subcommand (T-CB-028)
# ---------------------------------------------------------------------------


@click.group(name="calibration-backtest")
def calibration_backtest() -> None:
    """The Reckoning — replay-based calibration backtest.

    Replays historical Polymarket resolutions against the frozen state
    of data sources at each prediction timestamp, scores the model
    under the same library_version and class definition_versions that
    produced live predictions, and emits per-sector / per-class Brier
    plus reliability diagnostics. Outputs are decision-support analysis
    only — the subsystem does not place orders or recommend trades.
    """


@calibration_backtest.command(name="run")
@click.option(
    "--since",
    "since_iso",
    type=str,
    default=None,
    help="ISO-8601 inclusive lower bound on resolution_ts.",
)
@click.option(
    "--until",
    "until_iso",
    type=str,
    default=None,
    help="ISO-8601 inclusive upper bound on resolution_ts.",
)
@click.option(
    "--lag-days",
    type=int,
    default=7,
    show_default=True,
    help="Days subtracted from resolution_ts to derive prediction_ts.",
)
@click.option(
    "--class-id",
    "class_ids",
    type=str,
    multiple=True,
    help="Restrict the replay to one or more pattern_library class_ids (repeatable).",
)
@click.option(
    "--sector",
    "sectors",
    type=str,
    multiple=True,
    help="Restrict the replay to one or more sectors (repeatable).",
)
@click.option(
    "--venue",
    "venues",
    type=str,
    multiple=True,
    help="Venue filter for class_market_mappings (repeatable). Default: polymarket.",
)
@click.option(
    "--bin-count",
    type=int,
    default=None,
    help="Override the global reliability-diagram bin count (>= 2).",
)
@click.option(
    "--bin-count-per-sector",
    "bin_count_per_sector_raw",
    type=str,
    multiple=True,
    help="Per-sector bin-count override of the form SECTOR=N (repeatable).",
)
@click.option(
    "--allow-recent",
    is_flag=True,
    default=False,
    help="Bypass the 30-day recent-window guard (REQ-CB-RUN-002).",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(_FORMAT_CHOICES, case_sensitive=False),
    default="terminal",
    show_default=True,
    help="Render the resulting backtest_runs row in this format.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path. Default: data/trough.duckdb (or $RAZOR_ROOSTER_DB).",
)
def run(
    since_iso: str | None,
    until_iso: str | None,
    lag_days: int,
    class_ids: tuple[str, ...],
    sectors: tuple[str, ...],
    venues: tuple[str, ...],
    bin_count: int | None,
    bin_count_per_sector_raw: tuple[str, ...],
    allow_recent: bool,
    output_format: str,
    db_path_opt: str | None,
) -> None:
    """Run one calibration backtest and render the resulting run row.

    Argument resolution order:

    1. **Bare run** — when none of ``--since``, ``--until``,
       ``--class-id``, ``--sector``, or ``--venue`` is supplied, the
       command auto-derives the replay scope from the persisted
       resolution table (``MIN(resolution_ts)`` for ``--since``), the
       recent-window cutoff (``now() - 30d`` for ``--until``), the
       backtest config (``lag_days=7``), and the pattern-library class
       registry (every active class).
    2. **Explicit run** — when any one of those flags is set the operator
       is responsible for the scope; missing required flags exit 1.

    Pre-flight: a smoke-count of the JOIN ``polymarket_resolutions ⋈
    class_market_mappings`` is issued before invoking the replay loop
    so a mis-wired DB path or an empty corpus surfaces with a clear
    operator-facing error rather than an empty run row downstream.

    Output is dispatched by ``--format``: ``terminal``, ``markdown``,
    and ``html`` print the linter-audited rendering; ``json`` prints the
    deterministic JSON document carrying the canonical disclaimer
    field. ``--format`` is case-insensitive (``JSON`` / ``Json`` /
    ``json`` all dispatch to the same renderer).
    """

    db_path = _resolve_db_path(db_path_opt)
    conn, store = _open_store(db_path)

    try:
        try:
            params = _build_params(
                conn=conn,
                store=store,
                since_iso=since_iso,
                until_iso=until_iso,
                lag_days=lag_days,
                class_ids=class_ids,
                sectors=sectors,
                venues=venues,
                bin_count=bin_count,
                bin_count_per_sector_raw=bin_count_per_sector_raw,
                allow_recent=allow_recent,
            )
        except BacktestConfigError as exc:
            click.echo(f"calibration-backtest run: {exc}", err=True)
            raise click.exceptions.Exit(code=1) from exc

        # Smoke-check: zero-row corpora exit 1 with a clear message
        # rather than producing a vacuous run row.
        try:
            mapped_count = _count_mapped_resolutions(
                conn,
                since_ts=params.since_ts,
                until_ts=params.until_ts,
                venues=params.venues,
                class_ids=params.class_ids,
            )
        except BacktestConfigError as exc:
            click.echo(f"calibration-backtest run: {exc}", err=True)
            raise click.exceptions.Exit(code=1) from exc
        if mapped_count == 0:
            click.echo(
                "calibration-backtest run: no mapped resolutions in "
                f"[{params.since_ts.isoformat()} .. {params.until_ts.isoformat()}] "
                f"for venues={list(params.venues)!r} class_ids={list(params.class_ids)!r}; "
                "verify --db points to a populated DuckDB and that "
                "class_market_mappings holds active mappings.",
                err=True,
            )
            raise click.exceptions.Exit(code=1)

        try:
            result = run_backtest(
                params,
                conn=conn,
                store=store,
                persistence_conn=conn,
            )
        except RecentWindowError as exc:
            click.echo(f"recent-window guard tripped: {exc}", err=True)
            raise click.exceptions.Exit(code=1) from exc
        except BacktestConfigError as exc:
            click.echo(f"calibration-backtest run: {exc}", err=True)
            raise click.exceptions.Exit(code=1) from exc
        except CalibrationBacktestError as exc:
            click.echo(f"calibration backtest failed: {exc}", err=True)
            raise click.exceptions.Exit(code=2) from exc
    finally:
        try:
            conn.close()
        finally:
            store.close()

    renderer = _RENDERERS[output_format.lower()]
    try:
        rendered = renderer(result.run)
    except ImperativeLanguageDetected as exc:
        click.echo(
            "calibration-backtest run: rendered output failed the framing "
            f"linter (phrase={exc.phrase!r}, snippet={exc.snippet!r}).",
            err=True,
        )
        raise click.exceptions.Exit(code=2) from exc
    click.echo(rendered)


def _build_params(
    *,
    conn: duckdb.DuckDBPyConnection,
    store: DuckDBStore,
    since_iso: str | None,
    until_iso: str | None,
    lag_days: int,
    class_ids: tuple[str, ...],
    sectors: tuple[str, ...],
    venues: tuple[str, ...],
    bin_count: int | None,
    bin_count_per_sector_raw: tuple[str, ...],
    allow_recent: bool,
) -> RunParameters:
    """Resolve CLI flags + bare-run defaults into a :class:`RunParameters`.

    Centralising the resolution keeps the ``run`` command body tidy and
    makes the unit tests (``test_run_bare_uses_min_resolution_ts`` etc.)
    able to exercise the resolution rules without driving the entire
    replay pipeline.
    """

    bin_count_per_sector = _parse_bin_count_per_sector(bin_count_per_sector_raw)

    if _bare_run(since_iso, until_iso, class_ids, sectors, venues):
        # Bare-run defaults: derive scope from the persisted DB +
        # registry. Operators see a deterministic message when either
        # input is missing.
        since_ts = _earliest_resolution_ts(conn)
        until_ts = datetime.now(UTC) - timedelta(days=30)
        resolved_class_ids = _bare_class_ids(store)
        resolved_sectors: tuple[str, ...] = ()
        resolved_venues: tuple[str, ...] = ("polymarket",)
        resolved_lag_days = lag_days
    else:
        if since_iso is None or until_iso is None or not class_ids:
            raise BacktestConfigError(
                "--since, --until, and --class-id are required when any "
                "scope-narrowing flag is supplied; omit all of them to "
                "use the bare-run defaults."
            )
        since_ts = _parse_iso_datetime(since_iso, flag="--since")
        until_ts = _parse_iso_datetime(until_iso, flag="--until")
        resolved_class_ids = tuple(class_ids)
        resolved_sectors = tuple(sectors)
        resolved_venues = tuple(venues) if venues else ("polymarket",)
        resolved_lag_days = lag_days

    return RunParameters(
        since_ts=since_ts,
        until_ts=until_ts,
        lag_days=resolved_lag_days,
        class_ids=resolved_class_ids,
        sectors=resolved_sectors,
        venues=resolved_venues,
        allow_recent=allow_recent,
        bin_count=bin_count,
        bin_count_per_sector=bin_count_per_sector,
    )


# ---------------------------------------------------------------------------
# Subcommand: list (T-CB-032)
# ---------------------------------------------------------------------------


_RUN_ID_PREFIX_LEN: int = 12
"""Truncated ``run_id`` prefix used by the ``list`` and ``show`` tables."""

_SYSTEM_REVISION_PREFIX_LEN: int = 12
"""Truncated ``system_revision`` prefix used by the ``list`` table."""

_BRIER_DECIMALS: int = 4
"""Decimal places applied when formatting Brier scores in ``list``/``compare`` tables."""


def _fmt_run_brier(value: float | None) -> str:
    """Format an optional Brier score for the list/compare tables."""

    if value is None:
        return "(none)"
    return f"{value:.{_BRIER_DECIMALS}f}"


def _fmt_run_rate(numerator: int, denominator: int) -> str:
    """Format the fallback-polarity rate as a percentage with one decimal.

    Uses ``predictions_scored`` as the denominator so the surfaced value
    matches the persisted ``ScoreSummary.fallback_polarity_rate`` exactly.
    A zero-scored run renders ``(none)`` so the operator does not see a
    misleading 0.0% on a run that never scored.
    """

    if denominator <= 0:
        return "(none)"
    rate = (numerator / denominator) * 100.0
    return f"{rate:.1f}%"


def _render_list_table(runs: tuple[BacktestRun, ...]) -> str:
    """Render the ``list`` subcommand as a fixed-width text table.

    Columns mirror the design §3.9 spec: truncated ``run_id``,
    ISO-formatted ``started_at``, ``library_version``, truncated
    ``system_revision``, ``lag_days``, prediction counters,
    ``overall_brier`` (4-decimal), fallback-polarity rate (%), and the
    lifecycle ``status``. The output is passed through
    :func:`check_cli_framing` so a typo in the column header text
    (which the tests pin) trips the linter at build time rather than at
    operator-render time.
    """

    headers = (
        "run_id",
        "started_at",
        "lib",
        "system_revision",
        "lag",
        "total",
        "scored",
        "skipped",
        "overall_brier",
        "fallback_rate",
        "status",
    )
    rows: list[tuple[str, ...]] = []
    for r in runs:
        rows.append(
            (
                r.run_id[:_RUN_ID_PREFIX_LEN],
                r.started_at.isoformat(),
                str(r.library_version),
                r.system_revision[:_SYSTEM_REVISION_PREFIX_LEN],
                str(r.lag_days),
                str(r.predictions_total),
                str(r.predictions_scored),
                str(r.predictions_skipped),
                _fmt_run_brier(r.overall_brier),
                _fmt_run_rate(r.fallback_polarity_count, r.predictions_scored),
                r.status.value,
            )
        )

    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            if len(cell) > widths[idx]:
                widths[idx] = len(cell)

    lines: list[str] = []
    lines.append("Calibration Backtest runs")
    lines.append("=" * 64)
    lines.append("")
    if not runs:
        lines.append("(no runs found)")
        text = "\n".join(lines) + "\n"
        check_cli_framing(text)
        return text

    header_cells = [headers[i].ljust(widths[i]) for i in range(len(headers))]
    lines.append("  " + "  ".join(header_cells))
    lines.append("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        cells = [row[i].ljust(widths[i]) for i in range(len(headers))]
        lines.append("  " + "  ".join(cells))
    lines.append("")

    text = "\n".join(lines) + "\n"
    check_cli_framing(text)
    return text


def _render_list_json(runs: tuple[BacktestRun, ...]) -> str:
    """Render the ``list`` subcommand as deterministic JSON.

    Bypasses the framing linter (the consumer is a tool, mirroring
    ``--format json`` semantics on ``run``). The shape is one object per
    run carrying the same fields as the table renderer plus the raw
    ``library_version`` and full-length ``system_revision`` so machine
    consumers do not need to undo the truncation.
    """

    payload = {
        "runs": [
            {
                "run_id": r.run_id,
                "started_at": r.started_at.isoformat(),
                "library_version": r.library_version,
                "system_revision": r.system_revision,
                "lag_days": r.lag_days,
                "predictions_total": r.predictions_total,
                "predictions_scored": r.predictions_scored,
                "predictions_skipped": r.predictions_skipped,
                "overall_brier": r.overall_brier,
                "fallback_polarity_count": r.fallback_polarity_count,
                "status": r.status.value,
            }
            for r in runs
        ]
    }
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"


@calibration_backtest.command(name="list")
@click.option(
    "--since",
    "since_iso",
    type=str,
    default=None,
    help="ISO-8601 lower bound on started_at (inclusive).",
)
@click.option(
    "--limit",
    type=int,
    default=50,
    show_default=True,
    help="Maximum number of runs to return (>= 0).",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(_FORMAT_CHOICES, case_sensitive=False),
    default="terminal",
    show_default=True,
    help="Render the list as a terminal table, Markdown, HTML, or JSON.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path. Default: data/trough.duckdb (or $RAZOR_ROOSTER_DB).",
)
def list_runs_cmd(
    since_iso: str | None,
    limit: int,
    output_format: str,
    db_path_opt: str | None,
) -> None:
    """List recent calibration backtest runs in ``started_at`` DESC order.

    Calls :func:`persistence.operations.list_runs` and renders the
    resulting tuple. The ``--limit`` flag caps the result size; the
    ``--since`` flag scopes to runs that started at or after the given
    ISO timestamp. Non-JSON output passes through the framing linter so
    chrome strings stay decision-support framed.
    """

    if limit < 0:
        click.echo(f"--limit must be >= 0, got {limit!r}", err=True)
        raise click.exceptions.Exit(code=1)

    since_ts: datetime | None = None
    if since_iso is not None:
        since_ts = _parse_iso_datetime(since_iso, flag="--since")

    db_path = _resolve_db_path(db_path_opt)
    conn, store = _open_store(db_path)
    try:
        try:
            runs = persistence_ops.list_runs(conn, since=since_ts, limit=limit)
        except BacktestPersistenceError as exc:
            click.echo(f"calibration-backtest list: {exc}", err=True)
            raise click.exceptions.Exit(code=2) from exc
    finally:
        try:
            conn.close()
        finally:
            store.close()

    fmt = output_format.lower()
    try:
        rendered = _render_list_json(runs) if fmt == "json" else _render_list_table(runs)
    except ImperativeLanguageDetected as exc:
        click.echo(
            "calibration-backtest list: rendered output failed the framing "
            f"linter (phrase={exc.phrase!r}, snippet={exc.snippet!r}).",
            err=True,
        )
        raise click.exceptions.Exit(code=2) from exc
    click.echo(rendered, nl=False)


# ---------------------------------------------------------------------------
# Subcommand: show (T-CB-032)
# ---------------------------------------------------------------------------


@calibration_backtest.command(name="show")
@click.argument("run_id", type=str)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(_FORMAT_CHOICES, case_sensitive=False),
    default="terminal",
    show_default=True,
    help="Render format. Reuses the same renderers as `run`.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path. Default: data/trough.duckdb (or $RAZOR_ROOSTER_DB).",
)
def show(run_id: str, output_format: str, db_path_opt: str | None) -> None:
    """Render one calibration backtest run by ``run_id``.

    Calls :func:`persistence.operations.fetch_run`. A missing row raises
    :class:`RunNotFoundError` which the CLI surfaces as a deterministic
    "Run not found" message and exit code 1.
    """

    db_path = _resolve_db_path(db_path_opt)
    conn, store = _open_store(db_path)
    try:
        try:
            run_row = persistence_ops.fetch_run(conn, run_id)
        except BacktestPersistenceError as exc:
            click.echo(f"calibration-backtest show: {exc}", err=True)
            raise click.exceptions.Exit(code=2) from exc
        if run_row is None:
            err = RunNotFoundError(run_id)
            click.echo(f"calibration-backtest show: {err.message}", err=True)
            raise click.exceptions.Exit(code=1) from err
    finally:
        try:
            conn.close()
        finally:
            store.close()

    renderer = _RENDERERS[output_format.lower()]
    try:
        rendered = renderer(run_row)
    except ImperativeLanguageDetected as exc:
        click.echo(
            "calibration-backtest show: rendered output failed the framing "
            f"linter (phrase={exc.phrase!r}, snippet={exc.snippet!r}).",
            err=True,
        )
        raise click.exceptions.Exit(code=2) from exc
    click.echo(rendered, nl=False)


# ---------------------------------------------------------------------------
# Subcommand: compare (T-CB-032)
# ---------------------------------------------------------------------------


_COMPARE_RANK_CHOICES: tuple[str, ...] = ("absolute", "percent")


def _fmt_compare_brier(value: float | None) -> str:
    if value is None:
        return "(none)"
    return f"{value:.{_BRIER_DECIMALS}f}"


def _fmt_compare_delta(value: float | None) -> str:
    if value is None:
        return "(none)"
    return f"{value:+.{_BRIER_DECIMALS}f}"


def _fmt_compare_pct(value: float | None) -> str:
    if value is None:
        return "(none)"
    return f"{value:+.2f}%"


def _fmt_compare_crossed(value: bool | None) -> str:
    if value is None:
        return "(none)"
    return "yes" if value else "no"


def _render_compare_table(cells: list[CompareCell]) -> str:
    """Render the ``compare`` subcommand as a fixed-width text table."""

    headers = (
        "sector",
        "class_id",
        "brier_a",
        "brier_b",
        "delta_absolute",
        "delta_percent",
        "crossed",
        "present_in",
    )
    rows: list[tuple[str, ...]] = []
    for cell in cells:
        rows.append(
            (
                cell.sector,
                cell.class_id,
                _fmt_compare_brier(cell.brier_a),
                _fmt_compare_brier(cell.brier_b),
                _fmt_compare_delta(cell.delta_absolute),
                _fmt_compare_pct(cell.delta_percent),
                _fmt_compare_crossed(cell.crossed_miscalibration_threshold),
                cell.present_in.value,
            )
        )

    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell_text in enumerate(row):
            if len(cell_text) > widths[idx]:
                widths[idx] = len(cell_text)

    lines: list[str] = []
    lines.append("Calibration Backtest comparison")
    lines.append("=" * 64)
    lines.append("")
    if not cells:
        lines.append("(no comparable cells found)")
        text = "\n".join(lines) + "\n"
        check_cli_framing(text)
        return text

    header_cells = [headers[i].ljust(widths[i]) for i in range(len(headers))]
    lines.append("  " + "  ".join(header_cells))
    lines.append("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        rendered_row = [row[i].ljust(widths[i]) for i in range(len(headers))]
        lines.append("  " + "  ".join(rendered_row))
    lines.append("")

    text = "\n".join(lines) + "\n"
    check_cli_framing(text)
    return text


def _render_compare_json(cells: list[CompareCell]) -> str:
    """Render the ``compare`` subcommand as deterministic JSON."""

    payload = {
        "cells": [
            {
                "sector": cell.sector,
                "class_id": cell.class_id,
                "brier_a": cell.brier_a,
                "brier_b": cell.brier_b,
                "delta_absolute": cell.delta_absolute,
                "delta_percent": cell.delta_percent,
                "crossed_miscalibration_threshold": cell.crossed_miscalibration_threshold,
                "present_in": cell.present_in.value,
            }
            for cell in cells
        ]
    }
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"


@calibration_backtest.command(name="compare")
@click.argument("run_a", type=str)
@click.argument("run_b", type=str)
@click.option(
    "--compare-rank-by",
    "rank_by",
    type=click.Choice(_COMPARE_RANK_CHOICES, case_sensitive=False),
    default="absolute",
    show_default=True,
    help="Sort by absolute or percentage delta magnitude (descending).",
)
@click.option(
    "--top",
    "top_n",
    type=int,
    default=None,
    help="Restrict the rendered table to the top N ranked cells (>= 1).",
)
@click.option(
    "--threshold",
    type=float,
    default=None,
    help=(
        "Override the miscalibration threshold (>= 0.0). Defaults to "
        "compare.brier_miscalibration_threshold from backtest.yaml."
    ),
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(_FORMAT_CHOICES, case_sensitive=False),
    default="terminal",
    show_default=True,
    help="Render the ranked cell list in this format.",
)
@click.option(
    "--db",
    "db_path_opt",
    type=click.Path(),
    default=None,
    help="DuckDB path. Default: data/trough.duckdb (or $RAZOR_ROOSTER_DB).",
)
def compare(
    run_a: str,
    run_b: str,
    rank_by: str,
    top_n: int | None,
    threshold: float | None,
    output_format: str,
    db_path_opt: str | None,
) -> None:
    """Compare two backtest runs cell-by-cell on ``(sector, class_id)``.

    Issues the single-CTE compare query, ranks the results by
    ``--compare-rank-by``, optionally truncates to the top N cells, and
    renders the ranked list in the requested format. Non-JSON outputs
    pass through the framing linter.
    """

    if top_n is not None and top_n < 1:
        click.echo(f"--top must be >= 1 when set, got {top_n!r}", err=True)
        raise click.exceptions.Exit(code=1)
    if threshold is not None and threshold < 0.0:
        click.echo(f"--threshold must be >= 0.0 when set, got {threshold!r}", err=True)
        raise click.exceptions.Exit(code=1)

    rank_by_normalized = rank_by.lower()

    db_path = _resolve_db_path(db_path_opt)
    conn, store = _open_store(db_path)
    try:
        try:
            cells = compare_runs(conn, run_a, run_b, threshold=threshold)
        except BacktestPersistenceError as exc:
            click.echo(f"calibration-backtest compare: {exc}", err=True)
            raise click.exceptions.Exit(code=2) from exc
        except BacktestConfigError as exc:
            click.echo(f"calibration-backtest compare: {exc}", err=True)
            raise click.exceptions.Exit(code=1) from exc
    finally:
        try:
            conn.close()
        finally:
            store.close()

    if rank_by_normalized == "absolute":
        ranked = rank_compare_cells(cells, rank_by="absolute")
    else:
        ranked = rank_compare_cells(cells, rank_by="percent")
    if top_n is not None:
        ranked = ranked[:top_n]

    fmt = output_format.lower()
    try:
        rendered = _render_compare_json(ranked) if fmt == "json" else _render_compare_table(ranked)
    except ImperativeLanguageDetected as exc:
        click.echo(
            "calibration-backtest compare: rendered output failed the framing "
            f"linter (phrase={exc.phrase!r}, snippet={exc.snippet!r}).",
            err=True,
        )
        raise click.exceptions.Exit(code=2) from exc

    click.echo(rendered, nl=False)


# ---------------------------------------------------------------------------
# Subcommand: prune (T-CB-032)
# ---------------------------------------------------------------------------


def _list_run_ids_before(
    conn: duckdb.DuckDBPyConnection,
    *,
    before: datetime,
) -> tuple[str, ...]:
    """Return ``run_id`` values whose ``started_at`` precedes *before*.

    The ``prune`` CLI dispatches one :func:`persistence.operations.prune_run`
    call per row so the trace+prediction+run cascade runs inside its
    own transaction per run. Aggregating the row counts here keeps the
    summary output deterministic.
    """

    try:
        rows = conn.execute(
            f"SELECT run_id FROM {TABLE_RUNS} WHERE started_at < ? ORDER BY started_at ASC",
            [before],
        ).fetchall()
    except duckdb.Error as exc:
        raise BacktestPersistenceError(f"list run_ids before {before.isoformat()}: {exc}") from exc
    return tuple(str(row[0]) for row in rows)


@calibration_backtest.command(name="prune")
@click.option(
    "--before",
    "before_iso",
    type=str,
    required=True,
    help="ISO-8601 timestamp; delete runs started before this point.",
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
    help="DuckDB path. Default: data/trough.duckdb (or $RAZOR_ROOSTER_DB).",
)
def prune(before_iso: str, confirm: bool, db_path_opt: str | None) -> None:
    """Delete calibration backtest runs older than ``--before``.

    Mirrors :mod:`razor_rooster.signal_scanner.cli.prune` semantics:
    refuses without ``--confirm`` (exit code 1) so an operator cannot
    accidentally purge analysis history. With ``--confirm`` it queries
    ``backtest_runs`` for matching ``run_id`` values and dispatches one
    :func:`persistence.operations.prune_run` call per row, accumulating
    ``traces``/``predictions``/``runs`` counts and printing a summary.
    """

    if not confirm:
        click.echo("refusing to prune without --confirm", err=True)
        raise click.exceptions.Exit(code=1)

    cutoff = _parse_iso_datetime(before_iso, flag="--before")

    db_path = _resolve_db_path(db_path_opt)
    conn, store = _open_store(db_path)
    totals = {"runs": 0, "predictions": 0, "traces": 0}
    try:
        try:
            run_ids = _list_run_ids_before(conn, before=cutoff)
            for run_id in run_ids:
                counts = persistence_ops.prune_run(conn, run_id)
                totals["runs"] += counts.get("runs", 0)
                totals["predictions"] += counts.get("predictions", 0)
                totals["traces"] += counts.get("traces", 0)
        except BacktestPersistenceError as exc:
            click.echo(f"calibration-backtest prune: {exc}", err=True)
            raise click.exceptions.Exit(code=2) from exc
    finally:
        try:
            conn.close()
        finally:
            store.close()

    click.echo(
        f"Pruned {totals['runs']} runs / {totals['predictions']} predictions / "
        f"{totals['traces']} traces older than {cutoff.isoformat()}"
    )


__all__ = [
    "calibration_backtest",
    "compare",
    "list_runs_cmd",
    "prune",
    "run",
    "show",
]
