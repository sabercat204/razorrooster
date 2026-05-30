"""Pattern-library refresh runner (T-PL-050; design §3.7).

The refresh orchestrator wires the four computation engines together:

1. **Sync class registry** against ``pl_event_classes``; the diff feeds
   the version-bump rules in §3.6.
2. **Bump library version** via :func:`pattern_library.version.bump_for_reason`
   when the diff changed something.
3. **For each registered class**, in dependency order:
   a. Pull occurrences via ``cls.occurrence_query``.
   b. Persist ``OutcomeRecord`` rows to ``pl_outcomes``.
   c. Compute the base rate over the class's default window.
   d. Compute per-precursor signatures + the co-occurrence lookup.
   e. Compute the analogue feature space and persist its rows.
   f. Compute calibration when n_events >= 10.
4. **Record the refresh log row** in ``pl_refresh_log`` and stamp every
   class's ``last_evaluated_at`` / ``library_version_at_last_eval``.

Concurrency / isolation:

- File-based lock at ``data/library/.refresh.lock`` prevents concurrent
  refresh runs.
- ``max_workers`` (default 2) bounds per-class parallelism; each class's
  pipeline is sequential (engines depend on the previous stage's output).
- Per-class failures are caught and recorded in the refresh report;
  other classes still complete. The class's prior outputs are left in
  place — the failure does not corrupt them.

The runner deliberately does no automatic scheduling — refresh is
operator-initiated via ``razor-rooster pattern-library refresh`` (T-PL-051).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.pattern_library import registry
from razor_rooster.pattern_library.engines.analogues import (
    populate_feature_space,
)
from razor_rooster.pattern_library.engines.base_rates import (
    compute_base_rate,
)
from razor_rooster.pattern_library.engines.calibration import (
    DEFAULT_TRACE_DIR,
    MIN_OCCURRENCES_FOR_CALIBRATION,
    compute_calibration,
)
from razor_rooster.pattern_library.engines.signatures import (
    compute_signature,
)
from razor_rooster.pattern_library.models.event_class import EventClass
from razor_rooster.pattern_library.models.outcomes import OutcomeRecord
from razor_rooster.pattern_library.persistence.operations import (
    record_class_evaluation,
    record_refresh,
    upsert_analogue_features,
    upsert_base_rate,
    upsert_calibration,
    upsert_outcomes,
    upsert_signature,
)
from razor_rooster.pattern_library.registry import ClassDelta
from razor_rooster.pattern_library.version import (
    BumpReason,
    bump_for_reason,
    current_version,
)

logger = logging.getLogger(__name__)


DEFAULT_LOCK_PATH: Path = Path("data") / "library" / ".refresh.lock"
DEFAULT_MAX_WORKERS: int = 2

# How long to wait when trying to acquire the refresh lock before
# giving up. Refresh is operator-initiated so the timeout is short:
# ten seconds is plenty for "wait briefly, then surface the conflict."
LOCK_ACQUIRE_TIMEOUT_SECONDS: float = 10.0


class RefreshLockHeldError(RuntimeError):
    """Raised when the refresh lock cannot be acquired in time."""


@dataclass(slots=True)
class ClassRefreshOutcome:
    """Per-class refresh outcome."""

    class_id: str
    status: str  # 'ok' | 'failed' | 'skipped'
    duration_seconds: float
    occurrences_persisted: int = 0
    base_rate_computed: bool = False
    signatures_computed: int = 0
    analogue_points_persisted: int = 0
    calibration_status: str | None = None  # 'computed' | 'insufficient_data' | None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RefreshReport:
    """Aggregate result of one refresh run."""

    refresh_id: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    library_version: int = 0
    class_delta: ClassDelta | None = None
    bump_reason: str | None = None
    classes: list[ClassRefreshOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_refresh(
    store: DuckDBStore,
    *,
    only_class_id: str | None = None,
    force: bool = False,
    lock_path: Path | str | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    trace_dir: Path | str | None = None,
    rng: np.random.Generator | None = None,
    now: datetime | None = None,
) -> RefreshReport:
    """Run one library-wide refresh, or one targeted class refresh.

    Args:
        store: DuckDB store with both data_ingest and pattern_library
            schemas applied.
        only_class_id: When set, refresh just this one class. Other
            classes are left untouched. Useful during operator triage.
        force: When True, runs the refresh even if the registry diff
            shows no changes. The library version still bumps if any
            of the §3.6 conditions hold.
        lock_path: Override the default ``data/library/.refresh.lock``
            path. Tests pass a temp-dir path.
        max_workers: Bound on per-class parallelism. v1 default is 2;
            DuckDB connection pool's default cap matches.
        trace_dir: Override the calibration trace-file directory.
        rng: Seeded RNG for engines that need randomness; defaults to
            a per-class deterministic seed.
        now: Override "now" for testing / replay.

    Returns:
        :class:`RefreshReport` with per-class outcomes and any
        cycle-level errors.
    """
    started = now or datetime.now(tz=UTC)
    refresh_id = str(uuid.uuid4())
    report = RefreshReport(
        refresh_id=refresh_id,
        started_at=started,
        library_version=current_version(),
    )

    resolved_lock = Path(lock_path) if lock_path is not None else DEFAULT_LOCK_PATH

    try:
        with _acquire_refresh_lock(resolved_lock):
            _run_refresh_locked(
                store,
                report=report,
                only_class_id=only_class_id,
                force=force,
                max_workers=max_workers,
                trace_dir=Path(trace_dir) if trace_dir else DEFAULT_TRACE_DIR,
                rng=rng,
                started=started,
                refresh_id=refresh_id,
            )
    except RefreshLockHeldError as exc:
        logger.exception("refresh lock unavailable")
        report.errors.append(f"lock: {exc}")
        completed = datetime.now(tz=UTC)
        report.completed_at = completed
        report.duration_seconds = (completed - started).total_seconds()
        return report

    completed = datetime.now(tz=UTC)
    report.completed_at = completed
    report.duration_seconds = (completed - started).total_seconds()
    return report


# -- internals --------------------------------------------------------------


def _run_refresh_locked(
    store: DuckDBStore,
    *,
    report: RefreshReport,
    only_class_id: str | None,
    force: bool,
    max_workers: int,
    trace_dir: Path,
    rng: np.random.Generator | None,
    started: datetime,
    refresh_id: str,
) -> None:
    """The actual refresh work, executed under the file lock."""
    # Sync the registry first so pl_event_classes reflects the live set.
    with store.connection() as conn:
        delta = registry.sync_to_store(conn, when=started)
    report.class_delta = delta

    bump_reason = _resolve_bump_reason(delta=delta, force=force)
    if bump_reason is not None:
        affected = tuple(delta.added) + tuple(delta.removed) + tuple(delta.definition_changed)
        with store.connection() as conn:
            bump = bump_for_reason(
                conn,
                reason=bump_reason,
                affected_class_ids=affected,
                when=started,
            )
        report.library_version = bump.library_version
        report.bump_reason = bump_reason

    # Resolve the working set: all registered classes, optionally
    # filtered to a single id.
    classes = registry.get_all()
    if only_class_id is not None:
        classes = tuple(c for c in classes if c.class_id == only_class_id)
        if not classes:
            report.errors.append(
                f"only_class_id={only_class_id!r} did not match any registered class"
            )

    if not classes:
        _record_refresh_log(store=store, report=report, refresh_id=refresh_id, started=started)
        return

    if max_workers <= 1 or len(classes) <= 1:
        for cls in classes:
            outcome = _refresh_one_class(
                store=store,
                cls=cls,
                library_version=report.library_version,
                trace_dir=trace_dir,
                rng=rng,
                started=started,
            )
            report.classes.append(outcome)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _refresh_one_class,
                    store=store,
                    cls=cls,
                    library_version=report.library_version,
                    trace_dir=trace_dir,
                    rng=rng,
                    started=started,
                ): cls.class_id
                for cls in classes
            }
            for fut in as_completed(futures):
                class_id = futures[fut]
                try:
                    outcome = fut.result()
                except Exception as exc:
                    logger.exception("refresh: pool returned unhandled error for %s", class_id)
                    report.errors.append(f"pool_error[{class_id}]: {type(exc).__name__}: {exc}")
                    continue
                report.classes.append(outcome)

    # Stable ordering for the report regardless of executor scheduling.
    report.classes.sort(key=lambda o: o.class_id)
    _record_refresh_log(store=store, report=report, refresh_id=refresh_id, started=started)


def _refresh_one_class(
    *,
    store: DuckDBStore,
    cls: EventClass,
    library_version: int,
    trace_dir: Path,
    rng: np.random.Generator | None,
    started: datetime,
) -> ClassRefreshOutcome:
    """Run the full per-class pipeline. Returns a typed outcome."""
    class_started = time.monotonic()
    outcome = ClassRefreshOutcome(
        class_id=cls.class_id,
        status="ok",
        duration_seconds=0.0,
    )
    rng_local = rng or np.random.default_rng(seed=_class_seed(cls.class_id))

    try:
        # Stage 1: occurrences.
        with store.connection() as conn:
            occurrences_df = cls.occurrence_query(conn)

        outcomes = _build_outcome_records(cls=cls, df=occurrences_df, when=started)
        with store.connection() as conn:
            outcome.occurrences_persisted = upsert_outcomes(
                conn,
                outcomes,
                library_version=library_version,
                definition_version=cls.definition_version,
                when=started,
            )

        # Stage 2: base rate.
        with store.connection() as conn:
            base_rate = compute_base_rate(
                conn,
                cls,
                library_version=library_version,
                now=started,
            )
            upsert_base_rate(conn, base_rate)
        outcome.base_rate_computed = True
        if base_rate.low_sample_warning:
            outcome.warnings.append("low_sample")
        if base_rate.source_stale_warning:
            outcome.warnings.append("source_stale")

        # Stage 3: signatures + multi-variable combination.
        signatures_tuple: tuple[Any, ...] = ()
        if cls.precursors:
            with store.connection() as conn:
                signatures_tuple, _samples = compute_signature(
                    conn,
                    cls,
                    outcomes=outcomes,
                    library_version=library_version,
                    rng=rng_local,
                    now=started,
                )
                for sig in signatures_tuple:
                    upsert_signature(conn, sig)
            outcome.signatures_computed = len(signatures_tuple)
            if any(s.low_confidence_warning for s in signatures_tuple):
                outcome.warnings.append("low_confidence_signatures")

        # Stage 4: analogue feature space.
        if cls.analogue_features:
            with store.connection() as conn:
                space, rows = populate_feature_space(
                    conn,
                    cls,
                    outcomes=outcomes,
                    library_version=library_version,
                    rng=rng_local,
                    now=started,
                )
                if rows:
                    upsert_analogue_features(conn, space=space, rows=list(rows), when=started)
            outcome.analogue_points_persisted = space.point_count

        # Stage 5: calibration (when sample size permits).
        if len(outcomes) >= MIN_OCCURRENCES_FOR_CALIBRATION:
            calibration = compute_calibration(
                class_id=cls.class_id,
                library_version=library_version,
                definition_version=cls.definition_version,
                outcomes=outcomes,
                signatures=signatures_tuple,
                baseline_size=cls.baseline_sample_size,
                trace_dir=trace_dir,
                now=started,
            )
            with store.connection() as conn:
                upsert_calibration(conn, calibration)
            outcome.calibration_status = (
                "insufficient_data" if calibration.method == "insufficient_data" else "computed"
            )
        else:
            calibration = compute_calibration(
                class_id=cls.class_id,
                library_version=library_version,
                definition_version=cls.definition_version,
                outcomes=outcomes,
                signatures=signatures_tuple,
                baseline_size=cls.baseline_sample_size,
                trace_dir=trace_dir,
                now=started,
            )
            with store.connection() as conn:
                upsert_calibration(conn, calibration)
            outcome.calibration_status = "insufficient_data"

        # Final: stamp the class with its evaluation timestamp.
        with store.connection() as conn:
            record_class_evaluation(
                conn,
                class_id=cls.class_id,
                library_version=library_version,
                when=started,
            )
    except Exception as exc:
        logger.exception("refresh failed for class %s", cls.class_id)
        outcome.status = "failed"
        outcome.errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        outcome.duration_seconds = time.monotonic() - class_started

    return outcome


def _build_outcome_records(
    *,
    cls: EventClass,
    df: object,
    when: datetime,
) -> list[OutcomeRecord]:
    """Convert the occurrence_query DataFrame into typed OutcomeRecord rows.

    The DataFrame must contain at minimum an ``occurrence_ts`` column.
    Optional columns (``description``, ``end_ts``, ``source_records``)
    are surfaced when present. Each occurrence_id is a deterministic
    hash of class_id + occurrence_ts so re-runs are idempotent.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"occurrence_query must return a pandas DataFrame, got {type(df).__name__}")
    if "occurrence_ts" not in df.columns:
        raise ValueError(
            f"occurrence_query for class {cls.class_id!r} must include an 'occurrence_ts' column"
        )

    records: list[OutcomeRecord] = []
    for _row_idx, row in df.iterrows():
        occurrence_ts_raw = row["occurrence_ts"]
        occurrence_ts = pd.to_datetime(occurrence_ts_raw, utc=True, errors="coerce")
        if pd.isna(occurrence_ts):
            continue
        if hasattr(occurrence_ts, "to_pydatetime"):
            occurrence_dt: datetime = occurrence_ts.to_pydatetime()
        else:
            occurrence_dt = occurrence_ts

        end_ts = None
        if "end_ts" in df.columns:
            raw_end = row["end_ts"]
            if not pd.isna(raw_end):
                end_pd = pd.to_datetime(raw_end, utc=True, errors="coerce")
                if not pd.isna(end_pd) and hasattr(end_pd, "to_pydatetime"):
                    end_ts = end_pd.to_pydatetime()

        description: str | None = None
        if "description" in df.columns and not pd.isna(row.get("description")):
            description = str(row["description"])

        source_records_field: tuple[dict[str, str], ...] = ()
        if "source_records" in df.columns:
            raw_sr = row["source_records"]
            if isinstance(raw_sr, list):
                source_records_field = tuple(
                    {str(k): str(v) for k, v in item.items()}
                    for item in raw_sr
                    if isinstance(item, dict)
                )

        occurrence_id = _occurrence_id(cls.class_id, occurrence_dt)
        records.append(
            OutcomeRecord(
                class_id=cls.class_id,
                occurrence_id=occurrence_id,
                occurrence_ts=occurrence_dt,
                end_ts=end_ts,
                description=description,
                source_records=source_records_field,
            )
        )
    return records


def _occurrence_id(class_id: str, occurrence_ts: datetime) -> str:
    """Deterministic occurrence id keyed on (class_id, ISO timestamp)."""
    raw = f"{class_id}:{occurrence_ts.isoformat()}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _class_seed(class_id: str) -> int:
    """Deterministic seed per class so refreshes are reproducible."""
    return int.from_bytes(hashlib.sha256(class_id.encode("utf-8")).digest()[:4], "big")


def _resolve_bump_reason(*, delta: ClassDelta, force: bool) -> str | None:
    """Map the registry diff to a ``pl_library_versions.bump_reason`` value."""
    if delta.added:
        return BumpReason.CLASS_ADDED
    if delta.definition_changed:
        return BumpReason.CLASS_MODIFIED
    if delta.removed:
        return BumpReason.CLASS_REMOVED
    if force:
        return BumpReason.CODE_CHANGE
    return None


def _record_refresh_log(
    *,
    store: DuckDBStore,
    report: RefreshReport,
    refresh_id: str,
    started: datetime,
) -> None:
    """Append the refresh log row that summarises this run."""
    classes_payload: list[dict[str, Any]] = []
    for outcome in report.classes:
        classes_payload.append(
            {
                "class_id": outcome.class_id,
                "status": outcome.status,
                "duration_seconds": outcome.duration_seconds,
                "occurrences_persisted": outcome.occurrences_persisted,
                "signatures_computed": outcome.signatures_computed,
                "analogue_points_persisted": outcome.analogue_points_persisted,
                "calibration_status": outcome.calibration_status,
                "warnings": outcome.warnings,
                "errors": outcome.errors,
            }
        )
    error_summary: dict[str, Any] | None = None
    if report.errors:
        error_summary = {"refresh_errors": report.errors}

    ended = datetime.now(tz=UTC)
    with store.connection() as conn:
        record_refresh(
            conn,
            refresh_id=refresh_id,
            started_at=started,
            ended_at=ended,
            library_version=report.library_version,
            classes_processed=classes_payload,
            error_summary=error_summary,
        )


@contextmanager
def _acquire_refresh_lock(lock_path: Path) -> Iterator[None]:
    """File-based lock with a short polling loop.

    Uses ``os.O_EXCL`` to reserve the lock atomically. Waits up to
    :data:`LOCK_ACQUIRE_TIMEOUT_SECONDS` for an existing lock to
    release; gives up with :class:`RefreshLockHeldError` on timeout.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_ACQUIRE_TIMEOUT_SECONDS
    fd: int | None = None
    while True:
        try:
            fd = os.open(
                str(lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                mode=0o600,
            )
            break
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            if time.monotonic() >= deadline:
                raise RefreshLockHeldError(
                    f"refresh lock at {lock_path} is held; another refresh is in progress"
                ) from None
            time.sleep(0.25)
    try:
        os.write(fd, f"pid={os.getpid()} acquired={datetime.now(tz=UTC).isoformat()}\n".encode())
        yield
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()
