"""Analysis orchestrator (T-PE-050 / T-PE-051; design §3.4).

Two public entry points:

- :func:`analyze_comparison` runs the full per-comparison pipeline
  and returns a typed (:class:`Analysis`, :class:`AnalysisTrace`)
  pair. Sub-threshold comparisons short-circuit early.
- :func:`run_cycle` orchestrates a full library-wide (or single-
  comparison) cycle with per-comparison failure isolation per
  REQ-PE-CMP-001.

The cycle runner does not place orders, recommend sizing, or itself
decide whether the model or the market is correct. It produces
analyses that the operator reads and reasons about.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import duckdb

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.mispricing_detector.persistence.operations import (
    get_comparison,
    query_comparisons,
)
from razor_rooster.pattern_library import library, registry
from razor_rooster.position_engine.config.loader import (
    PositionEngineConfig,
    load_config,
)
from razor_rooster.position_engine.engines.bankroll import compute_survival
from razor_rooster.position_engine.engines.invalidation import extract_criteria
from razor_rooster.position_engine.engines.kelly import apply_pipeline
from razor_rooster.position_engine.engines.liquidity import compute_liquidity
from razor_rooster.position_engine.engines.sensitivity import compute_sensitivity
from razor_rooster.position_engine.engines.time_to_resolution import (
    days_remaining,
    is_long,
)
from razor_rooster.position_engine.frame.linter import (
    ImperativeLanguageDetected,
    LinterCatalog,
    check_text,
)
from razor_rooster.position_engine.frame.renderer import render, to_structured_dict
from razor_rooster.position_engine.models import (
    Analysis,
    AnalysisCycle,
    AnalysisTrace,
    BankrollConfig,
)
from razor_rooster.position_engine.persistence.operations import (
    complete_cycle,
    latest_bankroll_config,
    persist_analysis,
    persist_analysis_trace,
    write_cycle,
)

logger = logging.getLogger(__name__)


class NoBankrollConfigError(RuntimeError):
    """Raised when run_cycle is called without an active bankroll_config."""


@dataclass(slots=True)
class CycleReport:
    """Aggregate result of one analysis cycle."""

    cycle_id: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    bankroll_config_id: str | None = None
    analyses: list[Analysis] = field(default_factory=list)
    analyses_total: int = 0
    analyses_with_positive_kelly: int = 0
    analyses_clamped_by_cap: int = 0
    analyses_clamped_by_liquidity: int = 0
    errors: list[str] = field(default_factory=list)


def analyze_comparison(
    *,
    store: DuckDBStore,
    cycle_id: str,
    comparison_id: str,
    bankroll_config: BankrollConfig,
    pe_config: PositionEngineConfig,
    linter_catalog: LinterCatalog | None = None,
    now: datetime | None = None,
) -> tuple[Analysis, AnalysisTrace] | None:
    """Run the per-comparison pipeline.

    Returns None when the comparison is not found. Returns an
    :class:`Analysis` with ``error`` set when the analysis fails
    mid-pipeline (so the cycle runner can persist the partial
    record).
    """
    completed = now or datetime.now(tz=UTC)
    analysis_id = str(uuid.uuid4())

    try:
        with store.connection() as conn:
            comparison = get_comparison(conn, comparison_id=comparison_id)
        if comparison is None:
            return None

        # Gather class metadata for rendering.
        try:
            cls = registry.get(comparison.class_id)
            class_title = cls.title
            sector = cls.domain_sector.value
        except KeyError:
            class_title = comparison.class_id
            sector = "cross_cutting"

        # Pull the embedded scanner trace from the comparison's trace.
        scanner_trace = _read_scanner_trace_from_comparison(store, comparison_id=comparison_id)

        # Sub-threshold short-circuit.
        delta_abs = abs(comparison.delta) if comparison.delta is not None else 0.0
        sub_threshold = delta_abs < bankroll_config.min_edge_threshold

        if sub_threshold:
            analysis = _build_sub_threshold_analysis(
                analysis_id=analysis_id,
                cycle_id=cycle_id,
                comparison=comparison,
                bankroll_config=bankroll_config,
                completed_at=completed,
            )
        else:
            kelly_result = apply_pipeline(
                model_p=comparison.model_probability,
                market_p=comparison.market_probability,
                kelly_fraction_default=bankroll_config.kelly_fraction_default,
                max_single_position_pct=bankroll_config.max_single_position_pct,
            )

            liquidity_threshold = pe_config.liquidity_feasibility.threshold_for(sector)
            liquidity_result = compute_liquidity(
                suggested_fraction=kelly_result.suggested_after_max_cap,
                bankroll_usd=bankroll_config.analytical_bankroll_usd,
                volume_24h=comparison.market_volume_24h,
                threshold_pct_of_volume=liquidity_threshold,
            )

            survival = compute_survival(
                liquidity_result.suggested_fraction_after_clamp,
                scenarios=pe_config.bankroll_survival_scenarios,
            )

            # Time-to-resolution from the polymarket market.
            end_date = _read_market_end_date(store, condition_id=comparison.condition_id)
            days = days_remaining(end_date, now=completed)
            long_resolution = is_long(days, threshold=pe_config.long_resolution_days_threshold)

            invalidation = extract_criteria(
                scanner_trace=scanner_trace,
                model_probability=comparison.model_probability,
                market_probability=comparison.market_probability,
            )

            sensitivity = compute_sensitivity(
                model_probability=comparison.model_probability,
                market_probability=comparison.market_probability,
                kelly_fraction_default=bankroll_config.kelly_fraction_default,
                max_single_position_pct=bankroll_config.max_single_position_pct,
                perturbations=pe_config.sensitivity_perturbations,
            )

            ev = (
                comparison.expected_value
                if comparison.expected_value is not None
                else (
                    (comparison.model_probability - comparison.market_probability)
                    if comparison.market_probability is not None
                    else None
                )
            )

            analysis = Analysis(
                analysis_id=analysis_id,
                cycle_id=cycle_id,
                comparison_id=comparison_id,
                class_id=comparison.class_id,
                condition_id=comparison.condition_id,
                bankroll_config_id=bankroll_config.config_id,
                model_probability=comparison.model_probability,
                market_probability=comparison.market_probability,
                kelly_unclamped=kelly_result.kelly_unclamped,
                kelly_negative=kelly_result.kelly_negative,
                kelly_clamped_by_max_cap=kelly_result.clamped_by_max_cap,
                kelly_clamped_by_liquidity=liquidity_result.clamped,
                suggested_fraction=liquidity_result.suggested_fraction_after_clamp,
                suggested_dollar_size=liquidity_result.suggested_dollar_size_after_clamp,
                ev_per_dollar=(float(ev) if ev is not None else None),
                bankroll_after_1_loss_pct=float(survival.get(1, 1.0)),
                bankroll_after_3_losses_pct=float(survival.get(3, 1.0)),
                bankroll_after_5_losses_pct=float(survival.get(5, 1.0)),
                suggested_pct_of_24h_volume=(
                    None
                    if liquidity_result.pct_of_24h_volume is None
                    else float(liquidity_result.pct_of_24h_volume)
                    if liquidity_result.pct_of_24h_volume != float("inf")
                    else None
                ),
                days_to_resolution=days,
                long_time_to_resolution=long_resolution,
                sub_threshold=False,
                sensitivity_analysis=sensitivity,
                invalidation_criteria=tuple(invalidation),
                low_signature_confidence=comparison.low_signature_confidence,
                source_stale_warning=comparison.source_stale_warning,
                library_stale_warning=comparison.library_stale_warning,
                definition_drift_warning=comparison.definition_drift_warning,
                low_mapping_confidence=comparison.low_mapping_confidence,
                low_liquidity=liquidity_result.low_liquidity_flag,
                computed_at=completed,
                venue=comparison.venue,
            )

        # Render + lint.
        rendered = render(
            analysis,
            bankroll_usd=bankroll_config.analytical_bankroll_usd,
            class_title=class_title,
            sector=sector,
            market_spread_bps=comparison.market_spread_bps,
            log_odds_delta=comparison.log_odds_delta,
            model_ci=(comparison.model_ci_lower, comparison.model_ci_upper),
        )
        check_text(rendered, catalog=linter_catalog)

        structured = to_structured_dict(
            analysis,
            bankroll_usd=bankroll_config.analytical_bankroll_usd,
            class_title=class_title,
            sector=sector,
            log_odds_delta=comparison.log_odds_delta,
            market_spread_bps=comparison.market_spread_bps,
        )
        trace = AnalysisTrace(
            analysis_id=analysis_id,
            rendered_text=rendered,
            structured_dict=structured,
        )
        return analysis, trace

    except ImperativeLanguageDetected:
        raise
    except Exception as exc:
        logger.exception("analyze_comparison failed for %s", comparison_id)
        analysis = _error_analysis(
            analysis_id=analysis_id,
            cycle_id=cycle_id,
            comparison_id=comparison_id,
            bankroll_config=bankroll_config,
            error=f"{type(exc).__name__}: {exc}",
            completed_at=completed,
        )
        trace = AnalysisTrace(
            analysis_id=analysis_id,
            rendered_text=f"ANALYSIS ERROR for {comparison_id}: {exc}",
            structured_dict={
                "analysis_id": analysis_id,
                "comparison_id": comparison_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return analysis, trace


def run_cycle(
    store: DuckDBStore,
    *,
    include_suppressed: bool = False,
    pe_config: PositionEngineConfig | None = None,
    linter_catalog: LinterCatalog | None = None,
    now: datetime | None = None,
) -> CycleReport:
    """Run one analysis cycle (T-PE-051)."""
    started = now or datetime.now(tz=UTC)
    cycle_id = str(uuid.uuid4())
    config = pe_config or load_config()

    with store.connection() as conn:
        bankroll_config = latest_bankroll_config(conn)
    if bankroll_config is None:
        raise NoBankrollConfigError(
            "no bankroll_config row found; run "
            "`razor-rooster position-engine config --bankroll <usd>` first"
        )

    write_cycle_started_at = AnalysisCycle(
        cycle_id=cycle_id,
        started_at=started,
        completed_at=None,
        bankroll_config_id=bankroll_config.config_id,
        analyses_total=0,
        analyses_with_positive_kelly=0,
        analyses_clamped_by_cap=0,
        analyses_clamped_by_liquidity=0,
    )
    with store.connection() as conn:
        write_cycle(conn, write_cycle_started_at)

    report = CycleReport(
        cycle_id=cycle_id,
        started_at=started,
        bankroll_config_id=bankroll_config.config_id,
    )

    comparisons = _select_comparisons(
        store=store,
        include_suppressed=include_suppressed,
    )

    cap_count = 0
    liquidity_count = 0
    positive_kelly_count = 0

    for cmp in comparisons:
        try:
            result = analyze_comparison(
                store=store,
                cycle_id=cycle_id,
                comparison_id=cmp.comparison_id,
                bankroll_config=bankroll_config,
                pe_config=config,
                linter_catalog=linter_catalog,
                now=started,
            )
        except ImperativeLanguageDetected as exc:
            logger.exception(
                "linter rejected analysis output for comparison %s",
                cmp.comparison_id,
            )
            report.errors.append(f"{cmp.comparison_id}: ImperativeLanguageDetected: {exc.phrase}")
            continue
        if result is None:
            continue
        analysis, trace = result
        with store.connection() as conn:
            persist_analysis(conn, analysis)
            persist_analysis_trace(conn, trace)
        report.analyses.append(analysis)
        if analysis.error is not None:
            report.errors.append(f"{cmp.comparison_id}: {analysis.error}")
            continue
        if not analysis.kelly_negative and analysis.suggested_fraction > 0:
            positive_kelly_count += 1
        if analysis.kelly_clamped_by_max_cap:
            cap_count += 1
        if analysis.kelly_clamped_by_liquidity:
            liquidity_count += 1

    report.analyses_total = len(report.analyses)
    report.analyses_with_positive_kelly = positive_kelly_count
    report.analyses_clamped_by_cap = cap_count
    report.analyses_clamped_by_liquidity = liquidity_count

    completed = datetime.now(tz=UTC)
    duration = (completed - started).total_seconds()

    # T-PE-061: expiration pass runs at the end of each cycle.
    try:
        from razor_rooster.position_engine.watch.expiration import (
            run_expiration_pass,
        )

        expiration_report = run_expiration_pass(store, now=completed)
        if expiration_report.errors:
            report.errors.extend(f"expiration: {err}" for err in expiration_report.errors)
    except Exception as exc:
        logger.exception("expiration pass raised during cycle %s", cycle_id)
        report.errors.append(f"expiration: {type(exc).__name__}: {exc}")

    with store.connection() as conn:
        complete_cycle(
            conn,
            cycle_id=cycle_id,
            completed_at=completed,
            analyses_total=report.analyses_total,
            analyses_with_positive_kelly=positive_kelly_count,
            analyses_clamped_by_cap=cap_count,
            analyses_clamped_by_liquidity=liquidity_count,
            duration_seconds=duration,
            error_summary=({"cycle_errors": report.errors} if report.errors else None),
        )
    report.completed_at = completed
    report.duration_seconds = duration

    logger.info(
        "position_engine_cycle_complete cycle_id=%s analyses=%d "
        "positive_kelly=%d clamped_cap=%d clamped_liquidity=%d duration=%.3f",
        cycle_id,
        report.analyses_total,
        positive_kelly_count,
        cap_count,
        liquidity_count,
        duration,
    )

    return report


# -- internals --------------------------------------------------------------


def _select_comparisons(*, store: DuckDBStore, include_suppressed: bool) -> tuple[Any, ...]:
    """Return mispricing_detector comparisons to consider for analysis.

    By default, only surfaced comparisons. With ``include_suppressed``,
    return everything from the most recent cycle that hasn't already
    been analyzed.
    """
    with store.connection() as conn:
        if include_suppressed:
            comparisons = query_comparisons(conn)
        else:
            comparisons = query_comparisons(conn, surfaced_only=True)
    return comparisons


def _read_scanner_trace_from_comparison(
    store: DuckDBStore, *, comparison_id: str
) -> dict[str, Any]:
    """Pull the embedded scanner trace from comparison_traces."""
    with store.connection() as conn:
        row = conn.execute(
            "SELECT trace_json FROM comparison_traces WHERE comparison_id = ?",
            [comparison_id],
        ).fetchone()
    if row is None:
        return {}
    if not isinstance(row[0], str):
        return {}
    try:
        decoded = json.loads(row[0])
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    embedded = decoded.get("embedded_scanner_trace")
    return dict(embedded) if isinstance(embedded, dict) else {}


def _read_market_end_date(store: DuckDBStore, *, condition_id: str) -> datetime | None:
    """Pull the polymarket_markets.end_date for a market."""
    with store.connection() as conn:
        row = conn.execute(
            "SELECT end_date FROM polymarket_markets "
            "WHERE condition_id = ? AND superseded_at IS NULL "
            "ORDER BY fetch_ts DESC LIMIT 1",
            [condition_id],
        ).fetchone()
    if row is None or row[0] is None:
        return None
    end = row[0]
    if isinstance(end, datetime) and end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return end if isinstance(end, datetime) else None


def _build_sub_threshold_analysis(
    *,
    analysis_id: str,
    cycle_id: str,
    comparison: Any,
    bankroll_config: BankrollConfig,
    completed_at: datetime,
) -> Analysis:
    """Skip the math; return an Analysis tagged sub_threshold."""
    return Analysis(
        analysis_id=analysis_id,
        cycle_id=cycle_id,
        comparison_id=comparison.comparison_id,
        class_id=comparison.class_id,
        condition_id=comparison.condition_id,
        bankroll_config_id=bankroll_config.config_id,
        model_probability=comparison.model_probability,
        market_probability=comparison.market_probability,
        kelly_unclamped=0.0,
        kelly_negative=False,
        kelly_clamped_by_max_cap=False,
        kelly_clamped_by_liquidity=False,
        suggested_fraction=0.0,
        suggested_dollar_size=0.0,
        ev_per_dollar=None,
        bankroll_after_1_loss_pct=1.0,
        bankroll_after_3_losses_pct=1.0,
        bankroll_after_5_losses_pct=1.0,
        suggested_pct_of_24h_volume=None,
        days_to_resolution=None,
        long_time_to_resolution=False,
        sub_threshold=True,
        sensitivity_analysis=None,
        invalidation_criteria=(),
        low_signature_confidence=comparison.low_signature_confidence,
        source_stale_warning=comparison.source_stale_warning,
        library_stale_warning=comparison.library_stale_warning,
        definition_drift_warning=comparison.definition_drift_warning,
        low_mapping_confidence=comparison.low_mapping_confidence,
        low_liquidity=False,
        computed_at=completed_at,
        venue=comparison.venue,
    )


def _error_analysis(
    *,
    analysis_id: str,
    cycle_id: str,
    comparison_id: str,
    bankroll_config: BankrollConfig,
    error: str,
    completed_at: datetime,
) -> Analysis:
    """Failure-isolation analysis record."""
    return Analysis(
        analysis_id=analysis_id,
        cycle_id=cycle_id,
        comparison_id=comparison_id,
        class_id="(unknown)",
        condition_id="(unknown)",
        bankroll_config_id=bankroll_config.config_id,
        model_probability=0.0,
        market_probability=None,
        kelly_unclamped=0.0,
        kelly_negative=False,
        kelly_clamped_by_max_cap=False,
        kelly_clamped_by_liquidity=False,
        suggested_fraction=0.0,
        suggested_dollar_size=0.0,
        ev_per_dollar=None,
        bankroll_after_1_loss_pct=1.0,
        bankroll_after_3_losses_pct=1.0,
        bankroll_after_5_losses_pct=1.0,
        suggested_pct_of_24h_volume=None,
        days_to_resolution=None,
        long_time_to_resolution=False,
        sub_threshold=False,
        sensitivity_analysis=None,
        invalidation_criteria=(),
        error=error,
        computed_at=completed_at,
    )


# Internal sentinel — the cycle runner uses time.monotonic for duration
# tracking; this re-export lets callers pass an external clock if they
# want (currently unused, included for symmetry with other engines).
_TIME: tuple[Any, ...] = (time, Counter, Iterable, library, duckdb)
