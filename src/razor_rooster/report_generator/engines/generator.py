"""Generator orchestrator (T-RG-040; design §3.4).

Reads upstream tables, calls each section assembler with per-section
failure isolation, renders terminal + optional markdown, applies the
shared imperative-language linter, and persists the result to
``report_log``.

Per REQ-RG-EXEC-003 a failed section produces a "section
unavailable" placeholder rather than crashing the whole report.
Per REQ-RG-FRAME-001 / REQ-RG-FRAME-004 a linter rejection raises
:class:`razor_rooster.position_engine.frame.linter.ImperativeLanguageDetected`
and the report is **not** persisted, so the next run will re-attempt.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.position_engine.frame.linter import check_text
from razor_rooster.report_generator.config.loader import (
    ALL_SECTIONS,
    ReportConfig,
    load_config,
)
from razor_rooster.report_generator.engines.measurements import (
    MEASUREMENT_KIND_BRIER_PER_SECTOR,
    MEASUREMENT_KIND_CROSS_VENUE_SPREAD_BPS,
    MEASUREMENT_KIND_SINGLE_VENUE_DOMINANCE_SHARE,
    brier_per_sector_observations,
    compute_distribution,
    cross_venue_spread_observations,
    single_venue_dominance_observations,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    at_a_glance as at_a_glance_assembler,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    calibration as calibration_assembler,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    cross_venue as cross_venue_assembler,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    footer as footer_assembler,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    header as header_assembler,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    recent_tuning as recent_tuning_assembler,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    reliability as reliability_assembler,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    surfaced as surfaced_assembler,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    system_health as system_health_assembler,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    watched as watched_assembler,
)
from razor_rooster.report_generator.engines.section_assemblers import (
    watchlist as watchlist_assembler,
)
from razor_rooster.report_generator.models import (
    ReportRecord,
    ReportResult,
    SectionContent,
)
from razor_rooster.report_generator.persistence.operations import (
    persist_report,
    persist_threshold_measurement,
    prune_threshold_measurements,
    query_last_report,
)
from razor_rooster.report_generator.renderer import html as html_renderer
from razor_rooster.report_generator.renderer import markdown, terminal
from razor_rooster.report_generator.renderer.shared import (
    disclaimer_version_hash,
)

logger = logging.getLogger(__name__)


def generate(
    store: DuckDBStore,
    *,
    since: datetime | None = None,
    markdown_path: Path | None = None,
    html_path: Path | None = None,
    quiet: bool = False,
    config: ReportConfig | None = None,
    system_version: str = "0.1.0",
    library_version: int = 1,
    library_age_days: int | None = None,
    now: datetime | None = None,
) -> ReportResult:
    """Generate one report covering ``since`` → ``now``.

    Args:
        store: DuckDB store (already migrated for all subsystems).
        since: lower bound of the cycle window. Defaults to the prior
            report's ``generated_at`` if any, otherwise 24 hours ago.
        markdown_path: optional path to write a parallel markdown
            file. ``None`` skips markdown generation.
        html_path: optional path to write a parallel HTML file
            (T-RG-COMPAT-HTML-001 v0.44.0). ``None`` skips HTML
            generation.
        quiet: when ``True`` the terminal text is generated but not
            printed; the operator gets the markdown file.
        config: overrides the loaded config (test-injection).
        system_version: stamped in header + footer.
        library_version: stamped in header + report_log.
        library_age_days: optional pre-computed library age for the
            header (avoids the assembler having to recompute it).
        now: override the wall clock for testing.

    Returns:
        :class:`ReportResult` with the assembled outputs and metadata.
    """
    started = now or datetime.now(tz=UTC)
    cfg = config or load_config()
    report_id = str(uuid.uuid4())

    # Resolve the cycle window.
    since_ts = _resolve_since(store, since=since, now=started)
    until_ts = started

    # Load disclaimer text up front so the version hash is stable.
    disclaimer_text = footer_assembler.load_disclaimer_text()
    disclaimer_hash = disclaimer_version_hash(disclaimer_text)

    # Determine which body sections are enabled (configured) and which
    # are disabled (so the header can note the difference).
    enabled = tuple(s for s in cfg.enabled_sections if s in ALL_SECTIONS)
    disabled = tuple(s for s in ALL_SECTIONS if s not in enabled)

    # Header + footer first; their content is needed by the renderers.
    with store.connection() as conn:
        header_content = header_assembler.assemble(
            conn,
            report_id=report_id,
            since_ts=since_ts,
            until_ts=until_ts,
            library_version=library_version,
            library_age_days=library_age_days,
            disabled_sections=disabled,
        )
    footer_content = footer_assembler.assemble(
        report_id=report_id,
        system_version=system_version,
        completed_at=started,
    )

    # Body sections — per-section failure isolation.
    # ``at_a_glance`` is special: it depends on the other sections'
    # already-assembled content. We skip it in the first pass, then
    # fill it in after the loop completes.
    body_contents: list[SectionContent] = []
    sections_failed: list[dict[str, str]] = []
    at_a_glance_index: int | None = None
    for index, section_name in enumerate(enabled):
        if section_name == "at_a_glance":
            # Placeholder; we'll fill the content in after the loop.
            body_contents.append(SectionContent(name="at_a_glance", content=None))
            at_a_glance_index = index
            continue
        try:
            with store.connection() as conn:
                content = _assemble_section(
                    conn,
                    section_name=section_name,
                    since_ts=since_ts,
                    until_ts=until_ts,
                    cfg=cfg,
                )
            body_contents.append(SectionContent(name=section_name, content=content))
        except Exception as exc:
            logger.exception("report_generator: section %s failed", section_name)
            error_str = f"{type(exc).__name__}: {exc}"
            body_contents.append(SectionContent(name=section_name, content=None, error=error_str))
            sections_failed.append({"section": section_name, "error": error_str})

    # Second pass: fill the at_a_glance section using the assembled
    # content of the others. Best-effort isolation — a glance failure
    # leaves a placeholder.
    if at_a_glance_index is not None:
        try:
            content_by_name: dict[str, Mapping[str, Any] | None] = {
                s.name: s.content for s in body_contents
            }
            glance_content = at_a_glance_assembler.assemble(content_by_name)
            body_contents[at_a_glance_index] = SectionContent(
                name="at_a_glance", content=glance_content
            )
        except Exception as exc:
            logger.exception("report_generator: at_a_glance assembly failed")
            error_str = f"{type(exc).__name__}: {exc}"
            body_contents[at_a_glance_index] = SectionContent(
                name="at_a_glance", content=None, error=error_str
            )
            sections_failed.append({"section": "at_a_glance", "error": error_str})

    # Render. The linter runs on each output independently before any
    # persistence — REQ-RG-FRAME-001 / REQ-RG-FRAME-004.
    terminal_text = terminal.render(
        header=header_content,
        body_sections=tuple(body_contents),
        footer=footer_content,
    )
    check_text(terminal_text)

    markdown_text: str | None = None
    written_markdown_path: str | None = None
    if markdown_path is not None:
        md_text = markdown.render(
            header=header_content,
            body_sections=tuple(body_contents),
            footer=footer_content,
        )
        check_text(md_text)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(md_text, encoding="utf-8")
        markdown_text = md_text
        written_markdown_path = str(markdown_path)

    html_text: str | None = None
    written_html_path: str | None = None
    if html_path is not None:
        rendered_html = html_renderer.render(
            header=header_content,
            body_sections=tuple(body_contents),
            footer=footer_content,
        )
        check_text(rendered_html)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(rendered_html, encoding="utf-8")
        html_text = rendered_html
        written_html_path = str(html_path)

    if not quiet:
        print(terminal_text)

    completed = datetime.now(tz=UTC)
    duration = (completed - started).total_seconds()

    sections_rendered = tuple(s.name for s in body_contents if s.ok)

    record = ReportRecord(
        report_id=report_id,
        generated_at=completed,
        since_ts=since_ts,
        until_ts=until_ts,
        sections_enabled=enabled,
        sections_rendered=sections_rendered,
        sections_failed=tuple(sections_failed),
        library_version=library_version,
        disclaimer_version_hash=disclaimer_hash,
        rendered_terminal_text=terminal_text,
        rendered_markdown_text=markdown_text,
        markdown_path=written_markdown_path,
        rendered_html_text=html_text,
        html_path=written_html_path,
        duration_seconds=duration,
    )
    with store.connection() as conn:
        persist_report(conn, record)
        _persist_threshold_measurements(
            conn,
            report_id=report_id,
            measured_at=completed,
            body_contents=body_contents,
            cfg=cfg,
        )
        _maybe_auto_prune_measurements(
            conn,
            cfg=cfg,
            now=completed,
        )

    return ReportResult(
        report_id=report_id,
        generated_at=completed,
        since_ts=since_ts,
        until_ts=until_ts,
        sections_enabled=enabled,
        sections_rendered=sections_rendered,
        sections_failed=tuple(sections_failed),
        rendered_terminal_text=terminal_text,
        rendered_markdown_text=markdown_text,
        markdown_path=written_markdown_path,
        rendered_html_text=html_text,
        html_path=written_html_path,
        library_version=library_version,
        disclaimer_version_hash=disclaimer_hash,
        duration_seconds=duration,
        section_contents=tuple(body_contents),
    )


# -- internals --------------------------------------------------------------


def _resolve_since(store: DuckDBStore, *, since: datetime | None, now: datetime) -> datetime:
    if since is not None:
        return since
    with store.connection() as conn:
        last = query_last_report(conn)
    if last is not None:
        return last.generated_at
    return now - timedelta(days=1)


def _assemble_section(
    conn: duckdb.DuckDBPyConnection,
    *,
    section_name: str,
    since_ts: datetime,
    until_ts: datetime,
    cfg: ReportConfig,
) -> dict[str, object]:
    """Dispatch to the correct assembler for a given section name."""
    if section_name == "system_health":
        return system_health_assembler.assemble(
            conn,
            since_ts=since_ts,
            until_ts=until_ts,
        )
    if section_name == "recent_tuning":
        return recent_tuning_assembler.assemble(
            conn,
            since_ts=since_ts,
            until_ts=until_ts,
        )
    if section_name == "surfaced":
        return surfaced_assembler.assemble(
            conn,
            since_ts=since_ts,
            until_ts=until_ts,
            single_venue_dominance_pct=cfg.thresholds.single_venue_dominance_pct,
            single_venue_dominance_pct_per_sector=(
                cfg.thresholds.single_venue_dominance_pct_per_sector
            ),
        )
    if section_name == "cross_venue":
        return cross_venue_assembler.assemble(
            conn,
            since_ts=since_ts,
            until_ts=until_ts,
            spread_threshold_bps=cfg.thresholds.cross_venue_spread_bps,
            spread_threshold_bps_per_sector=(cfg.thresholds.cross_venue_spread_bps_per_sector),
        )
    if section_name == "watched":
        return watched_assembler.assemble(conn, since_ts=since_ts, until_ts=until_ts)
    if section_name == "calibration":
        return calibration_assembler.assemble(
            conn,
            since_ts=since_ts,
            until_ts=until_ts,
            brier_window_days=cfg.thresholds.brier_window_days,
            miscalibration_threshold=cfg.thresholds.brier_miscalibration,
            brier_window_days_per_sector=(cfg.thresholds.brier_window_days_per_sector),
            miscalibration_threshold_per_sector=(cfg.thresholds.brier_miscalibration_per_sector),
        )
    if section_name == "reliability":
        return reliability_assembler.assemble(
            conn,
            since_ts=since_ts,
            until_ts=until_ts,
            bin_count=cfg.thresholds.reliability_bin_count,
            window_days=cfg.thresholds.brier_window_days,
            min_resolutions_per_bin=cfg.thresholds.reliability_min_resolutions_per_bin,
            window_days_per_sector=cfg.thresholds.brier_window_days_per_sector,
            bin_count_per_sector=cfg.thresholds.reliability_bin_count_per_sector,
            min_resolutions_per_bin_per_sector=(
                cfg.thresholds.reliability_min_resolutions_per_bin_per_sector
            ),
        )
    if section_name == "watchlist":
        return watchlist_assembler.assemble(
            conn,
            since_ts=since_ts,
            until_ts=until_ts,
            verbosity=cfg.verbosity_for("watchlist"),
        )
    raise ValueError(f"unknown section name {section_name!r}")


__all__ = ["generate"]


# -- threshold-distribution measurements (T-RG-COMPAT-MEAS-001) ------------


def _persist_threshold_measurements(
    conn: duckdb.DuckDBPyConnection,
    *,
    report_id: str,
    measured_at: datetime,
    body_contents: list[SectionContent],
    cfg: ReportConfig,
) -> None:
    """Compute and persist per-cycle distribution snapshots.

    Best-effort. Any exception is logged and swallowed so a
    measurement-side bug never breaks the report itself. The
    operator-facing surface is the
    ``razor-rooster report measurements`` CLI subcommand and
    the ``report_threshold_measurements`` table.

    v0.40.0 records one kind: ``cross_venue_spread_bps``.
    v0.41.0 adds two more:
    ``single_venue_dominance_share`` (from the surfaced
    section's ``venue_shares`` mapping) and
    ``brier_per_sector`` (from the calibration section's
    ``sector_brier_scores`` list).
    """
    try:
        for section in body_contents:
            if not section.ok or section.content is None:
                continue
            if section.name == "cross_venue":
                spread_values = cross_venue_spread_observations(section.content)
                persist_threshold_measurement(
                    conn,
                    report_id=report_id,
                    measurement_kind=MEASUREMENT_KIND_CROSS_VENUE_SPREAD_BPS,
                    measured_at=measured_at,
                    distribution=compute_distribution(
                        spread_values,
                        threshold=float(cfg.thresholds.cross_venue_spread_bps),
                    ),
                )
            elif section.name == "surfaced":
                dominance_values = single_venue_dominance_observations(section.content)
                persist_threshold_measurement(
                    conn,
                    report_id=report_id,
                    measurement_kind=MEASUREMENT_KIND_SINGLE_VENUE_DOMINANCE_SHARE,
                    measured_at=measured_at,
                    distribution=compute_distribution(
                        dominance_values,
                        threshold=float(cfg.thresholds.single_venue_dominance_pct),
                    ),
                )
            elif section.name == "calibration":
                brier_values = brier_per_sector_observations(section.content)
                persist_threshold_measurement(
                    conn,
                    report_id=report_id,
                    measurement_kind=MEASUREMENT_KIND_BRIER_PER_SECTOR,
                    measured_at=measured_at,
                    distribution=compute_distribution(
                        brier_values,
                        threshold=float(cfg.thresholds.brier_miscalibration),
                    ),
                )
    except Exception:
        logger.exception(
            "report_generator: threshold measurement persistence failed; "
            "report itself was unaffected"
        )


def _maybe_auto_prune_measurements(
    conn: duckdb.DuckDBPyConnection,
    *,
    cfg: ReportConfig,
    now: datetime,
) -> None:
    """Optionally prune old measurement rows after a successful cycle.

    Best-effort. When ``cfg.auto_prune.enabled`` is False, this is
    a no-op. When True, deletes rows that match the configured
    retention strategy. Any exception is logged and swallowed so
    a prune-side bug never breaks report generation.

    Operators opt in via ``config/report.yaml`` ``auto_prune:``
    block (T-RG-COMPAT-AUTOPRUNE-001 v0.43.0).
    """
    if not cfg.auto_prune.enabled:
        return
    if cfg.auto_prune.older_than_days is None and cfg.auto_prune.keep_last is None:
        # Nothing to do; the operator enabled auto_prune but didn't set
        # either strategy.
        return
    try:
        before: datetime | None = None
        if cfg.auto_prune.older_than_days is not None:
            before = now - timedelta(days=cfg.auto_prune.older_than_days)
        prune_threshold_measurements(
            conn,
            before=before,
            keep_last=cfg.auto_prune.keep_last,
            confirm=True,
        )
    except Exception:
        logger.exception(
            "report_generator: auto-prune of threshold measurements "
            "failed; report itself was unaffected"
        )
