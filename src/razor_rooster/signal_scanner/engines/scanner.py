"""Scan orchestrator (T-SCAN-030 / T-SCAN-031; design §3.4).

Two public entry points:

- :func:`evaluate_class` runs the full per-class pipeline and returns
  a typed (:class:`ScanRecord`, :class:`Trace`) pair.
- :func:`run_scan` orchestrates a full library-wide scan with bounded
  parallelism, library-version pinning, file-based logging, and the
  REQ-SCAN-EXEC-* failure-isolation contract.

The scan runner does no automatic scheduling. Operators run it via
``razor-rooster scan run`` (T-SCAN-040).
"""

from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.pattern_library import library, registry
from razor_rooster.pattern_library.models.base_rate import BaseRateResult
from razor_rooster.pattern_library.models.event_class import EventClass, PrecursorVariable
from razor_rooster.pattern_library.models.signature import SignatureResult
from razor_rooster.signal_scanner.engines.candidates import (
    CandidateConfig,
    CandidateDecision,
    identify_candidate,
)
from razor_rooster.signal_scanner.engines.posterior import (
    DEFAULT_MONTE_CARLO_SAMPLES,
    base_rate_only,
    posterior_with_ci,
)
from razor_rooster.signal_scanner.engines.trace import build_trace
from razor_rooster.signal_scanner.models import ScanRecord, ScanSummary, Trace
from razor_rooster.signal_scanner.persistence.operations import (
    complete_summary,
    persist_record,
    persist_trace,
    write_summary,
)

logger = logging.getLogger(__name__)


DEFAULT_MAX_WORKERS: int = 4
DEFAULT_LIBRARY_STALE_DAYS: int = 14
DEFAULT_LOOKBACK_DAYS: int = 30


class StrictDriftAbort(RuntimeError):
    """Raised when ``--strict`` is set and a class has definition_version drift."""


class LibraryVersionChangeError(RuntimeError):
    """Raised when the library version changes mid-scan."""


@dataclass(slots=True)
class ScanReport:
    """Aggregate result of one scan run."""

    scan_id: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    pattern_library_version: int = 0
    classes: list[ScanRecord] = field(default_factory=list)
    candidates: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def evaluate_class(
    *,
    store: DuckDBStore,
    cls: EventClass,
    scan_id: str,
    scan_started_at: datetime,
    library_version: int,
    candidate_config: CandidateConfig,
    n_samples: int = DEFAULT_MONTE_CARLO_SAMPLES,
    library_stale_warning: bool = False,
    rng: np.random.Generator | None = None,
    strict: bool = False,
    now: datetime | None = None,
) -> tuple[ScanRecord, Trace]:
    """Run the per-class scan pipeline (T-SCAN-030).

    Pulls base rate + signatures from ``pattern_library``, evaluates
    precursors against current ``data_ingest`` state, computes the
    posterior, applies the candidate-identification gates, and
    assembles the typed (:class:`ScanRecord`, :class:`Trace`) pair.

    Failures inside a class evaluation are captured on the record's
    ``error`` field rather than raised, so the scan orchestrator can
    keep going. The strict-drift abort is the one exception — it
    re-raises so the orchestrator can fail-fast per design §3.4.
    """
    rng = rng or np.random.default_rng(seed=hash(cls.class_id) & 0xFFFFFFFF)
    completed = now or datetime.now(tz=UTC)

    try:
        base_rate = library.base_rate(store, cls.class_id, library_version=library_version)
        if base_rate is None:
            base_rate = library.base_rate(store, cls.class_id)
        signatures = library.signature(store, cls.class_id, library_version=library_version)

        if base_rate is None:
            return _no_base_rate_record(
                cls=cls,
                scan_id=scan_id,
                scan_started_at=scan_started_at,
                library_version=library_version,
                library_stale_warning=library_stale_warning,
                completed_at=completed,
            )

        drift = _detect_definition_drift(cls, base_rate, signatures)
        if drift and strict:
            raise StrictDriftAbort(
                f"definition_version drift on {cls.class_id!r}: "
                f"class={cls.definition_version} base_rate={base_rate.definition_version}"
            )

        current_values, source_stale = _evaluate_precursors(
            store=store, cls=cls, signatures=signatures, scan_started_at=scan_started_at
        )

        if not current_values or _all_values_missing(current_values, signatures):
            posterior = base_rate_only(base_rate)
            no_update_reason = (
                "all sources stale" if source_stale else "no current data for precursors"
            )
            decision = CandidateDecision(
                is_candidate=False,
                direction=None,
                rejection_reasons=("no_update_applied",),
            )
            warnings = _collect_warnings(
                base_rate=base_rate,
                signatures=signatures,
                source_stale=source_stale,
                drift=drift,
                library_stale=library_stale_warning,
                no_update=True,
            )
            trace_payload = build_trace(
                cls=cls,
                base_rate=base_rate,
                signatures=signatures,
                current_values=current_values,
                posterior=posterior,
                is_candidate=False,
                candidate_direction=None,
                warnings=warnings,
                no_update_applied=True,
                no_update_reason=no_update_reason,
                library_version=library_version,
                data_as_of=base_rate.data_as_of,
            )
            record = ScanRecord(
                scan_id=scan_id,
                class_id=cls.class_id,
                class_definition_version=cls.definition_version,
                pattern_library_version=library_version,
                data_as_of=base_rate.data_as_of,
                scan_started_at=scan_started_at,
                scan_completed_at=completed,
                base_rate=posterior.posterior,
                base_rate_ci_lower=posterior.posterior_ci_lower,
                base_rate_ci_upper=posterior.posterior_ci_upper,
                posterior=posterior.posterior,
                posterior_ci_lower=posterior.posterior_ci_lower,
                posterior_ci_upper=posterior.posterior_ci_upper,
                log_odds_shift=0.0,
                is_candidate=False,
                candidate_direction=None,
                signature_confidence=_average_confidence(signatures),
                low_signature_confidence=any(s.low_confidence_warning for s in signatures),
                source_stale_warning=source_stale,
                library_stale_warning=library_stale_warning,
                definition_drift_warning=drift,
                no_update_applied=True,
                no_update_reason=no_update_reason,
            )
            return record, Trace(scan_id=scan_id, class_id=cls.class_id, payload=trace_payload)

        posterior = posterior_with_ci(
            base_rate,
            signatures,
            current_values=current_values,
            n_samples=n_samples,
            rng=rng,
        )
        signature_confidence = _average_confidence(signatures)
        decision = identify_candidate(
            sector=cls.domain_sector.value,
            log_odds_shift=posterior.log_odds_shift,
            signature_confidence=signature_confidence,
            source_stale=source_stale,
            no_update_applied=False,
            config=candidate_config,
        )
        warnings = _collect_warnings(
            base_rate=base_rate,
            signatures=signatures,
            source_stale=source_stale,
            drift=drift,
            library_stale=library_stale_warning,
            no_update=False,
        )
        trace_payload = build_trace(
            cls=cls,
            base_rate=base_rate,
            signatures=signatures,
            current_values=current_values,
            posterior=posterior,
            is_candidate=decision.is_candidate,
            candidate_direction=decision.direction,
            warnings=warnings,
            library_version=library_version,
            data_as_of=base_rate.data_as_of,
        )
        record = ScanRecord(
            scan_id=scan_id,
            class_id=cls.class_id,
            class_definition_version=cls.definition_version,
            pattern_library_version=library_version,
            data_as_of=base_rate.data_as_of,
            scan_started_at=scan_started_at,
            scan_completed_at=completed,
            base_rate=float(min(max(base_rate.rate_per_year, 0.0), 1.0)),
            base_rate_ci_lower=float(base_rate.credible_interval_lower),
            base_rate_ci_upper=float(base_rate.credible_interval_upper),
            posterior=posterior.posterior,
            posterior_ci_lower=posterior.posterior_ci_lower,
            posterior_ci_upper=posterior.posterior_ci_upper,
            log_odds_shift=posterior.log_odds_shift,
            is_candidate=decision.is_candidate,
            candidate_direction=decision.direction,
            signature_confidence=signature_confidence,
            low_signature_confidence=(
                signature_confidence is not None
                and signature_confidence < candidate_config.confidence_floor
            ),
            source_stale_warning=source_stale,
            library_stale_warning=library_stale_warning,
            definition_drift_warning=drift,
        )
        return record, Trace(scan_id=scan_id, class_id=cls.class_id, payload=trace_payload)
    except StrictDriftAbort:
        raise
    except Exception as exc:
        logger.exception("evaluate_class failed for %s", cls.class_id)
        record = ScanRecord(
            scan_id=scan_id,
            class_id=cls.class_id,
            class_definition_version=cls.definition_version,
            pattern_library_version=library_version,
            data_as_of=scan_started_at,
            scan_started_at=scan_started_at,
            scan_completed_at=completed,
            base_rate=0.0,
            base_rate_ci_lower=0.0,
            base_rate_ci_upper=0.0,
            posterior=0.0,
            posterior_ci_lower=0.0,
            posterior_ci_upper=0.0,
            log_odds_shift=0.0,
            error=f"{type(exc).__name__}: {exc}",
            library_stale_warning=library_stale_warning,
        )
        trace = Trace(
            scan_id=scan_id,
            class_id=cls.class_id,
            payload={
                "class_id": cls.class_id,
                "error": f"{type(exc).__name__}: {exc}",
                "library_version": library_version,
            },
        )
        return record, trace


def run_scan(
    store: DuckDBStore,
    *,
    only_class_id: str | None = None,
    strict: bool = False,
    candidate_config: CandidateConfig | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    library_stale_threshold_days: int = DEFAULT_LIBRARY_STALE_DAYS,
    n_samples: int = DEFAULT_MONTE_CARLO_SAMPLES,
    rng: np.random.Generator | None = None,
    now: datetime | None = None,
) -> ScanReport:
    """Run one scan over registered event classes (T-SCAN-031).

    Args:
        store: DuckDB store with both data_ingest, polymarket_connector,
            pattern_library, and signal_scanner schemas applied.
        only_class_id: When set, scan only this class. Other classes
            are skipped.
        strict: When True, definition_version drift on any class
            aborts the scan with :class:`StrictDriftAbort`. Default
            False (drift is flagged but scan continues).
        candidate_config: Override the default candidate-identification
            knobs.
        max_workers: Bound on per-class parallelism. v1 default is 4.
        library_stale_threshold_days: Days since the most recent
            library refresh that triggers ``library_stale_warning``.
        n_samples: Monte Carlo sample count for posterior CI.
        rng: Optional seeded RNG for reproducibility (testing).
        now: Override "now" for testing / replay.
    """
    started = now or datetime.now(tz=UTC)
    scan_id = str(uuid.uuid4())
    cfg = candidate_config or CandidateConfig()
    library_version = library.current_version()

    library_stale = _is_library_stale(
        store=store, threshold_days=library_stale_threshold_days, now=started
    )
    classes = registry.get_all()
    if only_class_id is not None:
        classes = tuple(c for c in classes if c.class_id == only_class_id)

    report = ScanReport(
        scan_id=scan_id,
        started_at=started,
        pattern_library_version=library_version,
    )

    summary = ScanSummary(
        scan_id=scan_id,
        scan_started_at=started,
        scan_completed_at=None,
        pattern_library_version=library_version,
        classes_total=len(classes),
        classes_succeeded=0,
        classes_failed=0,
        classes_skipped=0,
        candidates_count=0,
        library_stale_warning=library_stale,
    )
    with store.connection() as conn:
        write_summary(conn, summary)

    if not classes:
        completed = datetime.now(tz=UTC)
        with store.connection() as conn:
            complete_summary(
                conn,
                scan_id=scan_id,
                completed_at=completed,
                classes_succeeded=0,
                classes_failed=0,
                classes_skipped=0,
                candidates_count=0,
            )
        report.completed_at = completed
        report.duration_seconds = (completed - started).total_seconds()
        return report

    if max_workers <= 1 or len(classes) <= 1:
        for cls in classes:
            _run_one_and_persist(
                store=store,
                cls=cls,
                report=report,
                scan_id=scan_id,
                scan_started_at=started,
                library_version=library_version,
                candidate_config=cfg,
                n_samples=n_samples,
                library_stale_warning=library_stale,
                rng=rng,
                strict=strict,
            )
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    evaluate_class,
                    store=store,
                    cls=cls,
                    scan_id=scan_id,
                    scan_started_at=started,
                    library_version=library_version,
                    candidate_config=cfg,
                    n_samples=n_samples,
                    library_stale_warning=library_stale,
                    rng=rng,
                    strict=strict,
                ): cls.class_id
                for cls in classes
            }
            for fut in as_completed(futures):
                class_id = futures[fut]
                try:
                    record, trace = fut.result()
                except StrictDriftAbort:
                    raise
                except Exception as exc:
                    logger.exception("scan worker failed for %s", class_id)
                    report.errors.append(f"pool_error[{class_id}]: {type(exc).__name__}: {exc}")
                    continue
                _persist_outcome(store=store, record=record, trace=trace, report=report)

    # Library-version invariance check (REQ-SCAN-EXEC-004).
    final_library_version = library.current_version()
    if final_library_version != library_version:
        raise LibraryVersionChangeError(
            f"pattern_library version changed mid-scan: "
            f"{library_version} -> {final_library_version}"
        )

    completed = datetime.now(tz=UTC)
    with store.connection() as conn:
        complete_summary(
            conn,
            scan_id=scan_id,
            completed_at=completed,
            classes_succeeded=report.succeeded,
            classes_failed=report.failed,
            classes_skipped=report.skipped,
            candidates_count=report.candidates,
            error_summary=({"scan_errors": report.errors} if report.errors else None),
        )
    report.classes.sort(key=lambda r: r.class_id)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    logger.info(
        "scan_complete scan_id=%s library_version=%d total=%d "
        "succeeded=%d failed=%d skipped=%d candidates=%d duration_seconds=%.3f",
        scan_id,
        library_version,
        len(classes),
        report.succeeded,
        report.failed,
        report.skipped,
        report.candidates,
        report.duration_seconds,
    )
    return report


# -- internals --------------------------------------------------------------


def _run_one_and_persist(
    *,
    store: DuckDBStore,
    cls: EventClass,
    report: ScanReport,
    scan_id: str,
    scan_started_at: datetime,
    library_version: int,
    candidate_config: CandidateConfig,
    n_samples: int,
    library_stale_warning: bool,
    rng: np.random.Generator | None,
    strict: bool,
) -> None:
    """Single-threaded happy-path: evaluate then persist."""
    record, trace = evaluate_class(
        store=store,
        cls=cls,
        scan_id=scan_id,
        scan_started_at=scan_started_at,
        library_version=library_version,
        candidate_config=candidate_config,
        n_samples=n_samples,
        library_stale_warning=library_stale_warning,
        rng=rng,
        strict=strict,
    )
    _persist_outcome(store=store, record=record, trace=trace, report=report)


def _persist_outcome(
    *,
    store: DuckDBStore,
    record: ScanRecord,
    trace: Trace,
    report: ScanReport,
) -> None:
    """Common persistence + report bookkeeping."""
    with store.connection() as conn:
        persist_record(conn, record)
        persist_trace(conn, trace)
    report.classes.append(record)
    if record.error is not None:
        report.failed += 1
    else:
        report.succeeded += 1
    if record.is_candidate:
        report.candidates += 1


def _evaluate_precursors(
    *,
    store: DuckDBStore,
    cls: EventClass,
    signatures: tuple[SignatureResult, ...],
    scan_started_at: datetime,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> tuple[dict[str, float | None], bool]:
    """Pull each precursor's current value over the past ``lookback_days``.

    Returns a (values, source_stale) tuple. ``values`` maps
    variable_id -> latest observed value (or None when missing).
    ``source_stale`` is True when no precursor produced a value at
    all — heuristic for "all upstream sources stale" from the
    scanner's perspective.
    """
    if not cls.precursors:
        return {}, False

    window_start = scan_started_at - timedelta(days=lookback_days)
    window_end = scan_started_at

    sig_index = {s.variable_id: s for s in signatures}
    values: dict[str, float | None] = {}
    successes = 0
    for precursor in cls.precursors:
        if precursor.variable_id not in sig_index:
            values[precursor.variable_id] = None
            continue
        try:
            value = _evaluate_one_precursor(
                store=store, precursor=precursor, window_start=window_start, window_end=window_end
            )
        except Exception:
            logger.exception(
                "precursor evaluation failed for %s.%s", cls.class_id, precursor.variable_id
            )
            value = None
        values[precursor.variable_id] = value
        if value is not None:
            successes += 1
    source_stale = successes == 0 and bool(cls.precursors)
    return values, source_stale


def _evaluate_one_precursor(
    *,
    store: DuckDBStore,
    precursor: PrecursorVariable,
    window_start: datetime,
    window_end: datetime,
) -> float | None:
    """Run a precursor's query and return its latest finite value."""
    with store.connection() as conn:
        series = precursor.query(conn, window_start, window_end)
    if series is None or len(series) == 0:
        return None
    # Latest non-NaN value.
    cleaned = series.dropna() if hasattr(series, "dropna") else series
    if len(cleaned) == 0:
        return None
    last = cleaned.iloc[-1] if hasattr(cleaned, "iloc") else cleaned[-1]
    try:
        return float(last)
    except (TypeError, ValueError):
        return None


def _all_values_missing(
    current_values: dict[str, float | None], signatures: tuple[SignatureResult, ...]
) -> bool:
    """True when no precursor with a usable signature produced a value."""
    if not signatures:
        return True
    for sig in signatures:
        if sig.hit_rate is None or sig.threshold_value is None:
            continue
        v = current_values.get(sig.variable_id)
        if v is not None:
            return False
    return True


def _detect_definition_drift(
    cls: EventClass, base_rate: BaseRateResult, signatures: tuple[SignatureResult, ...]
) -> bool:
    """True when class definition has advanced past persisted outputs."""
    if base_rate.definition_version != cls.definition_version:
        return True
    return any(s.definition_version != cls.definition_version for s in signatures)


def _average_confidence(signatures: tuple[SignatureResult, ...]) -> float | None:
    if not signatures:
        return None
    valid = [s.confidence_score for s in signatures if s.confidence_score is not None]
    if not valid:
        return None
    return float(sum(valid) / len(valid))


def _collect_warnings(
    *,
    base_rate: BaseRateResult,
    signatures: tuple[SignatureResult, ...],
    source_stale: bool,
    drift: bool,
    library_stale: bool,
    no_update: bool,
) -> tuple[str, ...]:
    out: list[str] = []
    if base_rate.low_sample_warning:
        out.append("low_sample")
    if base_rate.source_stale_warning or source_stale:
        out.append("source_stale")
    if drift:
        out.append("definition_drift")
    if library_stale:
        out.append("library_stale")
    if no_update:
        out.append("no_update_applied")
    if any(s.low_confidence_warning for s in signatures):
        out.append("low_confidence_signatures")
    return tuple(out)


def _is_library_stale(
    *,
    store: DuckDBStore,
    threshold_days: int,
    now: datetime,
) -> bool:
    """True when the most recent pl_refresh_log row is older than threshold."""
    with store.connection() as conn:
        row = conn.execute("SELECT MAX(ended_at) FROM pl_refresh_log").fetchone()
    if row is None or row[0] is None:
        return True  # never refreshed
    last_refresh = row[0]
    if last_refresh.tzinfo is None:
        last_refresh = last_refresh.replace(tzinfo=UTC)
    return bool((now - last_refresh) > timedelta(days=threshold_days))


def _no_base_rate_record(
    *,
    cls: EventClass,
    scan_id: str,
    scan_started_at: datetime,
    library_version: int,
    library_stale_warning: bool,
    completed_at: datetime,
) -> tuple[ScanRecord, Trace]:
    """Empty-base-rate record for classes pattern_library hasn't refreshed yet."""
    payload: dict[str, Any] = {
        "class_id": cls.class_id,
        "library_version": library_version,
        "no_update_applied": True,
        "no_update_reason": "no base rate persisted; run pattern-library refresh",
    }
    record = ScanRecord(
        scan_id=scan_id,
        class_id=cls.class_id,
        class_definition_version=cls.definition_version,
        pattern_library_version=library_version,
        data_as_of=scan_started_at,
        scan_started_at=scan_started_at,
        scan_completed_at=completed_at,
        base_rate=0.0,
        base_rate_ci_lower=0.0,
        base_rate_ci_upper=0.0,
        posterior=0.0,
        posterior_ci_lower=0.0,
        posterior_ci_upper=0.0,
        log_odds_shift=0.0,
        no_update_applied=True,
        no_update_reason="no base rate persisted; run pattern-library refresh",
        library_stale_warning=library_stale_warning,
    )
    return record, Trace(scan_id=scan_id, class_id=cls.class_id, payload=payload)


# Internal sentinel for monotonic-time durations; used by the scan
# log emission for per-class duration tracking.
_MONOTONIC: tuple[Any, ...] = (time,)
