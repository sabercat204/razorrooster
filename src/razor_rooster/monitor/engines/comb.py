"""Cycle orchestrator (T-MON-030 / T-MON-031; design §3.4).

The Comb's main loop. ``run_cycle`` reads every watched + acted-on
analysis from ``position_engine.watch_states``, calls
``evaluate_analysis`` for each with per-analysis isolation, persists
the resulting follow-up, and stamps a ``monitor_cycles`` row.

When a resolution is detected, the cycle calls
``position_engine.run_expiration_pass`` so any active watch states
on the resolved analysis transition to ``'expired'`` immediately.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import duckdb

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.monitor.config.loader import MonitorConfig, load_config
from razor_rooster.monitor.engines.alert_ranker import compute_alert_tiers
from razor_rooster.monitor.engines.change_detector import (
    compute_market_shift,
    compute_model_shift,
    precursor_snapshot_to_dict,
    snapshot_precursors,
)
from razor_rooster.monitor.engines.invalidation_evaluator import (
    InvalidationsResult,
    evaluate_invalidations,
    evaluation_to_dict,
)
from razor_rooster.monitor.engines.reasoning import build_reasoning_text
from razor_rooster.monitor.models import (
    AlertTier,
    FollowUp,
    MonitorCycle,
    ResolutionStatus,
    ShiftResult,
)
from razor_rooster.monitor.persistence.operations import (
    complete_cycle,
    persist_follow_up,
    write_cycle,
)
from razor_rooster.position_engine.models import Analysis
from razor_rooster.position_engine.persistence.operations import (
    get_analysis,
    list_by_state,
)
from razor_rooster.position_engine.watch.expiration import run_expiration_pass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CycleReport:
    """Aggregate result of one monitor cycle."""

    cycle_id: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    follow_ups_total: int = 0
    follow_ups_with_alerts: int = 0
    alerts_by_tier: dict[str, int] = field(default_factory=dict)
    resolutions_detected: int = 0
    expirations_written: int = 0
    errors: list[str] = field(default_factory=list)


# -- public API -------------------------------------------------------------


def run_cycle(
    store: DuckDBStore,
    *,
    config: MonitorConfig | None = None,
    now: datetime | None = None,
) -> CycleReport:
    """Run one monitor cycle over all watched + acted-on analyses.

    Per-analysis isolation: an exception during one analysis's
    evaluation is captured into the follow-up's ``error`` field and
    the cycle continues with the next analysis.
    """
    cfg = config or load_config()
    started = now or datetime.now(tz=UTC)
    cycle_id = str(uuid.uuid4())
    report = CycleReport(cycle_id=cycle_id, started_at=started)

    # Stamp cycle row up front so persist_follow_up has a parent FK.
    with store.connection() as conn:
        write_cycle(
            conn,
            MonitorCycle(
                cycle_id=cycle_id,
                started_at=started,
                completed_at=None,
                follow_ups_total=0,
                follow_ups_with_alerts=0,
                alerts_by_tier={},
            ),
        )

    # Collect (analysis_id, state) tuples first, then re-fetch analysis
    # under a fresh connection per evaluation to keep failure isolation.
    watched_ids: list[str] = []
    with store.connection() as conn:
        for state_value in ("watching", "acted_on"):
            rows = list_by_state(conn, state=state_value)
            watched_ids.extend(r.analysis_id for r in rows)

    for analysis_id in watched_ids:
        try:
            with store.connection() as conn:
                analysis = get_analysis(conn, analysis_id=analysis_id)
                if analysis is None:
                    logger.warning(
                        "watch_states pointed at missing analysis %s; skipping",
                        analysis_id,
                    )
                    continue
                follow_up = evaluate_analysis(
                    conn,
                    cycle_id=cycle_id,
                    analysis=analysis,
                    config=cfg,
                    now=started,
                )
                persist_follow_up(conn, follow_up)
        except Exception as exc:
            logger.exception("monitor cycle failed for analysis_id=%s", analysis_id)
            report.errors.append(f"{analysis_id}: {type(exc).__name__}: {exc}")
            try:
                with store.connection() as conn:
                    persist_follow_up(
                        conn,
                        _error_follow_up(
                            cycle_id=cycle_id,
                            analysis_id=analysis_id,
                            error=f"{type(exc).__name__}: {exc}",
                            started=started,
                        ),
                    )
            except Exception as inner:
                logger.exception("failed to persist error follow-up for %s", analysis_id)
                report.errors.append(
                    f"{analysis_id} (error-record write): {type(inner).__name__}: {inner}"
                )
            continue

        # Re-read the just-persisted follow-up to update aggregates and
        # detect resolution-driven expiration triggers.
        with store.connection() as conn:
            row = conn.execute(
                "SELECT primary_alert_tier, resolution_status "
                "FROM follow_ups WHERE follow_up_id = ?",
                [follow_up.follow_up_id],
            ).fetchone()
        if row is not None:
            primary = row[0]
            resolution_status = row[1]
            report.follow_ups_total += 1
            if primary is not None:
                report.follow_ups_with_alerts += 1
                report.alerts_by_tier[str(primary)] = report.alerts_by_tier.get(str(primary), 0) + 1
            if resolution_status != "unresolved":
                report.resolutions_detected += 1

    # Trigger position_engine's expiration pass once if any resolutions
    # were detected. The pass is idempotent and reads
    # comparison_resolutions; resolutions detected here should already
    # be in that table if the upstream mispricing_detector cycle ran,
    # so this is purely a safety net to keep watch states consistent.
    if report.resolutions_detected > 0:
        try:
            expiration = run_expiration_pass(store, now=started)
            report.expirations_written = expiration.expirations_written
        except Exception as exc:
            logger.exception("expiration pass after monitor cycle failed")
            report.errors.append(f"expiration pass: {type(exc).__name__}: {exc}")

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()

    error_summary: dict[str, Any] | None = None
    if report.errors:
        error_summary = {"errors": report.errors}

    with store.connection() as conn:
        complete_cycle(
            conn,
            cycle_id=cycle_id,
            completed_at=completed,
            follow_ups_total=report.follow_ups_total,
            follow_ups_with_alerts=report.follow_ups_with_alerts,
            alerts_by_tier=report.alerts_by_tier,
            duration_seconds=report.duration_seconds,
            error_summary=error_summary,
        )

    return report


def evaluate_analysis(
    conn: duckdb.DuckDBPyConnection,
    *,
    cycle_id: str,
    analysis: Analysis,
    config: MonitorConfig,
    now: datetime | None = None,
) -> FollowUp:
    """Evaluate one analysis and produce a FollowUp.

    Resolution check is first and short-circuits other detection.
    Otherwise reads current scan + price, runs detectors, builds the
    reasoning text, and assembles the FollowUp.
    """
    moment = now or datetime.now(tz=UTC)
    follow_up_id = str(uuid.uuid4())

    # Fetch the comparison row to know outcome_token_id and polarity.
    comp = _query_comparison_for_analysis(conn, comparison_id=analysis.comparison_id)
    outcome_token_id = comp.get("outcome_token_id") if comp else None
    polarity = comp.get("polarity", "aligned") if comp else "aligned"

    # Per-sector bands: we read class.domain_sector if available; fall
    # back to the default.
    sector = _query_class_sector(conn, class_id=analysis.class_id) or "default"
    bands = config.bands_for(sector)

    # Resolution: short-circuit if the underlying market has resolved.
    resolution_outcome = _query_resolution(
        conn,
        condition_id=analysis.condition_id,
        venue=analysis.venue,
    )
    if resolution_outcome is not None:
        resolution_status = _resolution_status_for_polarity(
            outcome=resolution_outcome,
            polarity=polarity,
        )
        return _resolution_follow_up(
            follow_up_id=follow_up_id,
            cycle_id=cycle_id,
            analysis=analysis,
            resolution_status=resolution_status,
            time_decay_alert_days=config.time_decay_alert_days,
            moment=moment,
        )

    # Current snapshots.
    current_scan_record = _query_latest_scan_record(conn, class_id=analysis.class_id)
    current_scan_id = str(current_scan_record["scan_id"]) if current_scan_record else None
    current_model_p = (
        float(current_scan_record["posterior"]) if current_scan_record is not None else None
    )
    current_model_ci: tuple[float, float] | None = None
    if current_scan_record is not None:
        current_model_ci = (
            float(current_scan_record["posterior_ci_lower"]),
            float(current_scan_record["posterior_ci_upper"]),
        )
    source_stale = bool(
        current_scan_record["source_stale_warning"] if current_scan_record else False
    )
    library_stale = bool(
        current_scan_record["library_stale_warning"] if current_scan_record else False
    )

    current_price_row = _query_latest_price_snapshot(
        conn,
        condition_id=analysis.condition_id,
        outcome_token_id=outcome_token_id,
    )
    current_market_p = (
        float(current_price_row["mid_price"])
        if current_price_row is not None and current_price_row.get("mid_price") is not None
        else None
    )
    current_market_snapshot_ts = (
        current_price_row["snapshot_ts"] if current_price_row is not None else None
    )
    if polarity == "inverted" and current_market_p is not None:
        current_market_p = 1.0 - current_market_p

    # Shifts.
    model_shift = compute_model_shift(
        analysis_model_p=analysis.model_probability,
        current_model_p=current_model_p,
        bands=bands,
    )
    market_shift = compute_market_shift(
        analysis_market_p=analysis.market_probability,
        current_market_p=current_market_p,
        bands=bands,
    )

    # Precursor snapshot: pull both analysis-time and current scan
    # traces and pair them.
    analysis_precursors = _query_analysis_precursors(conn, analysis_id=analysis.analysis_id)
    current_precursors = (
        _query_scan_precursors(conn, scan_id=current_scan_id, class_id=analysis.class_id)
        if current_scan_id is not None
        else None
    )
    precursor_snapshot = snapshot_precursors(
        analysis_precursors=analysis_precursors,
        current_precursors=current_precursors,
    )

    # Time.
    days_since = (moment - analysis.computed_at).days if analysis.computed_at else 0
    days_to_resolution = analysis.days_to_resolution
    time_decay = (
        days_to_resolution is not None and days_to_resolution <= config.time_decay_alert_days
    )

    # Invalidation.
    invalidation_result = evaluate_invalidations(
        invalidation_criteria=list(analysis.invalidation_criteria),
        current_precursors=current_precursors,
        current_market_p=current_market_p,
    )

    # Recommendation.
    recommended_review = (
        invalidation_result.triggered_count > 0
        or model_shift.band in ("material", "major")
        or market_shift.band in ("material", "major")
        or time_decay
    )

    primary_alert_tier, all_alert_tiers = compute_alert_tiers(
        resolution_status="unresolved",
        invalidation_triggered_count=invalidation_result.triggered_count,
        model_shift=model_shift,
        market_shift=market_shift,
        precursor_snapshot=precursor_snapshot,
        time_decay_alert=time_decay,
    )

    reasoning_text = build_reasoning_text(
        class_id=analysis.class_id,
        condition_id=analysis.condition_id,
        days_since_analysis=days_since,
        days_to_resolution=days_to_resolution,
        resolution_status="unresolved",
        model_shift=model_shift,
        market_shift=market_shift,
        precursor_snapshot=precursor_snapshot,
        invalidations=invalidation_result,
        primary_alert_tier=primary_alert_tier,
        all_alert_tiers=all_alert_tiers,
        recommended_review=recommended_review,
        time_decay_alert_days=config.time_decay_alert_days,
        venue=analysis.venue,
    )

    return FollowUp(
        follow_up_id=follow_up_id,
        cycle_id=cycle_id,
        analysis_id=analysis.analysis_id,
        analysis_model_p=analysis.model_probability,
        analysis_market_p=analysis.market_probability,
        analysis_computed_at=analysis.computed_at or moment,
        current_scan_id=current_scan_id,
        current_model_p=current_model_p,
        current_model_ci=current_model_ci,
        current_market_p=current_market_p,
        current_market_snapshot_ts=current_market_snapshot_ts,
        model_probability_shift=model_shift.value,
        model_shift_band=model_shift.band,
        market_probability_shift=market_shift.value,
        market_shift_band=market_shift.band,
        precursor_snapshot=tuple(precursor_snapshot_to_dict(p) for p in precursor_snapshot),
        days_since_analysis=days_since,
        days_to_resolution=days_to_resolution,
        time_decay_alert=time_decay,
        invalidation_evaluations=tuple(
            evaluation_to_dict(ev) for ev in invalidation_result.evaluations
        ),
        invalidation_triggered_count=invalidation_result.triggered_count,
        resolution_status="unresolved",
        recommended_review=recommended_review,
        primary_alert_tier=primary_alert_tier,
        alert_tiers=tuple(all_alert_tiers),
        reasoning_text=reasoning_text,
        source_stale_warning=source_stale,
        library_stale_warning=library_stale,
        error=None,
        computed_at=moment,
        venue=analysis.venue,
    )


# -- internal helpers -------------------------------------------------------


def _resolution_follow_up(
    *,
    follow_up_id: str,
    cycle_id: str,
    analysis: Analysis,
    resolution_status: ResolutionStatus,
    time_decay_alert_days: int,
    moment: datetime,
) -> FollowUp:
    """Build a resolution-tagged follow-up that short-circuits detection."""
    days_since = (moment - analysis.computed_at).days if analysis.computed_at else 0
    primary_alert_tier: AlertTier = "resolution"
    all_alert_tiers: tuple[AlertTier, ...] = ("resolution",)

    empty_invalidations = InvalidationsResult(evaluations=(), triggered_count=0)
    empty_shift = ShiftResult(value=None, band=None)
    reasoning_text = build_reasoning_text(
        class_id=analysis.class_id,
        condition_id=analysis.condition_id,
        days_since_analysis=days_since,
        days_to_resolution=None,
        resolution_status=resolution_status,
        model_shift=empty_shift,
        market_shift=empty_shift,
        precursor_snapshot=(),
        invalidations=empty_invalidations,
        primary_alert_tier=primary_alert_tier,
        all_alert_tiers=all_alert_tiers,
        recommended_review=True,
        time_decay_alert_days=time_decay_alert_days,
        venue=analysis.venue,
    )
    return FollowUp(
        follow_up_id=follow_up_id,
        cycle_id=cycle_id,
        analysis_id=analysis.analysis_id,
        analysis_model_p=analysis.model_probability,
        analysis_market_p=analysis.market_probability,
        analysis_computed_at=analysis.computed_at or moment,
        current_scan_id=None,
        current_model_p=None,
        current_model_ci=None,
        current_market_p=None,
        current_market_snapshot_ts=None,
        model_probability_shift=None,
        model_shift_band=None,
        market_probability_shift=None,
        market_shift_band=None,
        precursor_snapshot=(),
        days_since_analysis=days_since,
        days_to_resolution=None,
        time_decay_alert=False,
        invalidation_evaluations=(),
        invalidation_triggered_count=0,
        resolution_status=resolution_status,
        recommended_review=True,
        primary_alert_tier=primary_alert_tier,
        alert_tiers=all_alert_tiers,
        reasoning_text=reasoning_text,
        source_stale_warning=False,
        library_stale_warning=False,
        error=None,
        computed_at=moment,
        venue=analysis.venue,
    )


def _error_follow_up(
    *,
    cycle_id: str,
    analysis_id: str,
    error: str,
    started: datetime,
) -> FollowUp:
    """Minimal follow-up captured when evaluation throws."""
    return FollowUp(
        follow_up_id=str(uuid.uuid4()),
        cycle_id=cycle_id,
        analysis_id=analysis_id,
        analysis_model_p=0.0,
        analysis_market_p=None,
        analysis_computed_at=started,
        current_scan_id=None,
        current_model_p=None,
        current_model_ci=None,
        current_market_p=None,
        current_market_snapshot_ts=None,
        model_probability_shift=None,
        model_shift_band=None,
        market_probability_shift=None,
        market_shift_band=None,
        precursor_snapshot=(),
        days_since_analysis=0,
        days_to_resolution=None,
        time_decay_alert=False,
        invalidation_evaluations=(),
        invalidation_triggered_count=0,
        resolution_status="unresolved",
        recommended_review=False,
        primary_alert_tier=None,
        alert_tiers=(),
        reasoning_text=f"Evaluation failed: {error}",
        source_stale_warning=False,
        library_stale_warning=False,
        error=error,
        computed_at=started,
    )


def _query_resolution(
    conn: duckdb.DuckDBPyConnection, *, condition_id: str, venue: str = "polymarket"
) -> str | None:
    """Return resolution outcome for a market, or None if unresolved.

    Branches on venue:

    - ``venue='polymarket'``: read from ``polymarket_markets`` +
      ``polymarket_resolutions`` (the original v1 path).
    - ``venue='kalshi'``: read from ``kalshi_settlements`` (rows arrive
      once the Kalshi connector backfills settlements, T-KSI-043). When
      the table is empty or no row matches the ticker, the function
      returns None and the cycle treats the analysis as unresolved.

    Returns one of ``'yes'``, ``'no'``, ``'invalid'`` or None.
    """
    if venue == "kalshi":
        return _query_kalshi_resolution(conn, ticker=condition_id)
    return _query_polymarket_resolution(conn, condition_id=condition_id)


def _query_polymarket_resolution(
    conn: duckdb.DuckDBPyConnection, *, condition_id: str
) -> str | None:
    row = conn.execute(
        "SELECT m.resolved, r.winning_outcome_label, r.invalidated "
        "FROM polymarket_markets m "
        "LEFT JOIN polymarket_resolutions r "
        "  ON m.condition_id = r.condition_id "
        "  AND r.superseded_at IS NULL "
        "WHERE m.condition_id = ? AND m.superseded_at IS NULL "
        "ORDER BY r.resolution_ts DESC NULLS LAST LIMIT 1",
        [condition_id],
    ).fetchone()
    if row is None:
        return None
    resolved = bool(row[0])
    if not resolved:
        return None
    invalidated = bool(row[2]) if row[2] is not None else False
    if invalidated:
        return "invalid"
    label = row[1]
    if label is None:
        return None
    label_str = str(label).strip().lower()
    if label_str in {"yes", "true", "1"}:
        return "yes"
    if label_str in {"no", "false", "0"}:
        return "no"
    return None


def _query_kalshi_resolution(conn: duckdb.DuckDBPyConnection, *, ticker: str) -> str | None:
    """Read the latest non-superseded settlement row for a Kalshi ticker.

    Returns ``'yes'`` / ``'no'`` from the ``result`` column,
    ``'invalid'`` when ``voided`` is true, or None if no settlement row
    exists.

    The query is defensive against the table not yet existing — if the
    Kalshi connector hasn't been initialized on this database, return
    None so the cycle treats the analysis as unresolved rather than
    crashing.
    """
    try:
        row = conn.execute(
            "SELECT result, voided FROM kalshi_settlements "
            "WHERE ticker = ? AND superseded_at IS NULL "
            "ORDER BY settlement_ts DESC LIMIT 1",
            [ticker],
        ).fetchone()
    except duckdb.CatalogException:
        return None
    if row is None:
        return None
    voided = bool(row[1]) if row[1] is not None else False
    if voided:
        return "invalid"
    result = row[0]
    if result is None:
        return None
    result_str = str(result).strip().lower()
    if result_str in {"yes", "true", "1"}:
        return "yes"
    if result_str in {"no", "false", "0"}:
        return "no"
    if result_str in {"void", "voided", "invalid"}:
        return "invalid"
    return None


def _resolution_status_for_polarity(*, outcome: str, polarity: str) -> ResolutionStatus:
    """Map raw outcome + mapping polarity to a ResolutionStatus."""
    if outcome == "invalid":
        return "resolved_invalid"
    if polarity == "inverted":
        # Inverted polarity flips the meaning of yes/no for the model.
        return "resolved_no" if outcome == "yes" else "resolved_yes"
    return "resolved_yes" if outcome == "yes" else "resolved_no"


def _query_comparison_for_analysis(
    conn: duckdb.DuckDBPyConnection, *, comparison_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT comparison_id, outcome_token_id, polarity FROM comparisons WHERE comparison_id = ?",
        [comparison_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "comparison_id": str(row[0]),
        "outcome_token_id": (str(row[1]) if row[1] is not None else None),
        "polarity": str(row[2]) if row[2] is not None else "aligned",
    }


def _query_class_sector(conn: duckdb.DuckDBPyConnection, *, class_id: str) -> str | None:
    row = conn.execute(
        "SELECT domain_sector FROM pl_event_classes WHERE class_id = ?",
        [class_id],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _query_latest_scan_record(
    conn: duckdb.DuckDBPyConnection, *, class_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT scan_id, posterior, posterior_ci_lower, posterior_ci_upper, "
        "source_stale_warning, library_stale_warning "
        "FROM scan_records "
        "WHERE class_id = ? "
        "ORDER BY scan_started_at DESC LIMIT 1",
        [class_id],
    ).fetchone()
    if row is None:
        return None
    return {
        "scan_id": str(row[0]),
        "posterior": float(row[1]),
        "posterior_ci_lower": float(row[2]),
        "posterior_ci_upper": float(row[3]),
        "source_stale_warning": bool(row[4]),
        "library_stale_warning": bool(row[5]),
    }


def _query_latest_price_snapshot(
    conn: duckdb.DuckDBPyConnection,
    *,
    condition_id: str,
    outcome_token_id: str | None,
) -> dict[str, Any] | None:
    if outcome_token_id is not None:
        row = conn.execute(
            "SELECT mid_price, snapshot_ts FROM polymarket_price_snapshots "
            "WHERE condition_id = ? AND outcome_token_id = ? "
            "  AND superseded_at IS NULL "
            "ORDER BY snapshot_ts DESC LIMIT 1",
            [condition_id, outcome_token_id],
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT mid_price, snapshot_ts FROM polymarket_price_snapshots "
            "WHERE condition_id = ? AND superseded_at IS NULL "
            "ORDER BY snapshot_ts DESC LIMIT 1",
            [condition_id],
        ).fetchone()
    if row is None:
        return None
    return {
        "mid_price": (float(row[0]) if row[0] is not None else None),
        "snapshot_ts": row[1],
    }


def _query_analysis_precursors(
    conn: duckdb.DuckDBPyConnection, *, analysis_id: str
) -> list[Mapping[str, Any]]:
    """Pull precursor list from the analysis trace's structured_dict.

    The mispricing-detector / position-engine trace surfaces a
    ``precursors`` list under ``signature_summary``; if the layout
    differs, return [] and let the change detector treat the analysis
    as having no observable precursors.
    """
    row = conn.execute(
        "SELECT structured_dict FROM analysis_traces WHERE analysis_id = ?",
        [analysis_id],
    ).fetchone()
    if row is None or row[0] is None:
        return []
    try:
        decoded = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, Mapping):
        return []
    precursors = decoded.get("precursors")
    if isinstance(precursors, list):
        return [p for p in precursors if isinstance(p, Mapping)]
    sig = decoded.get("signature_summary")
    if isinstance(sig, Mapping):
        nested = sig.get("precursors")
        if isinstance(nested, list):
            return [p for p in nested if isinstance(p, Mapping)]
    return []


def _query_scan_precursors(
    conn: duckdb.DuckDBPyConnection, *, scan_id: str | None, class_id: str
) -> list[Mapping[str, Any]] | None:
    """Pull precursor list from the latest scan trace for a class."""
    if scan_id is None:
        return None
    row = conn.execute(
        "SELECT trace_json FROM scan_traces WHERE scan_id = ? AND class_id = ?",
        [scan_id, class_id],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        decoded = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, Mapping):
        return None
    precursors = decoded.get("precursors")
    if isinstance(precursors, list):
        return [p for p in precursors if isinstance(p, Mapping)]
    sig = decoded.get("signature_summary")
    if isinstance(sig, Mapping):
        nested = sig.get("precursors")
        if isinstance(nested, list):
            return [p for p in nested if isinstance(p, Mapping)]
    return []


__all__ = [
    "CycleReport",
    "evaluate_analysis",
    "run_cycle",
]


_RESERVED: tuple[Any, ...] = (Iterable,)
