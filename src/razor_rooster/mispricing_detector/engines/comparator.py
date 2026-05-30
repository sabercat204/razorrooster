"""Comparator (T-MD-034 / T-MD-040; design §3.4).

Two public entry points:

- :func:`compute_comparison` is the per-mapping workhorse: pulls
  model side from ``signal_scanner.scan_records``, market side from
  ``polymarket_*`` tables, computes delta + CI overlap + surfacing
  decision, and assembles a typed (:class:`Comparison`,
  :class:`ComparisonTrace`) pair.

- :func:`run_cycle` orchestrates a full library-wide (or single-class)
  cycle with per-mapping failure isolation per REQ-MD-CMP failure
  isolation.

The cycle runner does not place orders, recommend sizing, or itself
decide whether the model or the market is correct. It surfaces the
data and the reasoning.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.mispricing_detector.engines.ci_overlap import check_ci_overlap
from razor_rooster.mispricing_detector.engines.delta import (
    MarketSnapshot,
    compute_delta,
    expected_value,
    log_odds_delta,
    market_probability_from,
)
from razor_rooster.mispricing_detector.engines.mapping_resolver import resolve as resolve_mappings
from razor_rooster.mispricing_detector.engines.surfacing import (
    SurfacingConfig,
    confidence_weighted_score,
    surfacing_decision,
)
from razor_rooster.mispricing_detector.engines.trace import (
    ambiguity_factors_from_inputs,
    build_trace,
    case_for_market_from_context,
    case_for_model_from_signature,
)
from razor_rooster.mispricing_detector.mapping.auto_heuristic import HeuristicConfig
from razor_rooster.mispricing_detector.models import (
    ClassMarketMapping,
    Comparison,
    ComparisonCycle,
    ComparisonTrace,
)
from razor_rooster.mispricing_detector.persistence.operations import (
    complete_cycle,
    persist_comparison,
    persist_trace,
    write_cycle,
)
from razor_rooster.pattern_library import library, registry
from razor_rooster.signal_scanner.persistence.operations import (
    query_scan_records,
)

logger = logging.getLogger(__name__)


DEFAULT_MARKET_PRICE_FRESHNESS_SECONDS: int = 12 * 3600  # 12 hours


class NoScanAvailableError(RuntimeError):
    """Raised when no signal_scanner scan record is available for a class."""


class MissingMarketError(RuntimeError):
    """Raised when a mapping references a market that's not present."""


class MultiOutcomeMarketSkipped(RuntimeError):
    """Raised when a mapping points at a non-binary market (deferred to v1.1)."""


@dataclass(slots=True)
class CycleReport:
    """Aggregate result of one comparison cycle."""

    cycle_id: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    library_version: int = 0
    scan_id: str | None = None
    comparisons: list[Comparison] = field(default_factory=list)
    surfaced: int = 0
    suppressed_breakdown: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _MarketContext:
    """Pulled-together market snapshot + metadata used for one comparison."""

    market_probability: float | None
    snapshot: MarketSnapshot
    snapshot_ts: datetime | None
    outcome_token_id: str
    is_binary: bool
    market_warnings: tuple[str, ...]


def compute_comparison(
    *,
    store: DuckDBStore,
    cycle_id: str,
    mapping: ClassMarketMapping,
    scan_id: str,
    library_version: int,
    library_stale_warning: bool = False,
    market_price_freshness_seconds: int = DEFAULT_MARKET_PRICE_FRESHNESS_SECONDS,
    liquidity_floor: float | None = None,
    surfacing_config: SurfacingConfig | None = None,
    sector: str = "cross_cutting",
    now: datetime | None = None,
) -> tuple[Comparison, ComparisonTrace]:
    """Build one Comparison + Trace pair for a (mapping, scan) combo.

    Failure-isolation contract: per-comparison errors are caught and
    represented as ``error`` on the Comparison record rather than
    raised, so the cycle runner can keep going. The single exception
    is :class:`NoScanAvailableError`, raised by the cycle runner
    itself before per-mapping work begins.
    """
    completed = now or datetime.now(tz=UTC)
    cfg = surfacing_config or SurfacingConfig()
    comparison_id = str(uuid.uuid4())

    try:
        scan_record = _read_scan_record(store, scan_id=scan_id, class_id=mapping.class_id)
        market_ctx = _read_market_context(
            store,
            condition_id=mapping.condition_id,
            polarity=mapping.polarity,
            venue=mapping.venue,
        )
        if not market_ctx.is_binary:
            raise MultiOutcomeMarketSkipped(
                f"market {mapping.condition_id!r} is non-binary; deferred to v1.1"
            )

        snapshot = market_ctx.snapshot
        market_p = market_ctx.market_probability
        delta = compute_delta(scan_record["posterior"], market_p)
        log_delta = log_odds_delta(scan_record["posterior"], market_p)
        ev = expected_value(scan_record["posterior"], market_p)
        ci_overlap = check_ci_overlap(
            model_ci_lower=scan_record["posterior_ci_lower"],
            model_ci_upper=scan_record["posterior_ci_upper"],
            market_bid=snapshot.best_bid,
            market_ask=snapshot.best_ask,
        )

        # Compute warning flags.
        signature_confidence = scan_record.get("signature_confidence")
        low_signature_confidence = bool(
            scan_record.get("low_signature_confidence")
            or (signature_confidence is not None and signature_confidence < 0.3)
        )
        source_stale_warning = bool(scan_record.get("source_stale_warning"))
        scanner_library_stale = bool(scan_record.get("library_stale_warning"))
        definition_drift_warning = bool(scan_record.get("definition_drift_warning"))

        stale_market_price = _is_market_stale(
            snapshot_ts=market_ctx.snapshot_ts,
            now=completed,
            threshold_seconds=market_price_freshness_seconds,
        )
        no_market_price = market_p is None
        degenerate_orderbook = "degenerate_orderbook" in market_ctx.market_warnings
        low_liquidity = (
            liquidity_floor is not None
            and snapshot.volume_24h is not None
            and snapshot.volume_24h < liquidity_floor
        )
        low_mapping_confidence = mapping.mapping_confidence == "low"

        decision = surfacing_decision(
            sector=sector,
            log_odds_delta=log_delta,
            ci_overlap=ci_overlap,
            mapping_confidence=mapping.mapping_confidence,
            low_signature_confidence=low_signature_confidence,
            library_stale_warning=library_stale_warning or scanner_library_stale,
            stale_market_price=stale_market_price,
            no_market_price=no_market_price,
            low_liquidity=bool(low_liquidity),
            config=cfg,
        )

        score = (
            confidence_weighted_score(
                log_odds_delta=log_delta,
                signature_confidence=signature_confidence,
                market_volume_24h=snapshot.volume_24h,
                liquidity_floor=liquidity_floor,
            )
            if decision.surfaced
            else None
        )

        # Reasoning trace.
        embedded = scan_record.get("embedded_trace")
        case_model = case_for_model_from_signature(embedded_scanner_trace=embedded)
        case_market = case_for_market_from_context(
            market_volume_24h=snapshot.volume_24h,
            market_spread_bps=snapshot.spread_bps,
            market_probability=market_p,
            market_snapshot_ts=(
                market_ctx.snapshot_ts.isoformat() if market_ctx.snapshot_ts is not None else None
            ),
            liquidity_floor=liquidity_floor,
            embedded_scanner_trace=embedded,
        )
        ambiguity = ambiguity_factors_from_inputs(
            mapping_confidence=mapping.mapping_confidence,
            ci_overlap=ci_overlap,
            polarity=mapping.polarity,
        )

        warnings_list: list[str] = []
        for w in scan_record.get("warnings") or []:
            if w not in warnings_list:
                warnings_list.append(str(w))
        for label, fired in (
            ("stale_market_price", stale_market_price),
            ("no_market_price", no_market_price),
            ("degenerate_orderbook", degenerate_orderbook),
            ("low_liquidity", bool(low_liquidity)),
            ("low_mapping_confidence", low_mapping_confidence),
            ("library_stale_warning", library_stale_warning or scanner_library_stale),
        ):
            if fired and label not in warnings_list:
                warnings_list.append(label)

        trace_payload = build_trace(
            class_id=mapping.class_id,
            condition_id=mapping.condition_id,
            polarity=mapping.polarity,
            mapping=mapping,
            model_probability=scan_record["posterior"],
            model_ci=(scan_record["posterior_ci_lower"], scan_record["posterior_ci_upper"]),
            market_probability=market_p,
            market_best_bid=snapshot.best_bid,
            market_best_ask=snapshot.best_ask,
            market_volume_24h=snapshot.volume_24h,
            market_spread_bps=snapshot.spread_bps,
            market_snapshot_ts=(
                market_ctx.snapshot_ts.isoformat() if market_ctx.snapshot_ts is not None else None
            ),
            delta=delta,
            log_odds_delta=log_delta,
            ci_overlap=ci_overlap,
            expected_value_=ev,
            confidence_weighted_score=score,
            embedded_scanner_trace=embedded,
            case_for_model=case_model,
            case_for_market=case_market,
            ambiguity_factors=ambiguity,
            warnings=tuple(warnings_list),
            suppression_reasons=decision.suppression_reasons,
            surfaced=decision.surfaced,
        )

        comparison = Comparison(
            comparison_id=comparison_id,
            cycle_id=cycle_id,
            mapping_id=mapping.mapping_id,
            class_id=mapping.class_id,
            condition_id=mapping.condition_id,
            outcome_token_id=market_ctx.outcome_token_id,
            polarity=mapping.polarity,
            scan_id=scan_id,
            model_probability=scan_record["posterior"],
            model_ci_lower=scan_record["posterior_ci_lower"],
            model_ci_upper=scan_record["posterior_ci_upper"],
            market_probability=market_p,
            market_best_bid=snapshot.best_bid,
            market_best_ask=snapshot.best_ask,
            market_last_trade_price=snapshot.last_trade_price,
            market_volume_24h=snapshot.volume_24h,
            market_spread_bps=snapshot.spread_bps,
            market_snapshot_ts=market_ctx.snapshot_ts,
            delta=delta,
            log_odds_delta=log_delta,
            ci_overlap=ci_overlap,
            expected_value=ev,
            confidence_weighted_score=score,
            surfaced=decision.surfaced,
            suppression_reasons=decision.suppression_reasons,
            low_signature_confidence=low_signature_confidence,
            source_stale_warning=source_stale_warning,
            library_stale_warning=library_stale_warning or scanner_library_stale,
            definition_drift_warning=definition_drift_warning,
            stale_market_price=stale_market_price,
            no_market_price=no_market_price,
            degenerate_orderbook=degenerate_orderbook,
            low_liquidity=bool(low_liquidity),
            low_mapping_confidence=low_mapping_confidence,
            computed_at=completed,
            venue=mapping.venue,
        )
        return comparison, ComparisonTrace(comparison_id=comparison_id, payload=trace_payload)
    except MultiOutcomeMarketSkipped:
        raise
    except Exception as exc:
        logger.exception(
            "compute_comparison failed for class %s market %s",
            mapping.class_id,
            mapping.condition_id,
        )
        return _error_comparison(
            comparison_id=comparison_id,
            cycle_id=cycle_id,
            mapping=mapping,
            scan_id=scan_id,
            error=f"{type(exc).__name__}: {exc}",
            now=completed,
        )


def run_cycle(
    store: DuckDBStore,
    *,
    class_id_filter: str | None = None,
    surfacing_config: SurfacingConfig | None = None,
    heuristic_config: HeuristicConfig | None = None,
    market_price_freshness_seconds: int = DEFAULT_MARKET_PRICE_FRESHNESS_SECONDS,
    liquidity_floor: float | None = 10000.0,
    library_stale_warning: bool = False,
    now: datetime | None = None,
) -> CycleReport:
    """Run one comparison cycle (T-MD-040)."""
    started = now or datetime.now(tz=UTC)
    cycle_id = str(uuid.uuid4())
    library_version = library.current_version()

    scan_id = _latest_scan_id(store)
    if scan_id is None:
        raise NoScanAvailableError(
            "no signal_scanner scan record available; run `razor-rooster scan run` first"
        )

    cycle = ComparisonCycle(
        cycle_id=cycle_id,
        started_at=started,
        completed_at=None,
        comparisons_total=0,
        surfaced_count=0,
        suppressed_breakdown={},
        library_version_at_cycle=library_version,
        scan_id_consumed=scan_id,
    )
    with store.connection() as conn:
        write_cycle(conn, cycle)

    report = CycleReport(
        cycle_id=cycle_id,
        started_at=started,
        library_version=library_version,
        scan_id=scan_id,
    )

    with store.connection() as conn:
        mappings = resolve_mappings(
            conn,
            class_id_filter=class_id_filter,
            heuristic_config=heuristic_config,
            when=started,
        )

    if not mappings:
        completed = datetime.now(tz=UTC)
        with store.connection() as conn:
            complete_cycle(
                conn,
                cycle_id=cycle_id,
                completed_at=completed,
                comparisons_total=0,
                surfaced_count=0,
                suppressed_breakdown={},
            )
        report.completed_at = completed
        report.duration_seconds = (completed - started).total_seconds()
        return report

    sector_by_class = _sector_lookup_for_classes({m.class_id for m in mappings})
    suppression_counts: Counter[str] = Counter()

    for mapping in mappings:
        sector = sector_by_class.get(mapping.class_id, "cross_cutting")
        try:
            comparison, trace = compute_comparison(
                store=store,
                cycle_id=cycle_id,
                mapping=mapping,
                scan_id=scan_id,
                library_version=library_version,
                library_stale_warning=library_stale_warning,
                market_price_freshness_seconds=market_price_freshness_seconds,
                liquidity_floor=liquidity_floor,
                surfacing_config=surfacing_config,
                sector=sector,
                now=started,
            )
        except MultiOutcomeMarketSkipped as exc:
            logger.info("cycle skipped non-binary market %s: %s", mapping.condition_id, exc)
            suppression_counts["multi_outcome_market_skipped"] += 1
            continue
        with store.connection() as conn:
            persist_comparison(conn, comparison)
            persist_trace(conn, trace)
        report.comparisons.append(comparison)
        if comparison.surfaced:
            report.surfaced += 1
        for reason in comparison.suppression_reasons:
            suppression_counts[reason] += 1
        if comparison.error is not None:
            report.errors.append(f"{comparison.class_id}: {comparison.error}")

    breakdown = dict(suppression_counts)

    # T-MD-042: Linkage pass runs after the comparison pass.
    try:
        linkage_report = _run_linkage_after_cycle(store, started=started)
        if linkage_report.errors:
            report.errors.extend(f"linkage: {err}" for err in linkage_report.errors)
    except Exception as exc:
        logger.exception("linkage pass raised during cycle %s", cycle_id)
        report.errors.append(f"linkage: {type(exc).__name__}: {exc}")

    completed = datetime.now(tz=UTC)
    with store.connection() as conn:
        complete_cycle(
            conn,
            cycle_id=cycle_id,
            completed_at=completed,
            comparisons_total=len(report.comparisons),
            surfaced_count=report.surfaced,
            suppressed_breakdown=breakdown,
            error_summary=({"cycle_errors": report.errors} if report.errors else None),
        )
    report.suppressed_breakdown = breakdown
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    return report


def _run_linkage_after_cycle(store: DuckDBStore, *, started: datetime) -> Any:
    """Run the linkage pass after the comparison pass completes.

    Imported lazily to keep ``engines.linkage`` decoupled from the
    cycle module's load-order. Returns the LinkageReport opaquely;
    the cycle runner only inspects ``.errors``.
    """
    from razor_rooster.mispricing_detector.engines.linkage import run_linkage_pass

    return run_linkage_pass(store, now=started)


# -- internals --------------------------------------------------------------


def _latest_scan_id(store: DuckDBStore) -> str | None:
    """Return the most recent ``signal_scanner.scan_summaries.scan_id``."""
    with store.connection() as conn:
        row = conn.execute(
            "SELECT scan_id FROM scan_summaries "
            "WHERE scan_completed_at IS NOT NULL "
            "ORDER BY scan_started_at DESC LIMIT 1"
        ).fetchone()
    return str(row[0]) if row is not None else None


def _read_scan_record(store: DuckDBStore, *, scan_id: str, class_id: str) -> dict[str, Any]:
    """Pull the scan record + embedded trace for one (scan, class) pair.

    Returns a dict so the comparator can treat the scanner record
    structurally without taking a hard dependency on the signal_scanner
    Pydantic / dataclass surface.
    """
    with store.connection() as conn:
        records = query_scan_records(conn, scan_id=scan_id)
    record = next((r for r in records if r.class_id == class_id), None)
    if record is None:
        raise RuntimeError(
            f"scan record for class_id={class_id!r} not found in scan_id={scan_id!r}"
        )
    with store.connection() as conn:
        trace_row = conn.execute(
            "SELECT trace_json FROM scan_traces WHERE scan_id = ? AND class_id = ?",
            [scan_id, class_id],
        ).fetchone()
    embedded: dict[str, Any] = {}
    if trace_row and isinstance(trace_row[0], str):
        try:
            decoded = json.loads(trace_row[0])
            if isinstance(decoded, dict):
                embedded = decoded
        except json.JSONDecodeError:
            embedded = {}
    return {
        "posterior": float(record.posterior),
        "posterior_ci_lower": float(record.posterior_ci_lower),
        "posterior_ci_upper": float(record.posterior_ci_upper),
        "log_odds_shift": float(record.log_odds_shift),
        "signature_confidence": (
            float(record.signature_confidence) if record.signature_confidence is not None else None
        ),
        "low_signature_confidence": bool(record.low_signature_confidence),
        "source_stale_warning": bool(record.source_stale_warning),
        "library_stale_warning": bool(record.library_stale_warning),
        "definition_drift_warning": bool(record.definition_drift_warning),
        "warnings": list(embedded.get("warnings") or []),
        "embedded_trace": embedded,
    }


def _read_market_context(
    store: DuckDBStore,
    *,
    condition_id: str,
    polarity: str,
    venue: str = "polymarket",
) -> _MarketContext:
    """Load market metadata + latest price snapshot.

    Branches on ``venue``: 'polymarket' reads ``polymarket_*`` tables
    (the v1 path); 'kalshi' reads ``kalshi_*`` tables (T-KSI-061).
    Both branches return the same :class:`_MarketContext` shape so the
    rest of the comparator stays venue-agnostic.
    """
    if venue == "kalshi":
        return _read_kalshi_market_context(store, ticker=condition_id, polarity=polarity)
    return _read_polymarket_market_context(store, condition_id=condition_id, polarity=polarity)


def _read_polymarket_market_context(
    store: DuckDBStore, *, condition_id: str, polarity: str
) -> _MarketContext:
    """Polymarket-side market reader (the original v1 path)."""
    with store.connection() as conn:
        market_row = conn.execute(
            "SELECT market_type, outcome_tokens FROM polymarket_markets "
            "WHERE condition_id = ? AND superseded_at IS NULL "
            "ORDER BY fetch_ts DESC LIMIT 1",
            [condition_id],
        ).fetchone()
    if market_row is None:
        raise MissingMarketError(f"polymarket_markets has no row for {condition_id!r}")

    is_binary = str(market_row[0]).lower() == "binary"
    outcome_tokens = []
    if isinstance(market_row[1], str) and market_row[1]:
        try:
            decoded = json.loads(market_row[1])
            if isinstance(decoded, list):
                outcome_tokens = decoded
        except json.JSONDecodeError:
            outcome_tokens = []

    yes_token = _pick_yes_token(outcome_tokens)
    if not yes_token:
        # Fall back to the first listed token. This still allows the
        # comparator to report SOMETHING; surfacing logic will likely
        # suppress.
        yes_token = outcome_tokens[0].get("id") if outcome_tokens else "unknown"

    with store.connection() as conn:
        snap_row = conn.execute(
            "SELECT snapshot_ts, mid_price, best_bid, best_ask, last_trade_price, "
            "last_trade_ts, volume_24h, spread_bps "
            "FROM polymarket_price_snapshots "
            "WHERE condition_id = ? AND outcome_token_id = ? AND superseded_at IS NULL "
            "ORDER BY snapshot_ts DESC LIMIT 1",
            [condition_id, yes_token],
        ).fetchone()

    if snap_row is None:
        snapshot = MarketSnapshot(best_bid=None, best_ask=None, last_trade_price=None)
        market_p, warnings_list = market_probability_from(snapshot, polarity=polarity)  # type: ignore[arg-type]
        return _MarketContext(
            market_probability=market_p,
            snapshot=snapshot,
            snapshot_ts=None,
            outcome_token_id=str(yes_token),
            is_binary=is_binary,
            market_warnings=tuple(warnings_list),
        )

    snapshot = MarketSnapshot(
        best_bid=(float(snap_row[2]) if snap_row[2] is not None else None),
        best_ask=(float(snap_row[3]) if snap_row[3] is not None else None),
        last_trade_price=(float(snap_row[4]) if snap_row[4] is not None else None),
        volume_24h=(float(snap_row[6]) if snap_row[6] is not None else None),
        spread_bps=(int(snap_row[7]) if snap_row[7] is not None else None),
    )
    market_p, warnings_list = market_probability_from(snapshot, polarity=polarity)  # type: ignore[arg-type]
    return _MarketContext(
        market_probability=market_p,
        snapshot=snapshot,
        snapshot_ts=snap_row[0],
        outcome_token_id=str(yes_token),
        is_binary=is_binary,
        market_warnings=tuple(warnings_list),
    )


def _read_kalshi_market_context(
    store: DuckDBStore, *, ticker: str, polarity: str
) -> _MarketContext:
    """Kalshi-side market reader (T-KSI-061; design §3.10).

    Reads ``kalshi_markets`` for binary detection and the latest
    non-superseded ``kalshi_price_snapshots`` row for the YES-side
    quotes. Convention: ``outcome_token_id`` mirrors the ticker for
    Kalshi rows since Kalshi has no separate token concept; this
    keeps the column populated end-to-end.
    """
    with store.connection() as conn:
        market_row = conn.execute(
            "SELECT market_type FROM kalshi_markets "
            "WHERE ticker = ? AND superseded_at IS NULL "
            "ORDER BY fetch_ts DESC LIMIT 1",
            [ticker],
        ).fetchone()
    if market_row is None:
        raise MissingMarketError(f"kalshi_markets has no row for {ticker!r}")

    is_binary = str(market_row[0]).lower() == "binary"

    with store.connection() as conn:
        snap_row = conn.execute(
            "SELECT snapshot_ts, mid_price_dollars, yes_bid_dollars, yes_ask_dollars, "
            "last_trade_price_dollars, last_trade_ts, volume_24h, spread_bps "
            "FROM kalshi_price_snapshots "
            "WHERE ticker = ? AND superseded_at IS NULL "
            "ORDER BY snapshot_ts DESC LIMIT 1",
            [ticker],
        ).fetchone()

    if snap_row is None:
        snapshot = MarketSnapshot(best_bid=None, best_ask=None, last_trade_price=None)
        market_p, warnings_list = market_probability_from(snapshot, polarity=polarity)  # type: ignore[arg-type]
        return _MarketContext(
            market_probability=market_p,
            snapshot=snapshot,
            snapshot_ts=None,
            outcome_token_id=ticker,
            is_binary=is_binary,
            market_warnings=tuple(warnings_list),
        )

    snapshot = MarketSnapshot(
        best_bid=(float(snap_row[2]) if snap_row[2] is not None else None),
        best_ask=(float(snap_row[3]) if snap_row[3] is not None else None),
        last_trade_price=(float(snap_row[4]) if snap_row[4] is not None else None),
        volume_24h=(float(snap_row[6]) if snap_row[6] is not None else None),
        spread_bps=(int(snap_row[7]) if snap_row[7] is not None else None),
    )
    market_p, warnings_list = market_probability_from(snapshot, polarity=polarity)  # type: ignore[arg-type]
    return _MarketContext(
        market_probability=market_p,
        snapshot=snapshot,
        snapshot_ts=snap_row[0],
        outcome_token_id=ticker,
        is_binary=is_binary,
        market_warnings=tuple(warnings_list),
    )


def _pick_yes_token(tokens: list[Any]) -> str | None:
    for token in tokens:
        if not isinstance(token, dict):
            continue
        outcome = str(token.get("outcome", "")).lower()
        if outcome == "yes":
            tok_id = token.get("id") or token.get("token_id")
            if tok_id is not None:
                return str(tok_id)
    return None


def _is_market_stale(
    *, snapshot_ts: datetime | None, now: datetime, threshold_seconds: int
) -> bool:
    if snapshot_ts is None:
        return True
    if snapshot_ts.tzinfo is None:
        snapshot_ts = snapshot_ts.replace(tzinfo=UTC)
    return (now - snapshot_ts) > timedelta(seconds=threshold_seconds)


def _sector_lookup_for_classes(class_ids: set[str]) -> dict[str, str]:
    """Build class_id -> sector_value lookup for the cycle's classes."""
    out: dict[str, str] = {}
    for class_id in class_ids:
        try:
            cls = registry.get(class_id)
        except KeyError:
            continue
        out[class_id] = cls.domain_sector.value
    return out


def _error_comparison(
    *,
    comparison_id: str,
    cycle_id: str,
    mapping: ClassMarketMapping,
    scan_id: str,
    error: str,
    now: datetime,
) -> tuple[Comparison, ComparisonTrace]:
    comparison = Comparison(
        comparison_id=comparison_id,
        cycle_id=cycle_id,
        mapping_id=mapping.mapping_id,
        class_id=mapping.class_id,
        condition_id=mapping.condition_id,
        outcome_token_id="unknown",
        polarity=mapping.polarity,
        scan_id=scan_id,
        model_probability=0.0,
        model_ci_lower=0.0,
        model_ci_upper=0.0,
        market_probability=None,
        market_best_bid=None,
        market_best_ask=None,
        market_last_trade_price=None,
        market_volume_24h=None,
        market_spread_bps=None,
        market_snapshot_ts=None,
        delta=None,
        log_odds_delta=None,
        ci_overlap=False,
        expected_value=None,
        confidence_weighted_score=None,
        surfaced=False,
        suppression_reasons=("error",),
        no_market_price=True,
        error=error,
        computed_at=now,
        venue=mapping.venue,
    )
    payload: dict[str, Any] = {
        "class_id": mapping.class_id,
        "condition_id": mapping.condition_id,
        "error": error,
        "surfaced": False,
    }
    return comparison, ComparisonTrace(comparison_id=comparison_id, payload=payload)


__all__ = [
    "DEFAULT_MARKET_PRICE_FRESHNESS_SECONDS",
    "CycleReport",
    "MissingMarketError",
    "MultiOutcomeMarketSkipped",
    "NoScanAvailableError",
    "compute_comparison",
    "run_cycle",
]
