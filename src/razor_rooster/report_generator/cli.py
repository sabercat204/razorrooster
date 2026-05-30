"""``razor-rooster report`` CLI (T-RG-001 / T-RG-050; design §3.8).

Operator commands for report generation.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.mispricing_detector.persistence.migrations import (
    run_pending_mispricing_migrations,
)
from razor_rooster.monitor.persistence.migrations import (
    run_pending_monitor_migrations,
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
from razor_rooster.report_generator.engines.generator import generate
from razor_rooster.report_generator.models import ReportRecord
from razor_rooster.report_generator.persistence.migrations import (
    run_pending_report_generator_migrations,
)
from razor_rooster.report_generator.persistence.operations import (
    get_report,
    list_reports,
    list_threshold_measurements,
    query_last_report,
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
        run_pending_position_engine_migrations(conn)
        run_pending_monitor_migrations(conn)
        run_pending_report_generator_migrations(conn)
    return store


@click.group(name="report")
def report() -> None:
    """The Crow — operator-facing report renderer.

    Daily-cadence document that summarizes newly surfaced
    comparisons, watched analyses, calibration log, watchlist, and
    system health. No trading recommendations; conditional language
    only; equal-prominence "case for market" sections.
    """


@report.command(name="version")
def version() -> None:
    """Print the report_generator subsystem schema namespace."""
    click.echo("report_generator schema namespace: 7001+")


@report.command(name="generate")
@click.option(
    "--since",
    default=None,
    help=(
        "ISO 8601 cycle window start. Defaults to the prior report's "
        "generated_at, or 24 hours ago on first run."
    ),
)
@click.option(
    "--markdown",
    "markdown_path_str",
    default=None,
    help="Optional path to write a parallel markdown file.",
)
@click.option(
    "--html",
    "html_path_str",
    default=None,
    help=(
        "Optional path to write a parallel HTML file. "
        "Self-contained: inline CSS, no external assets, no JavaScript."
    ),
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Skip terminal output (useful when only the markdown export is needed).",
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store. Defaults to RAZOR_ROOSTER_DB or data/trough.duckdb.",
)
def generate_cmd(
    since: str | None,
    markdown_path_str: str | None,
    html_path_str: str | None,
    quiet: bool,
    db_path_str: str | None,
) -> None:
    """Generate a report covering the period since the prior report."""
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
    markdown_path = Path(markdown_path_str) if markdown_path_str else None
    html_path = Path(html_path_str) if html_path_str else None
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        result = generate(
            store,
            since=since_dt,
            markdown_path=markdown_path,
            html_path=html_path,
            quiet=quiet,
        )
    finally:
        store.close()
    click.echo(f"report_id: {result.report_id}")
    click.echo(
        f"sections_rendered: {len(result.sections_rendered)} / "
        f"{len(result.sections_enabled)} "
        f"({', '.join(result.sections_rendered)})"
    )
    if result.sections_failed:
        click.echo("sections_failed:", err=True)
        for f in result.sections_failed:
            click.echo(f"  {f.get('section')}: {f.get('error')}", err=True)
    if result.markdown_path:
        click.echo(f"markdown_path: {result.markdown_path}")
    if result.duration_seconds is not None:
        click.echo(f"duration_seconds: {result.duration_seconds:.3f}")


@report.command(name="show")
@click.argument("report_id")
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def show(report_id: str, db_path_str: str | None) -> None:
    """Print the stored terminal text of a previous report."""
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            record = get_report(conn, report_id=report_id)
    finally:
        store.close()
    if record is None:
        click.echo(f"No report found for id {report_id!r}.", err=True)
        raise click.exceptions.Exit(code=1)
    click.echo(record.rendered_terminal_text)


@report.command(name="compare")
@click.argument("report_id_a")
@click.argument("report_id_b")
@click.option(
    "--diff/--no-diff",
    "show_diff",
    default=True,
    show_default=True,
    help="Include the unified terminal-text diff in the output.",
)
@click.option(
    "--diff-lines",
    default=200,
    show_default=True,
    help="Maximum lines of unified diff to print.",
)
@click.option(
    "--html",
    "html_path_str",
    default=None,
    help=(
        "Optional path to write a self-contained two-column "
        "side-by-side HTML view of the comparison. Inline CSS, "
        "no external assets, no JavaScript."
    ),
)
@click.option(
    "--word-diff/--no-word-diff",
    "word_diff",
    default=True,
    show_default=True,
    help=(
        "When set (default), paired deletion/addition lines in "
        "the HTML unified-diff panel get word-level highlights. "
        "Pass --no-word-diff for plain whole-line styling on "
        "narrow viewports."
    ),
)
@click.option(
    "--side-by-side/--no-side-by-side",
    "side_by_side",
    default=True,
    show_default=True,
    help=(
        "When set (default), the HTML page includes a "
        "two-column side-by-side terminal-text panel. Pass "
        "--no-side-by-side to suppress it for a more compact "
        "page focused on the structural diff."
    ),
)
@click.option(
    "--quick-jump/--no-quick-jump",
    "quick_jump",
    default=True,
    show_default=True,
    help=(
        "When set (default), the HTML header includes a nav "
        "block with anchor links to each panel. Pass "
        "--no-quick-jump for a stripped-down header on "
        "single-section views."
    ),
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def compare_cmd(
    report_id_a: str,
    report_id_b: str,
    show_diff: bool,
    diff_lines: int,
    html_path_str: str | None,
    word_diff: bool,
    side_by_side: bool,
    quick_jump: bool,
    db_path_str: str | None,
) -> None:
    """Diff two reports by ID.

    Convention: ``report_id_a`` is older, ``report_id_b`` is newer.
    The CLI prints the metadata changes (sections added/removed,
    library version delta, disclaimer-hash drift, terminal-text
    length delta) and an optional unified-diff preview of the
    rendered terminal text.

    Strictly descriptive — reports observed differences only.
    """
    from razor_rooster.report_generator.engines.compare import compare_reports

    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            record_a = get_report(conn, report_id=report_id_a)
            record_b = get_report(conn, report_id=report_id_b)
    finally:
        store.close()
    missing: list[str] = []
    if record_a is None:
        missing.append(report_id_a)
    if record_b is None:
        missing.append(report_id_b)
    if missing:
        click.echo(
            f"No report found for id(s): {', '.join(repr(m) for m in missing)}.",
            err=True,
        )
        raise click.exceptions.Exit(code=1)
    assert record_a is not None
    assert record_b is not None
    diff = compare_reports(record_a, record_b)
    click.echo(f"a: report {diff.report_id_a}")
    click.echo(
        f"   generated_at: "
        f"{diff.generated_at_a.isoformat() if hasattr(diff.generated_at_a, 'isoformat') else diff.generated_at_a}"
    )
    click.echo(f"b: report {diff.report_id_b}")
    click.echo(
        f"   generated_at: "
        f"{diff.generated_at_b.isoformat() if hasattr(diff.generated_at_b, 'isoformat') else diff.generated_at_b}"
    )
    click.echo(f"time between: {diff.time_between}")
    click.echo("")
    click.echo("metadata:")
    if diff.library_version_changed:
        click.echo(f"  library version: {diff.library_version_a} → {diff.library_version_b}")
    else:
        click.echo(f"  library version: {diff.library_version_a} (unchanged)")
    if diff.disclaimer_changed:
        click.echo(
            f"  disclaimer hash: {diff.disclaimer_hash_a[:8]}… → {diff.disclaimer_hash_b[:8]}…"
        )
    else:
        click.echo("  disclaimer hash: unchanged")
    click.echo("")
    click.echo("sections:")
    if diff.sections_added:
        click.echo(f"  added: {', '.join(diff.sections_added)}")
    if diff.sections_removed:
        click.echo(f"  removed: {', '.join(diff.sections_removed)}")
    if not diff.sections_added and not diff.sections_removed:
        click.echo("  (no presence changes)")
    if diff.sections_failed_diff:
        click.echo(f"  section-failure delta: {', '.join(diff.sections_failed_diff)}")
    click.echo("")
    click.echo(
        f"terminal-text length: {diff.terminal_length_a} → {diff.terminal_length_b} "
        f"({diff.terminal_length_delta:+d} chars)"
    )
    if show_diff and diff.unified_terminal_diff:
        click.echo("")
        click.echo("--- unified diff (truncated to --diff-lines) ---")
        diff_lines_list = diff.unified_terminal_diff.splitlines()
        for line in diff_lines_list[: max(0, int(diff_lines))]:
            click.echo(line)
        if len(diff_lines_list) > diff_lines:
            click.echo(f"... ({len(diff_lines_list) - diff_lines} more lines)")
    if html_path_str is not None:
        from razor_rooster.position_engine.frame.linter import check_text
        from razor_rooster.report_generator.engines.compare_html import (
            render_compare_html,
        )

        html_content = render_compare_html(
            record_a=record_a,
            record_b=record_b,
            diff=diff,
            diff_line_limit=int(diff_lines),
            word_diff=word_diff,
            side_by_side=side_by_side,
            quick_jump=quick_jump,
        )
        check_text(html_content)
        html_path = Path(html_path_str)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html_content, encoding="utf-8")
        click.echo(f"html_path: {html_path}")


@report.command(name="compare-latest")
@click.option(
    "--diff/--no-diff",
    "show_diff",
    default=True,
    show_default=True,
    help="Include the unified terminal-text diff in the output.",
)
@click.option(
    "--diff-lines",
    default=200,
    show_default=True,
    help="Maximum lines of unified diff to print.",
)
@click.option(
    "--html",
    "html_path_str",
    default=None,
    help=(
        "Optional path to write a self-contained two-column "
        "side-by-side HTML view of the comparison. Inline CSS, "
        "no external assets, no JavaScript."
    ),
)
@click.option(
    "--word-diff/--no-word-diff",
    "word_diff",
    default=True,
    show_default=True,
    help=(
        "When set (default), paired deletion/addition lines in "
        "the HTML unified-diff panel get word-level highlights."
    ),
)
@click.option(
    "--side-by-side/--no-side-by-side",
    "side_by_side",
    default=True,
    show_default=True,
    help=(
        "When set (default), the HTML page includes a two-column side-by-side terminal-text panel."
    ),
)
@click.option(
    "--quick-jump/--no-quick-jump",
    "quick_jump",
    default=True,
    show_default=True,
    help=(
        "When set (default), the HTML header includes a nav block with anchor links to each panel."
    ),
)
@click.option(
    "--offset",
    "offset",
    default=0,
    show_default=True,
    type=int,
    help=(
        "Step backward through history. Offset 0 (default) "
        "diffs reports [0] (newest) and [1]. Offset 1 diffs "
        "[1] and [2], and so on. Useful for stepping pair-by-"
        "pair through recent cycles."
    ),
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
@click.pass_context
def compare_latest_cmd(
    ctx: click.Context,
    show_diff: bool,
    diff_lines: int,
    html_path_str: str | None,
    word_diff: bool,
    side_by_side: bool,
    quick_jump: bool,
    offset: int,
    db_path_str: str | None,
) -> None:
    """Diff the two most recent reports (or a pair offset back in time).

    Convenience wrapper over ``report compare``: resolves the two
    newest persisted reports' ids (newer is ``b``, older is ``a``)
    and forwards the remaining flags to the compare path.

    ``--offset N`` steps backward through history: offset 0
    (default) diffs reports ``[0]`` and ``[1]``; offset 1 diffs
    ``[1]`` and ``[2]``; and so on.

    Strictly descriptive — reports observed differences only.
    """
    if offset < 0:
        raise click.BadParameter(f"--offset {offset} must be >= 0")
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            reports = list_reports(conn, limit=offset + 2)
    finally:
        store.close()
    needed = offset + 2
    if len(reports) < needed:
        click.echo(
            f"Need at least {needed} reports for compare-latest --offset {offset}; "
            f"found {len(reports)}.",
            err=True,
        )
        raise click.exceptions.Exit(code=1)
    # list_reports returns newest-first.
    record_b, record_a = reports[offset], reports[offset + 1]
    click.echo(f"comparing latest pair: a={record_a.report_id}  b={record_b.report_id}")
    ctx.invoke(
        compare_cmd,
        report_id_a=record_a.report_id,
        report_id_b=record_b.report_id,
        show_diff=show_diff,
        diff_lines=diff_lines,
        html_path_str=html_path_str,
        word_diff=word_diff,
        side_by_side=side_by_side,
        quick_jump=quick_jump,
        db_path_str=db_path_str,
    )


@report.command(name="list")
@click.option(
    "--since",
    default=None,
    help="Only show reports generated at or after this ISO 8601 timestamp.",
)
@click.option(
    "--limit",
    default=20,
    type=int,
    help="Maximum number of reports to list.",
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def list_cmd(since: str | None, limit: int, db_path_str: str | None) -> None:
    """List historical reports in reverse chronological order."""
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
            reports = list_reports(conn, since=since_dt, limit=limit)
    finally:
        store.close()
    if not reports:
        click.echo("No reports.")
        return
    for r in reports:
        click.echo(
            f"{r.generated_at.isoformat()}  {r.report_id}  "
            f"sections={len(r.sections_rendered)}/{len(r.sections_enabled)}  "
            f"failed={len(r.sections_failed)}"
        )


@report.command(name="latest")
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def latest(db_path_str: str | None) -> None:
    """Print the rendered terminal text of the most recent report."""
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            record = query_last_report(conn)
    finally:
        store.close()
    if record is None:
        click.echo("No reports yet.")
        return
    click.echo(record.rendered_terminal_text)


@report.command(name="digest")
@click.option(
    "--days",
    default=None,
    type=int,
    help=("Window in days to summarize. Range [1, 365]. Default 7 if --since is not set."),
)
@click.option(
    "--since",
    default=None,
    help=(
        "Window start as an ISO 8601 timestamp. Mutually exclusive "
        "with --days. Useful for scoping to 'everything since the "
        "last operator-driven config change'."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help=(
        "Emit JSON Lines: one JSON object per report (newest first) "
        "followed by a single 'aggregate' object. Compatible with "
        "jq, head, and other unix tooling."
    ),
)
@click.option(
    "--report-id",
    "report_id_prefix",
    default=None,
    help=(
        "Optional report-id prefix filter (e.g. 'rpt-2026-05' to "
        "scope to May 2026 cycles). Combines cleanly with --days, "
        "--since, and --json."
    ),
)
@click.option(
    "--sort-by",
    "sort_by",
    type=click.Choice(["generated_at", "sections_failed", "terminal_chars"], case_sensitive=False),
    default="generated_at",
    show_default=True,
    help=(
        "Sort the per-row listing by this field. 'generated_at' "
        "preserves the existing newest-first ordering. "
        "'sections_failed' surfaces the most-failed cycles first. "
        "'terminal_chars' surfaces the longest reports first."
    ),
)
@click.option(
    "--sort-direction",
    "sort_direction",
    type=click.Choice(["asc", "desc"], case_sensitive=False),
    default="desc",
    show_default=True,
    help=("Sort direction. Default 'desc' is newest/longest/highest first; 'asc' inverts."),
)
@click.option(
    "--top",
    "top_n",
    default=None,
    type=int,
    help=(
        "Optional cap on the number of reports listed (after "
        "sorting). Useful with --sort-by to surface only the "
        "first N most-failed or longest cycles. The aggregate "
        "header still reports totals over the full unsliced "
        "window. Range [1, 1000]."
    ),
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def digest_cmd(
    days: int | None,
    since: str | None,
    as_json: bool,
    report_id_prefix: str | None,
    sort_by: str,
    sort_direction: str,
    top_n: int | None,
    db_path_str: str | None,
) -> None:
    """Print a one-line-per-report digest of recent reports.

    Each row shows the report's generated_at timestamp, report_id,
    sections-rendered count, sections-failed count, terminal-text
    length, and (when present) the markdown/html output paths.

    A small aggregate header sits above the rows: report count,
    total cycles with at least one failed section, total cycles
    with markdown / HTML outputs, average sections-rendered, and
    average terminal-text length.

    The window is selected by ``--days N`` (default 7) or
    ``--since ISO``; the two flags are mutually exclusive.

    ``--json`` emits JSON Lines output (one report object per
    line, newest first, followed by a single
    ``{"kind": "aggregate", ...}`` line) — useful for piping
    into jq or other downstream tooling.

    ``--sort-by`` selects the ordering field
    (``generated_at`` / ``sections_failed`` / ``terminal_chars``)
    and ``--sort-direction`` controls direction (``asc``/``desc``).

    Useful for at-a-glance review of cycle activity over a
    window. Strictly descriptive — the digest reports observed
    activity, never ranks or recommends.
    """
    if days is not None and since is not None:
        raise click.BadParameter("--days and --since are mutually exclusive; pick one")
    cutoff: datetime
    window_label: str
    if since is not None:
        try:
            cutoff = datetime.fromisoformat(since)
        except ValueError as exc:
            raise click.BadParameter(f"invalid --since: {exc}") from exc
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        window_label = f"since {cutoff.isoformat()}"
    else:
        effective_days = 7 if days is None else days
        if effective_days < 1 or effective_days > 365:
            raise click.BadParameter(f"--days {effective_days} is out of range [1, 365]")
        cutoff = datetime.now(tz=UTC) - timedelta(days=effective_days)
        window_label = f"in the last {effective_days} day(s)"
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            reports = list_reports(conn, since=cutoff, limit=None)
    finally:
        store.close()
    if report_id_prefix is not None:
        reports = tuple(r for r in reports if r.report_id.startswith(report_id_prefix))
    reports = _sort_digest_reports(reports, sort_by=sort_by, sort_direction=sort_direction)
    if top_n is not None and (top_n < 1 or top_n > 1000):
        raise click.BadParameter(f"--top {top_n} is out of range [1, 1000]")
    full_reports = reports  # aggregate is computed over the unsliced window
    sliced_reports = reports[:top_n] if top_n is not None else reports
    prefix_label = (
        f" (filtered by report-id prefix '{report_id_prefix}')" if report_id_prefix else ""
    )
    if as_json:
        _emit_digest_json(
            sliced_reports,
            full_reports=full_reports,
            window_label=window_label,
            cutoff=cutoff,
            report_id_prefix=report_id_prefix,
            top_n=top_n,
        )
        return
    if not full_reports:
        click.echo(f"No reports {window_label}{prefix_label}.")
        return
    n_full = len(full_reports)
    cycles_with_failures = sum(1 for r in full_reports if len(r.sections_failed) > 0)
    cycles_with_md = sum(1 for r in full_reports if r.markdown_path)
    cycles_with_html = sum(1 for r in full_reports if r.html_path)
    avg_sections_rendered = sum(len(r.sections_rendered) for r in full_reports) / n_full
    avg_terminal_chars = sum(len(r.rendered_terminal_text) for r in full_reports) / n_full
    click.echo(f"reports {window_label}{prefix_label}: {n_full}")
    click.echo(
        f"  cycles with failures: {cycles_with_failures}  "
        f"with markdown: {cycles_with_md}  "
        f"with html: {cycles_with_html}"
    )
    click.echo(
        f"  avg sections rendered: {avg_sections_rendered:.1f}  "
        f"avg terminal chars: {avg_terminal_chars:.0f}"
    )
    if top_n is not None and len(sliced_reports) < n_full:
        click.echo(
            f"  showing top {len(sliced_reports)} of {n_full} "
            f"(--top {top_n}, sorted by {sort_by} {sort_direction})"
        )
    click.echo("")
    for r in sliced_reports:
        markers: list[str] = []
        if r.markdown_path:
            markers.append("md")
        if r.html_path:
            markers.append("html")
        markers_str = f" [{', '.join(markers)}]" if markers else ""
        click.echo(
            f"{r.generated_at.isoformat()}  {r.report_id}  "
            f"sections={len(r.sections_rendered)}/{len(r.sections_enabled)}  "
            f"failed={len(r.sections_failed)}  "
            f"terminal_chars={len(r.rendered_terminal_text)}"
            f"{markers_str}"
        )


def _sort_digest_reports(
    reports: tuple[ReportRecord, ...],
    *,
    sort_by: str,
    sort_direction: str,
) -> tuple[ReportRecord, ...]:
    """Sort the digest report list by the requested field and direction.

    The default ordering (``generated_at`` desc) preserves the
    newest-first listing the existing digest output already used.
    Other supported fields:
    - ``sections_failed`` — sorts by ``len(r.sections_failed)``.
    - ``terminal_chars`` — sorts by ``len(r.rendered_terminal_text)``.

    For non-default sorts the secondary sort is always
    ``generated_at`` desc, so reports that tie on the primary
    field still appear newest-first.

    Strictly descriptive — sorting reports never ranks or
    recommends.
    """
    reverse = sort_direction.lower() == "desc"
    key = sort_by.lower()
    if key == "generated_at":
        return tuple(sorted(reports, key=lambda r: r.generated_at, reverse=reverse))
    if key == "sections_failed":
        return tuple(
            sorted(
                reports,
                key=lambda r: (len(r.sections_failed), r.generated_at),
                reverse=reverse,
            )
        )
    if key == "terminal_chars":
        return tuple(
            sorted(
                reports,
                key=lambda r: (len(r.rendered_terminal_text), r.generated_at),
                reverse=reverse,
            )
        )
    # click.Choice already validates this; defensive fallback.
    return reports


def _emit_digest_json(
    reports: tuple[ReportRecord, ...],
    *,
    window_label: str,
    cutoff: datetime,
    report_id_prefix: str | None = None,
    full_reports: tuple[ReportRecord, ...] | None = None,
    top_n: int | None = None,
) -> None:
    """Emit jsonlines digest output: per-report objects + aggregate.

    The per-report objects come from ``reports`` (which the
    caller may have already sliced via ``--top``). The aggregate
    object is always computed over ``full_reports`` (the full
    unsliced window) so the totals reflect the operator-defined
    selection regardless of whether ``--top`` is in effect.

    When ``full_reports`` is None, the caller hasn't sliced and
    ``reports`` is treated as the full set (backward compatible).
    """
    import json as _json

    aggregate_source: tuple[ReportRecord, ...] = (
        full_reports if full_reports is not None else reports
    )
    for r in reports:
        click.echo(
            _json.dumps(
                {
                    "kind": "report",
                    "report_id": r.report_id,
                    "generated_at": r.generated_at.isoformat(),
                    "sections_rendered": len(r.sections_rendered),
                    "sections_enabled": len(r.sections_enabled),
                    "sections_failed": len(r.sections_failed),
                    "terminal_chars": len(r.rendered_terminal_text),
                    "markdown_path": r.markdown_path,
                    "html_path": r.html_path,
                },
                sort_keys=True,
            )
        )
    n = len(aggregate_source)
    aggregate: dict[str, object]
    if n == 0:
        aggregate = {
            "kind": "aggregate",
            "window": window_label,
            "since": cutoff.isoformat(),
            "report_id_prefix": report_id_prefix,
            "report_count": 0,
            "cycles_with_failures": 0,
            "cycles_with_markdown": 0,
            "cycles_with_html": 0,
            "avg_sections_rendered": None,
            "avg_terminal_chars": None,
            "top_n": top_n,
            "top_n_emitted": len(reports) if top_n is not None else None,
        }
    else:
        aggregate = {
            "kind": "aggregate",
            "window": window_label,
            "since": cutoff.isoformat(),
            "report_id_prefix": report_id_prefix,
            "report_count": n,
            "cycles_with_failures": sum(1 for r in aggregate_source if len(r.sections_failed) > 0),
            "cycles_with_markdown": sum(1 for r in aggregate_source if r.markdown_path),
            "cycles_with_html": sum(1 for r in aggregate_source if r.html_path),
            "avg_sections_rendered": sum(len(r.sections_rendered) for r in aggregate_source) / n,
            "avg_terminal_chars": sum(len(r.rendered_terminal_text) for r in aggregate_source) / n,
            "top_n": top_n,
            "top_n_emitted": len(reports) if top_n is not None else None,
        }
    click.echo(_json.dumps(aggregate, sort_keys=True))


@report.command(name="watch")
@click.option(
    "--interval",
    "interval_seconds",
    default=3600,
    show_default=True,
    type=int,
    help=("Interval in seconds between cycles. Range [60, 86400] (1 minute to 24 hours)."),
)
@click.option(
    "--html",
    "html_path_str",
    default=None,
    help=(
        "Optional path to overwrite with the HTML rendering on each "
        "cycle. Browsers can keep the file open and refresh to see "
        "the latest cycle."
    ),
)
@click.option(
    "--markdown",
    "markdown_path_str",
    default=None,
    help="Optional path to overwrite with the markdown rendering on each cycle.",
)
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help=(
        "Run a single cycle and exit. Useful in tests; in normal "
        "operation watch loops until interrupted."
    ),
)
@click.option(
    "--max-cycles",
    default=None,
    type=int,
    help=(
        "Optional cap on the number of cycles before exit. "
        "Mostly useful in tests; production runs leave this unset."
    ),
)
@click.option(
    "--on-change",
    "on_change",
    is_flag=True,
    default=False,
    help=(
        "Skip generate() when upstream state hasn't changed since "
        "the prior cycle. Detection covers latest scan/comparison/"
        "follow-up/tuning-log IDs. The first cycle always runs."
    ),
)
@click.option(
    "--summary-file",
    "summary_file_str",
    default=None,
    help=(
        "Optional path to write the exit-summary block. Useful "
        "for cron-driven watch invocations where the operator "
        "wants to harvest the summary without parsing log files. "
        "When the path ends in '.json' the summary is written as "
        "a single JSON object; otherwise plain text matching the "
        "stdout format. The path may contain a '{timestamp}' "
        "placeholder; it expands to a UTC ISO 8601 timestamp "
        "(filesystem-safe — colons replaced with hyphens) so "
        "successive runs produce discrete files instead of "
        "overwriting one another."
    ),
)
@click.option(
    "--summary-retention",
    "summary_retention_days",
    default=None,
    type=int,
    help=(
        "Optional retention window in days. After writing the "
        "new summary file, older files in the same directory "
        "matching the same path pattern (suffix + '{timestamp}' "
        "placeholder, when used) are deleted. Only applies when "
        "--summary-file uses the '{timestamp}' placeholder. "
        "Range [1, 365]."
    ),
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def watch_cmd(
    interval_seconds: int,
    html_path_str: str | None,
    markdown_path_str: str | None,
    once: bool,
    max_cycles: int | None,
    on_change: bool,
    summary_file_str: str | None,
    summary_retention_days: int | None,
    db_path_str: str | None,
) -> None:
    """Run report-generate on a fixed cadence in a loop.

    Pure ergonomics: this is the same code path as
    ``razor-rooster report generate`` invoked once per interval.
    Each cycle overwrites the same ``--html``/``--markdown`` file
    so a browser tab pointed at the file can refresh to see the
    latest cycle.

    Loops until interrupted (Ctrl+C). ``--once`` runs a single
    cycle and exits; ``--max-cycles N`` exits after N cycles.
    ``--on-change`` skips ``generate()`` when upstream tables
    haven't changed since the prior cycle. When the loop resumes
    after one or more skipped cycles, it logs which fingerprint
    field(s) drove the resume.

    No new analytical surface — the loop calls the existing
    ``generate()`` engine. The same imperative-language linter
    runs on every cycle's output.
    """
    from razor_rooster.report_generator.engines.change_detection import (
        UpstreamFingerprint,
        compute_upstream_fingerprint,
    )

    if interval_seconds < 60 or interval_seconds > 86_400:
        raise click.BadParameter(f"--interval {interval_seconds} is out of range [60, 86400]")
    if summary_retention_days is not None:
        if summary_retention_days < 1 or summary_retention_days > 365:
            raise click.BadParameter(
                f"--summary-retention {summary_retention_days} is out of range [1, 365]"
            )
        if summary_file_str is None or "{timestamp}" not in summary_file_str:
            raise click.BadParameter(
                "--summary-retention requires --summary-file with the '{timestamp}' placeholder"
            )
    html_path = Path(html_path_str) if html_path_str else None
    markdown_path = Path(markdown_path_str) if markdown_path_str else None
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    cycles_run = 0
    cycles_failed = 0
    cycles_skipped = 0
    last_fingerprint: UpstreamFingerprint | None = None
    consecutive_skips = 0
    cycle_durations: list[float] = []
    distinct_changed_fields: set[str] = set()
    try:
        while True:
            cycle_start = datetime.now(tz=UTC)
            should_run = True
            changed_fields: list[str] = []
            if on_change:
                with store.connection() as conn:
                    current_fingerprint = compute_upstream_fingerprint(conn)
                if last_fingerprint is not None and current_fingerprint.is_same_as(
                    last_fingerprint
                ):
                    should_run = False
                else:
                    if last_fingerprint is not None:
                        changed_fields = _diff_fingerprint_fields(
                            last_fingerprint, current_fingerprint
                        )
                    last_fingerprint = current_fingerprint
            if should_run:
                resume_note = ""
                if consecutive_skips > 0 and changed_fields:
                    resume_note = (
                        f" (resume after {consecutive_skips} skipped: "
                        f"{', '.join(changed_fields)} changed)"
                    )
                if changed_fields:
                    distinct_changed_fields.update(changed_fields)
                click.echo(
                    f"[{cycle_start.isoformat()}] running report cycle "
                    f"(cycle {cycles_run + 1}){resume_note}..."
                )
                run_started = time.monotonic()
                try:
                    result = generate(
                        store,
                        markdown_path=markdown_path,
                        html_path=html_path,
                        quiet=True,
                    )
                    duration = time.monotonic() - run_started
                    cycle_durations.append(duration)
                    click.echo(
                        f"[{cycle_start.isoformat()}] report_id: {result.report_id}  "
                        f"sections rendered: {len(result.sections_rendered)}"
                    )
                except Exception:
                    duration = time.monotonic() - run_started
                    cycle_durations.append(duration)
                    cycles_failed += 1
                    logger.exception(
                        "report_generator: watch cycle failed; will retry on next interval"
                    )
                    click.echo(
                        f"[{cycle_start.isoformat()}] cycle failed; see logs",
                        err=True,
                    )
                cycles_run += 1
                consecutive_skips = 0
            else:
                click.echo(
                    f"[{cycle_start.isoformat()}] skipping cycle (--on-change: upstream unchanged)"
                )
                cycles_skipped += 1
                consecutive_skips += 1
            if once:
                break
            total_cycles = cycles_run + cycles_skipped
            if max_cycles is not None and total_cycles >= max_cycles:
                break
            try:
                time.sleep(interval_seconds)
            except KeyboardInterrupt:
                click.echo("Watch interrupted; exiting.")
                break
    finally:
        store.close()
    _emit_watch_exit_summary(
        cycles_run=cycles_run,
        cycles_skipped=cycles_skipped,
        cycles_failed=cycles_failed,
        cycle_durations=cycle_durations,
        distinct_changed_fields=distinct_changed_fields,
        interval_seconds=interval_seconds,
        summary_file=Path(summary_file_str) if summary_file_str else None,
        summary_template=summary_file_str,
        summary_retention_days=summary_retention_days,
    )


def _emit_watch_exit_summary(
    *,
    cycles_run: int,
    cycles_skipped: int,
    cycles_failed: int,
    cycle_durations: list[float],
    distinct_changed_fields: set[str],
    interval_seconds: int,
    summary_file: Path | None = None,
    summary_template: str | None = None,
    summary_retention_days: int | None = None,
) -> None:
    """Print the multi-line watch-loop exit summary.

    Strictly descriptive — the summary reports observed activity
    across the loop, never ranks or recommends.

    When ``summary_file`` is set, also writes the summary to the
    given path. Paths ending in ``.json`` get a single JSON
    object; other paths get plain text matching the stdout
    format. The path may contain a literal ``{timestamp}``
    placeholder; it expands to a UTC ISO 8601 timestamp with
    colons replaced by hyphens so successive runs produce
    discrete files (filesystem-safe across macOS / Linux /
    Windows).

    When ``summary_retention_days`` is set (and the path uses
    the ``{timestamp}`` placeholder), summaries older than that
    many days in the same directory and matching the same
    filename pattern are pruned after the new file is written.
    """
    skipped_str = f" ({cycles_skipped} skipped)" if cycles_skipped else ""
    summary_lines: list[str] = []
    summary_lines.append(f"Watch exited after {cycles_run} cycle(s){skipped_str}.")
    avg_duration: float | None = None
    if cycle_durations:
        avg_duration = sum(cycle_durations) / len(cycle_durations)
        summary_lines.append(
            f"  cycles failed: {cycles_failed}  avg cycle duration: {avg_duration:.3f}s"
        )
    if distinct_changed_fields:
        summary_lines.append(
            f"  fingerprint fields changed during loop: "
            f"{', '.join(sorted(distinct_changed_fields))}"
        )
    if cycles_skipped:
        total_skip_seconds = cycles_skipped * interval_seconds
        summary_lines.append(
            f"  total skip time: ~{total_skip_seconds}s "
            f"({cycles_skipped} cycle(s) x {interval_seconds}s interval)"
        )
    for line in summary_lines:
        click.echo(line)
    if summary_file is not None:
        resolved = _resolve_summary_path(summary_file)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if resolved.suffix == ".json":
            import json as _json

            payload = {
                "kind": "watch_summary",
                "cycles_run": cycles_run,
                "cycles_skipped": cycles_skipped,
                "cycles_failed": cycles_failed,
                "avg_cycle_duration_seconds": avg_duration,
                "fingerprint_fields_changed": sorted(distinct_changed_fields),
                "total_skip_seconds": (cycles_skipped * interval_seconds if cycles_skipped else 0),
                "interval_seconds": interval_seconds,
            }
            resolved.write_text(_json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        else:
            resolved.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
        if str(resolved) != str(summary_file):
            click.echo(f"summary written to: {resolved}")
        if summary_retention_days is not None and summary_template is not None:
            removed = _prune_old_summaries(
                template=summary_template,
                retention_days=summary_retention_days,
                keep_path=resolved,
            )
            if removed:
                click.echo(
                    f"summary retention: pruned {removed} file(s) older than "
                    f"{summary_retention_days} day(s)"
                )


def _prune_old_summaries(
    *,
    template: str,
    retention_days: int,
    keep_path: Path,
) -> int:
    """Delete summary files older than ``retention_days`` matching the template.

    The template is the literal ``--summary-file`` value (e.g.
    ``logs/watch-{timestamp}.json``). The directory is the
    parent of the template; the filename glob substitutes ``*``
    for ``{timestamp}`` so files emitted by prior runs match.

    The currently-just-written file (``keep_path``) is never
    pruned, regardless of its age.

    Strictly descriptive — pruning never alters analytical
    state; it only manages disk usage.
    """
    template_path = Path(template)
    parent = template_path.parent
    pattern = template_path.name.replace("{timestamp}", "*")
    if not parent.exists():
        return 0
    cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)
    cutoff_ts = cutoff.timestamp()
    removed = 0
    for path in parent.glob(pattern):
        if not path.is_file():
            continue
        if path.resolve() == keep_path.resolve():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_ts:
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.exception(
                    "report_generator: failed to prune summary file %s; continuing", path
                )
    return removed


def _resolve_summary_path(summary_file: Path) -> Path:
    """Expand ``{timestamp}`` in a summary-file path.

    The placeholder is filesystem-safe: ``2026-05-16T14-30-00+00-00``
    (colons replaced with hyphens). Paths without the placeholder
    are returned unchanged so existing operator scripts keep
    working.
    """
    raw = str(summary_file)
    if "{timestamp}" not in raw:
        return summary_file
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    safe = now.replace(":", "-")
    return Path(raw.replace("{timestamp}", safe))


def _diff_fingerprint_fields(
    prior: object,
    current: object,
) -> list[str]:
    """Return the list of fingerprint field names whose values changed.

    Reads the four documented fields off ``UpstreamFingerprint``
    and emits short human-readable labels (``scan``, ``comparison``,
    ``follow_up``, ``tuning_log``).
    """
    fields = (
        ("latest_scan_id", "scan"),
        ("latest_comparison_id", "comparison"),
        ("latest_follow_up_id", "follow_up"),
        ("latest_tuning_log_id", "tuning_log"),
    )
    out: list[str] = []
    for attr, label in fields:
        if getattr(prior, attr, None) != getattr(current, attr, None):
            out.append(label)
    return out


@report.command(name="measurements")
@click.option(
    "--kind",
    default="cross_venue_spread_bps",
    help=(
        "Measurement kind to inspect. Default is "
        "'cross_venue_spread_bps'. Other shipped kinds: "
        "'single_venue_dominance_share' (max venue's share of "
        "combined 24h volume per multi-venue class) and "
        "'brier_per_sector' (per-sector rolling Brier score)."
    ),
)
@click.option(
    "--since",
    default=None,
    help="Only show measurements at or after this ISO 8601 timestamp.",
)
@click.option(
    "--limit",
    default=20,
    show_default=True,
    help="Maximum rows to display.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Print the raw distribution JSON for each measurement.",
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def measurements_cmd(
    kind: str,
    since: str | None,
    limit: int,
    as_json: bool,
    db_path_str: str | None,
) -> None:
    """Show per-cycle threshold-distribution measurements.

    Helps the operator decide whether their configured thresholds
    (in ``config/report.yaml``) are well-calibrated for the corpus.
    For ``cross_venue_spread_bps`` the renderer prints the n,
    n_above_threshold, configured threshold, and the percentile
    spread per cycle so the operator can compare their threshold
    to the empirical distribution over time.
    """
    since_dt: datetime | None = None
    if since is not None:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError as exc:
            raise click.BadParameter(f"invalid --since: {exc}") from exc
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            rows = list_threshold_measurements(
                conn,
                measurement_kind=kind,
                since=since_dt,
                limit=limit,
            )
    finally:
        store.close()
    if not rows:
        click.echo(f"No '{kind}' measurements yet.")
        return
    import json as _json

    for record in rows:
        if as_json:
            click.echo(
                _json.dumps(
                    {
                        "report_id": record.report_id,
                        "measurement_kind": record.measurement_kind,
                        "measured_at": record.measured_at.isoformat(),
                        "n_observations": record.n_observations,
                        "n_above_threshold": record.n_above_threshold,
                        "configured_threshold": record.configured_threshold,
                        "distribution": record.distribution,
                    },
                    sort_keys=True,
                )
            )
            continue
        timestamp = record.measured_at.isoformat()
        click.echo(f"{timestamp}  report={record.report_id}  kind={record.measurement_kind}")
        click.echo(
            f"  n={record.n_observations}  "
            f"above_threshold={record.n_above_threshold}  "
            f"threshold={record.configured_threshold:g}"
        )
        dist = record.distribution
        if not dist:
            click.echo("  (no distribution payload)")
            continue
        if dist.get("n", 0) == 0:
            click.echo("  (zero observations this cycle)")
            continue
        min_v = dist.get("min")
        max_v = dist.get("max")
        mean_v = dist.get("mean")
        stddev = dist.get("stddev")
        click.echo(
            f"  min={_fmt(min_v)}  max={_fmt(max_v)}  mean={_fmt(mean_v)}  stddev={_fmt(stddev)}"
        )
        percentiles = dist.get("percentiles") or {}
        if isinstance(percentiles, dict):
            parts = []
            for q in sorted(percentiles):
                v = percentiles[q]
                parts.append(f"p{q.replace('0.', '')}={_fmt(v)}")
            if parts:
                click.echo("  " + "  ".join(parts))


@report.command(name="prune-measurements")
@click.option(
    "--before",
    "before_iso",
    default=None,
    help="ISO 8601 timestamp; delete measurements older than this point.",
)
@click.option(
    "--keep-last",
    default=None,
    type=int,
    help=(
        "Keep only the N most recent measurements per kind. "
        "Older rows are deleted. Pass 0 to delete every row "
        "for the targeted kind."
    ),
)
@click.option(
    "--kind",
    default=None,
    help=("Optional measurement kind to scope the prune to. Without it, all kinds are considered."),
)
@click.option(
    "--confirm",
    is_flag=True,
    default=False,
    help="Required to actually delete; without it the command refuses.",
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def prune_measurements_cmd(
    before_iso: str | None,
    keep_last: int | None,
    kind: str | None,
    confirm: bool,
    db_path_str: str | None,
) -> None:
    """Delete old ``report_threshold_measurements`` rows.

    Default retention is unbounded — the table is small per cycle
    so most operators can leave it alone. Run this when disk
    pressure becomes a concern, or when you want to reset the
    measurement history before a tuning cycle.

    Either ``--before`` or ``--keep-last`` (or both) must be set.
    The two strategies stack: rows are deleted when *either*
    condition fires.

    Refuses without ``--confirm``.
    """
    from razor_rooster.report_generator.persistence.operations import (
        PruneConfirmationError,
        prune_threshold_measurements,
    )

    if before_iso is None and keep_last is None:
        click.echo(
            "refusing to prune without --before or --keep-last",
            err=True,
        )
        raise click.exceptions.Exit(code=2)
    if not confirm:
        click.echo("refusing to prune without --confirm", err=True)
        raise click.exceptions.Exit(code=2)
    cutoff: datetime | None = None
    if before_iso is not None:
        try:
            cutoff = datetime.fromisoformat(before_iso)
        except ValueError as exc:
            raise click.BadParameter(f"invalid --before: {exc}") from exc
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            try:
                deleted = prune_threshold_measurements(
                    conn,
                    before=cutoff,
                    keep_last=keep_last,
                    measurement_kind=kind,
                    confirm=True,
                )
            except (PruneConfirmationError, ValueError) as exc:
                click.echo(str(exc), err=True)
                raise click.exceptions.Exit(code=2) from exc
    finally:
        store.close()
    parts: list[str] = []
    if cutoff is not None:
        parts.append(f"older than {cutoff.isoformat()}")
    if keep_last is not None:
        parts.append(f"beyond the newest {keep_last} per kind")
    scope = "; ".join(parts) if parts else "matching no criteria"
    kind_str = f" for kind '{kind}'" if kind else ""
    click.echo(f"pruned {deleted} measurement row(s){kind_str} ({scope})")


@report.command(name="tuning-log")
@click.option(
    "--kind",
    default=None,
    help="Optional measurement kind to scope the log to.",
)
@click.option(
    "--since",
    default=None,
    help="Only show entries at or after this ISO 8601 timestamp.",
)
@click.option(
    "--limit",
    default=20,
    show_default=True,
    help="Maximum rows to display.",
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def tuning_log_cmd(
    kind: str | None,
    since: str | None,
    limit: int,
    db_path_str: str | None,
) -> None:
    """Show historical ``threshold_tuning_log`` entries.

    One row per successful
    ``razor-rooster report suggest-thresholds --apply`` write.
    Useful for retroactive review of how thresholds drifted —
    you can see what was changed, when, by what amount, and
    where the backup file lives.
    """
    from razor_rooster.report_generator.persistence.operations import (
        list_tuning_log_entries,
    )

    since_dt: datetime | None = None
    if since is not None:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError as exc:
            raise click.BadParameter(f"invalid --since: {exc}") from exc
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)
    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            rows = list_tuning_log_entries(
                conn,
                measurement_kind=kind,
                since=since_dt,
                limit=limit,
            )
    finally:
        store.close()
    if not rows:
        click.echo("No tuning-log entries yet.")
        return
    for entry in rows:
        click.echo(
            f"{entry.applied_at.isoformat()}  kind={entry.measurement_kind}  knob={entry.knob}"
        )
        click.echo(
            f"  previous: {_fmt(entry.previous_value)}  "
            f"new: {_fmt(entry.new_value)}"
            + (
                f"  target: p{round(entry.target_percentile * 100)}"
                if entry.target_percentile is not None
                else ""
            )
        )
        if entry.backup_path:
            click.echo(f"  backup: {entry.backup_path}")
        if entry.note:
            click.echo(f"  note: {entry.note}")


@report.command(name="tuning-log-undo")
@click.argument("log_id")
@click.option(
    "--yes",
    "auto_confirm",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt.",
)
@click.option(
    "--config",
    "config_path_str",
    default=None,
    help=(
        "Path to config/report.yaml to restore. Defaults to "
        "config/report.yaml in the workspace root."
    ),
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def tuning_log_undo_cmd(
    log_id: str,
    auto_confirm: bool,
    config_path_str: str | None,
    db_path_str: str | None,
) -> None:
    """Restore config/report.yaml from a tuning-log entry's backup.

    The flow is symmetric with ``--apply``:
    1. The current config is copied to a fresh
       ``.bak.<timestamp>`` so the undo itself is reversible.
    2. The historical backup recorded in the tuning-log entry
       is copied over the live config.
    3. A new tuning-log entry is recorded describing the undo.

    Refuses if the tuning-log entry is missing a ``backup_path``,
    if the backup file no longer exists on disk, or if the live
    config doesn't exist.

    Strictly descriptive prompt phrasing.
    """
    from razor_rooster.report_generator.engines.suggestions import (
        ApplyError,
        undo_tuning_log_entry,
    )
    from razor_rooster.report_generator.persistence.operations import (
        get_tuning_log_entry,
        persist_tuning_log_entry,
    )

    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            entry = get_tuning_log_entry(conn, log_id=log_id)
            if entry is None:
                click.echo(
                    f"No tuning-log entry found with log_id={log_id!r}.",
                    err=True,
                )
                raise click.exceptions.Exit(code=2)
            if entry.backup_path is None:
                click.echo(
                    f"Tuning-log entry {log_id!r} has no backup_path recorded; cannot undo.",
                    err=True,
                )
                raise click.exceptions.Exit(code=2)
            config_path = (
                Path(config_path_str) if config_path_str else Path("config") / "report.yaml"
            )
            backup_path = Path(entry.backup_path)
            click.echo(f"Undo tuning-log entry {log_id!r}?")
            click.echo(
                f"  applied_at: {entry.applied_at.isoformat()}  "
                f"kind: {entry.measurement_kind}  knob: {entry.knob}"
            )
            click.echo(
                f"  current value will be restored to "
                f"{_fmt(entry.previous_value)} "
                f"(was {_fmt(entry.new_value)} after the apply)."
            )
            click.echo(f"  source backup: {backup_path}")
            if not auto_confirm and not click.confirm(
                "  Proceed with this undo? A timestamped backup of the "
                "current config will be written first.",
                default=False,
            ):
                click.echo("Skipped (no change applied).")
                return
            try:
                result = undo_tuning_log_entry(
                    config_path=config_path,
                    backup_path=backup_path,
                    log_id=log_id,
                )
            except ApplyError as exc:
                click.echo(f"Refused: {exc}", err=True)
                raise click.exceptions.Exit(code=2) from exc
            click.echo(
                f"Undone. {result.config_path} restored from "
                f"{result.restored_from}. "
                f"Pre-undo backup saved to {result.current_backup_path}."
            )
            try:
                persist_tuning_log_entry(
                    conn,
                    log_id=str(uuid.uuid4()),
                    applied_at=datetime.now(tz=UTC),
                    measurement_kind=entry.measurement_kind,
                    knob=entry.knob,
                    previous_value=entry.new_value,
                    new_value=(
                        float(entry.previous_value) if entry.previous_value is not None else 0.0
                    ),
                    target_percentile=entry.target_percentile,
                    backup_path=str(result.current_backup_path),
                    note=(
                        f"undo of log_id={log_id} "
                        + (
                            "(restored from a backup whose pre-apply value was "
                            "unknown; the new_value column shows the post-undo "
                            "previous_value of the original apply)"
                            if entry.previous_value is None
                            else f"(restored {entry.knob} from {entry.new_value} → {entry.previous_value})"
                        )
                    ),
                )
            except Exception:
                logger.exception(
                    "report_generator: tuning-log persistence failed for "
                    "the undo entry; the config restore itself succeeded"
                )
    finally:
        store.close()


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, int | float):
        return f"{float(value):.4g}"
    return str(value)


@report.command(name="explain-thresholds")
@click.option(
    "--kind",
    default=None,
    help=(
        "Measurement kind to explain. Default: explain every shipped kind. "
        "Pass a single kind name to scope the output."
    ),
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def explain_thresholds_cmd(kind: str | None, db_path_str: str | None) -> None:
    """Show where each configured threshold sits in the latest measurement distribution.

    Reads the most recent ``report_threshold_measurements`` row per
    kind, computes the percentile rank of the configured threshold
    within that distribution, and prints a descriptive summary so
    the operator can see whether the section will be quiet or
    crowded under the current setup.

    Strictly descriptive. The output never tells the operator to
    raise or lower a threshold; it only reports where they sit.
    """
    from razor_rooster.report_generator.engines.measurements import (
        SHIPPED_MEASUREMENT_KINDS,
        threshold_percentile_rank,
    )

    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            requested_kinds: tuple[str, ...] = (
                (kind,) if kind is not None else SHIPPED_MEASUREMENT_KINDS
            )
            for measurement_kind in requested_kinds:
                latest = list_threshold_measurements(
                    conn,
                    measurement_kind=measurement_kind,
                    limit=1,
                )
                click.echo(f"kind: {measurement_kind}")
                if not latest:
                    click.echo(f"  no '{measurement_kind}' measurements yet.")
                    click.echo("")
                    continue
                record = latest[0]
                click.echo(
                    f"  latest cycle: {record.measured_at.isoformat()}  report={record.report_id}"
                )
                click.echo(f"  configured threshold: {_fmt(record.configured_threshold)}")
                click.echo(
                    f"  observations this cycle: n={record.n_observations}, "
                    f"above_threshold={record.n_above_threshold}"
                )
                if record.n_observations == 0:
                    click.echo("  empirical distribution: (no observations this cycle)")
                    click.echo("")
                    continue
                rank = threshold_percentile_rank(record.distribution)
                if rank is None:
                    click.echo(
                        "  empirical distribution: (no percentile data; cannot place threshold)"
                    )
                    click.echo("")
                    continue
                rank_pct = round(rank * 100)
                if rank_pct == 0:
                    note = (
                        "the configured threshold sits below the bottom of the "
                        "recorded distribution"
                    )
                elif rank_pct == 100:
                    note = (
                        "the configured threshold sits at or above the top of "
                        "the recorded distribution"
                    )
                else:
                    note = (
                        f"the configured threshold sits at the p{rank_pct} of "
                        "this cycle's distribution"
                    )
                click.echo(f"  percentile rank: {note}")
                # Echo a few key percentiles so the operator can see the shape.
                percentiles = record.distribution.get("percentiles") or {}
                if isinstance(percentiles, dict) and percentiles:
                    parts = []
                    for q in sorted(percentiles):
                        v = percentiles[q]
                        parts.append(f"p{q.replace('0.', '')}={_fmt(v)}")
                    click.echo("  percentiles: " + "  ".join(parts))
                click.echo("")
    finally:
        store.close()


@report.command(name="suggest-thresholds")
@click.option(
    "--kind",
    default=None,
    help=(
        "Measurement kind to suggest thresholds for. Default: every shipped kind. "
        "Required when --apply is set."
    ),
)
@click.option(
    "--lookback-cycles",
    default=30,
    show_default=True,
    help="Number of recent cycles to read (most recent first).",
)
@click.option(
    "--target-pct",
    "target_pcts",
    multiple=True,
    default=None,
    help=(
        "Target percentile(s) to suggest threshold values for. "
        "Repeatable. Defaults to 0.50, 0.70, 0.90. With --apply, "
        "exactly one --target-pct must be supplied."
    ),
)
@click.option(
    "--apply",
    "apply_flag",
    is_flag=True,
    default=False,
    help=(
        "Write the suggested value back to config/report.yaml. "
        "Requires --kind and exactly one --target-pct. Saves a "
        "timestamped backup of the existing config before "
        "writing. Prompts for confirmation unless --yes is set."
    ),
)
@click.option(
    "--diff",
    "show_diff",
    is_flag=True,
    default=False,
    help=(
        "When --apply is set, print a unified-diff-style preview "
        "of the YAML change before the confirmation prompt."
    ),
)
@click.option(
    "--yes",
    "auto_confirm",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt when --apply is set.",
)
@click.option(
    "--config",
    "config_path_str",
    default=None,
    help=(
        "Path to config/report.yaml when --apply is set. "
        "Defaults to config/report.yaml in the workspace root."
    ),
)
@click.option(
    "--note",
    "operator_note",
    default=None,
    help=(
        "Optional free-text note to attach to the tuning-log entry "
        "when --apply is set. Useful for retroactive review."
    ),
)
@click.option(
    "--db",
    "db_path_str",
    default=None,
    help="Path to the DuckDB store.",
)
def suggest_thresholds_cmd(
    kind: str | None,
    lookback_cycles: int,
    target_pcts: tuple[str, ...] | None,
    apply_flag: bool,
    show_diff: bool,
    auto_confirm: bool,
    config_path_str: str | None,
    operator_note: str | None,
    db_path_str: str | None,
) -> None:
    """Suggest threshold values for each shipped measurement kind.

    For each kind, reads the most recent measurements, averages the
    recorded percentile cuts across cycles, and emits one suggested
    value per target percentile so the operator can see what
    threshold value would land at, say, the p70 of their corpus's
    distribution.

    Read path: strictly descriptive. The output reports what each
    percentile cut would mean if the operator chose to use it.

    Write path (``--apply``): reversible config edit. The CLI saves
    a timestamped backup of ``config/report.yaml`` before writing,
    prompts for confirmation, and refuses postures that would
    silence guard rails (e.g. ``--target-pct 1.0`` for the
    dominance threshold). Operators who want that posture edit the
    YAML by hand.
    """
    from razor_rooster.report_generator.engines.measurements import (
        SHIPPED_MEASUREMENT_KINDS,
    )
    from razor_rooster.report_generator.engines.suggestions import (
        DEFAULT_TARGET_PERCENTILES,
        ApplyError,
        apply_threshold_suggestion,
        compute_apply_diff,
        suggest_thresholds,
    )

    parsed_targets: tuple[float, ...]
    if not target_pcts:
        parsed_targets = DEFAULT_TARGET_PERCENTILES
    else:
        try:
            parsed_targets = tuple(float(t) for t in target_pcts)
        except (TypeError, ValueError) as exc:
            raise click.BadParameter(f"invalid --target-pct: {exc}") from exc
        for t in parsed_targets:
            if not 0.0 <= t <= 1.0:
                raise click.BadParameter(f"--target-pct {t} is out of range [0.0, 1.0]")

    if apply_flag:
        if kind is None:
            raise click.BadParameter("--apply requires --kind (one kind at a time)")
        if len(parsed_targets) != 1:
            raise click.BadParameter("--apply requires exactly one --target-pct")
    if show_diff and not apply_flag:
        raise click.BadParameter("--diff is only meaningful with --apply")

    requested_kinds: tuple[str, ...] = (kind,) if kind is not None else SHIPPED_MEASUREMENT_KINDS

    db_path = _resolve_db_path(db_path_str)
    store = _open_store(db_path)
    try:
        with store.connection() as conn:
            for measurement_kind in requested_kinds:
                report_obj = suggest_thresholds(
                    conn,
                    measurement_kind=measurement_kind,
                    lookback_cycles=lookback_cycles,
                    target_percentiles=parsed_targets,
                )
                click.echo(f"kind: {measurement_kind}")
                click.echo(
                    f"  cycles inspected: {report_obj.cycles_inspected}  "
                    f"cycles with data: {report_obj.cycles_with_data}"
                )
                if report_obj.current_threshold is not None:
                    click.echo(
                        f"  current configured threshold: {_fmt(report_obj.current_threshold)}"
                    )
                if report_obj.stability_cv is not None:
                    if report_obj.unstable:
                        click.echo(
                            f"  stability: cv={_fmt(report_obj.stability_cv)} "
                            "(unstable; percentile cuts vary widely "
                            "cycle-to-cycle, suggestion is noisy)"
                        )
                    else:
                        click.echo(
                            f"  stability: cv={_fmt(report_obj.stability_cv)} "
                            "(stable; percentile cuts are consistent "
                            "across cycles)"
                        )
                if not report_obj.suggestions:
                    click.echo("  not enough data to suggest thresholds yet.")
                    click.echo("")
                    continue
                click.echo("  percentile-target → suggested value:")
                for s in report_obj.suggestions:
                    target_pct_int = round(s.target_percentile * 100)
                    click.echo(f"    p{target_pct_int}: {_fmt(s.suggested_value)}")
                click.echo("")

                if not apply_flag:
                    continue
                # --apply path: exactly one suggestion was emitted because
                # we required exactly one --target-pct above.
                suggestion = report_obj.suggestions[0]
                config_path = (
                    Path(config_path_str) if config_path_str else Path("config") / "report.yaml"
                )
                target_pct_int = round(suggestion.target_percentile * 100)
                click.echo(
                    "Apply suggested value "
                    f"{_fmt(suggestion.suggested_value)} "
                    f"(at p{target_pct_int}) to thresholds for "
                    f"'{measurement_kind}'?"
                )
                click.echo(
                    f"  config: {config_path}  "
                    f"current: {_fmt(report_obj.current_threshold)}  "
                    f"new: {_fmt(suggestion.suggested_value)}"
                )
                if report_obj.unstable:
                    click.echo(
                        "  note: the underlying distribution is "
                        "flagged as unstable for the lookback window; "
                        "percentile cuts vary widely cycle-to-cycle "
                        "and this suggestion is noisier than usual."
                    )
                if show_diff:
                    click.echo("")
                    click.echo(
                        compute_apply_diff(
                            config_path=config_path,
                            measurement_kind=measurement_kind,
                            new_value=suggestion.suggested_value,
                        )
                    )
                    click.echo("")
                if not auto_confirm and not click.confirm(
                    "  Proceed with this change? A timestamped backup will be written first.",
                    default=False,
                ):
                    click.echo("Skipped (no change applied).")
                    return
                try:
                    result = apply_threshold_suggestion(
                        config_path=config_path,
                        measurement_kind=measurement_kind,
                        new_value=suggestion.suggested_value,
                        target_percentile=suggestion.target_percentile,
                    )
                except ApplyError as exc:
                    click.echo(f"Refused: {exc}", err=True)
                    raise click.exceptions.Exit(code=2) from exc
                click.echo(
                    f"Applied. thresholds.{result.knob} now "
                    f"{_fmt(result.new_value)} (was "
                    f"{_fmt(result.previous_value)}). "
                    f"Backup saved to {result.backup_path}."
                )
                # Record the apply in the tuning log so retroactive
                # review is possible (T-RG-COMPAT-TUNINGLOG-001).
                # Best-effort: a log-write failure is logged but
                # doesn't undo the apply.
                from razor_rooster.report_generator.persistence.operations import (
                    persist_tuning_log_entry,
                )

                try:
                    persist_tuning_log_entry(
                        conn,
                        log_id=str(uuid.uuid4()),
                        applied_at=datetime.now(tz=UTC),
                        measurement_kind=measurement_kind,
                        knob=result.knob,
                        previous_value=result.previous_value,
                        new_value=result.new_value,
                        target_percentile=suggestion.target_percentile,
                        backup_path=str(result.backup_path),
                        note=operator_note,
                    )
                except Exception:
                    logger.exception(
                        "report_generator: tuning-log persistence failed; "
                        "the config write itself succeeded"
                    )
    finally:
        store.close()
