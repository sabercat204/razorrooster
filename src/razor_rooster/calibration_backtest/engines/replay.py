"""Replay-loop building blocks (T-CB-017; design §3.5).

This module hosts the per-prediction orchestration helper used by the
calibration_backtest replay loop. T-CB-017 lands :func:`evaluate_class_at_frozen_time`,
the thin wrapper that bridges the freezer's time-honesty contract with
``signal_scanner``'s public posterior pipeline. T-CB-018 will extend this
module with :func:`run_backtest` (the main resolution-enumeration loop)
and T-CB-019 will wire the persistence + trace-encoding plumbing.

Why a wrapper rather than calling ``signal_scanner.evaluate_class``
directly? The live scanner pipeline is opinionated about "now": it
records ``scan_started_at`` and ``scan_completed_at``, picks a candidate
direction, persists ``scan_records``/``scan_traces`` rows, and emits the
``ScanRecord`` shape required by the scanner schema. The backtest only
needs the **scoring inputs** — ``model_p`` (the posterior point estimate)
plus a JSON-serialisable trace dict — and absolutely must not mutate
scanner persistence tables during a replay. Reusing
``signal_scanner.engines.posterior.evaluate_precursors_at_time`` (the
public, time-frozen wrapper around ``_evaluate_precursors``) and
``signal_scanner.engines.posterior.posterior_with_ci`` keeps the
backtest non-divergent from the live scoring path while sidestepping
the orchestration sugar (T-CB-017 design rationale, OQ-CB-001).

The function returns ``(model_p, trace)`` where:

* ``model_p`` is the posterior point estimate as a plain ``float``
  (``PosteriorResult.posterior``); the rest of ``PosteriorResult`` is
  not propagated because the calibration scoring engine consumes the
  point only (Brier scoring per design §3.6).
* ``trace`` is the dict produced by
  :func:`signal_scanner.engines.trace.build_trace`, the same payload
  the live scanner would have emitted given identical inputs. Reusing
  the scanner's trace builder guarantees backtest traces and live-scan
  traces share a single schema, which keeps the report_generator's
  trace renderer reusable and locks in the "anti-divergence" contract
  (T-CB-017 verification).

Failure modes:

* ``InsufficientPrecursorData`` — fewer than ``min_support`` non-``None``
  current values were obtained from
  :func:`evaluate_precursors_at_time`. Caller (``run_backtest``) maps
  this to ``skip_reason='insufficient_data'`` (design §3.13).
* ``BacktestConfigError`` — the requested ``class_id`` is not in the
  pattern_library registry, or ``min_support < 1``. Both indicate a
  configuration/wiring bug, not a recoverable data issue.
* Other exceptions propagate — the replay loop wraps each per-prediction
  call in a ``try/except Exception`` for failure isolation
  (REQ-CB-RUN-005); we do not swallow unexpected errors here.

Note on ``min_support``: the pattern_library's :class:`EventClass` does
not currently expose a ``min_support`` attribute, so this wrapper accepts
``min_support`` as a keyword-only parameter (default ``1`` — at least one
non-``None`` precursor value). The replay loop (T-CB-018) will thread
this from the run configuration; tests can pin it explicitly. Defining
the floor here (rather than reaching into pattern_library) keeps T-CB-017
self-contained and avoids cross-subsystem schema churn during Phase 3.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import duckdb

from razor_rooster.calibration_backtest import version as version_module
from razor_rooster.calibration_backtest.engines import freezer as freezer_module
from razor_rooster.calibration_backtest.engines import polarity as polarity_module
from razor_rooster.calibration_backtest.engines import trace_codec
from razor_rooster.calibration_backtest.engines.freezer import FrozenState
from razor_rooster.calibration_backtest.engines.lag import (
    derive_prediction_ts,
    validate_lag,
)
from razor_rooster.calibration_backtest.errors import (
    BacktestConfigError,
    CalibrationBacktestError,
    InsufficientPrecursorData,
    NoPolarityError,
    RecentWindowError,
)
from razor_rooster.calibration_backtest.models import (
    BacktestPrediction,
    BacktestRun,
    BacktestStatus,
    BacktestTrace,
    CompressionAlgorithm,
    PolaritySource,
    PolarityValue,
    PredictionStatus,
    RunParameters,
    SkipReason,
)
from razor_rooster.calibration_backtest.persistence import operations as persistence
from razor_rooster.calibration_backtest.run_id import compute_run_id_for_params
from razor_rooster.pattern_library import library, registry
from razor_rooster.pattern_library.models.base_rate import BaseRateResult
from razor_rooster.pattern_library.models.event_class import EventClass
from razor_rooster.pattern_library.models.signature import SignatureResult
from razor_rooster.pattern_library.version import (
    current_version as _pattern_library_current_version,
)
from razor_rooster.signal_scanner.engines.posterior import (
    PosteriorResult,
    posterior_with_ci,
)
from razor_rooster.signal_scanner.engines.posterior import (
    evaluate_precursors_at_time as _evaluate_precursors_at_time,
)
from razor_rooster.signal_scanner.engines.trace import build_trace

if TYPE_CHECKING:
    from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore


__all__ = [
    "DEFAULT_FALLBACK_BIN_COUNT",
    "DEFAULT_MIN_SUPPORT",
    "DEFAULT_RECENT_WINDOW_DAYS",
    "MappedResolution",
    "ReplayResult",
    "evaluate_class_at_frozen_time",
    "freezer_module",
    "iter_mapped_resolutions",
    "library",
    "polarity_correct",
    "polarity_module",
    "registry",
    "run_backtest",
    "validate_lag",
]

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

DEFAULT_RECENT_WINDOW_DAYS: Final[int] = 30
"""Recent-window guard horizon in days (REQ-CB-RUN-002, design §3.5).

When ``params.until_ts`` lands within this many days of ``now()`` and
``params.allow_recent`` is ``False``, :func:`run_backtest` raises
:class:`RecentWindowError` before any persistence side-effects fire.
``--allow-recent`` clears the guard and the run row records
``allow_recent=True`` for auditability.
"""


DEFAULT_MIN_SUPPORT: int = 1
"""Default minimum number of non-``None`` precursor values required to score.

The replay loop (T-CB-018) overrides this from run configuration; tests
pin it explicitly. The default of ``1`` keeps the wrapper permissive: any
prediction with at least one observable precursor proceeds to the
posterior, while a class whose every precursor is missing at
``prediction_ts`` is correctly skipped with ``insufficient_data``.
"""


def evaluate_class_at_frozen_time(
    class_id: str,
    prediction_ts: datetime,
    frozen: FrozenState,
    *,
    store: DuckDBStore,
    library_version: int | None = None,
    min_support: int = DEFAULT_MIN_SUPPORT,
    n_samples: int | None = None,
    co_occurrence_correction: float = 0.0,
) -> tuple[float, dict[str, Any]]:
    """Score one ``(class_id, prediction_ts)`` pair against frozen-time data.

    Pipeline (design §3.5, T-CB-017 deliverables):

    1. Resolve the :class:`EventClass` from
       :mod:`razor_rooster.pattern_library.registry` by ``class_id``.
       The registry is the only source of truth for the in-Python class
       definition (precursor variables, prior alpha/beta, etc.); the
       ``pl_event_classes`` table mirrored by ``library.list_classes``
       carries metadata but not the class's queries, so the registry
       lookup is required.
    2. Pull the latest persisted base rate and signatures for the class
       via the pattern_library facade (``library.base_rate``,
       ``library.signature``), pinned to ``library_version`` when one is
       supplied.
    3. Call
       :func:`signal_scanner.engines.posterior.evaluate_precursors_at_time`
       with ``as_of_ts=prediction_ts``. The scanner's public wrapper is
       responsible for honouring the freezer's
       ``source_publication_ts <= prediction_ts`` contract on every
       underlying precursor query (T-CB-017 prerequisite sub-task).
    4. Count non-``None`` entries in the returned ``current_values``.
       If the count is below ``min_support``, raise
       :class:`InsufficientPrecursorData` with structured context so the
       replay loop can record an ``insufficient_data`` skip row.
    5. Call :func:`signal_scanner.engines.posterior.posterior_with_ci`
       (unchanged, per OQ-CB-001) to obtain a :class:`PosteriorResult`.
       Extract ``posterior_result.posterior`` as the model probability
       (a plain ``float``); the CI bounds and other PosteriorResult
       fields are still recorded inside the returned trace, but the
       calibration scoring engine consumes the point only.
    6. Build a trace dict via
       :func:`signal_scanner.engines.trace.build_trace` so backtest
       traces share the live-scan schema. ``data_as_of`` is set to
       ``prediction_ts`` (the frozen-time horizon), ``library_version``
       is the resolved version, and the candidate gates are not
       evaluated (the backtest doesn't trade, so candidacy flags
       default to ``False`` / ``None``).

    Args:
        class_id: Pattern-library class identifier.
        prediction_ts: Simulated decision instant (timezone-aware).
        frozen: :class:`FrozenState` from
            :func:`razor_rooster.calibration_backtest.engines.freezer.freeze`.
            Carries the ``source_publication_ts_boundary`` echoing
            ``prediction_ts`` and the registered-source set; the wrapper
            does not re-query the boundary, but the parameter is kept
            non-optional so the caller cannot accidentally bypass the
            freezer guard. The replay loop (T-CB-018) is responsible for
            calling :func:`freeze` and short-circuiting with
            ``source_data_not_frozen`` when it returns ``None`` —
            ``frozen`` reaching this function therefore implies a
            successfully frozen state.
        store: DuckDB store with data_ingest, pattern_library, and
            signal_scanner schemas applied.
        library_version: Optional library version to pin base-rate and
            signature lookups against. ``None`` means "use the latest
            persisted row regardless of library version" — the same
            semantics as ``signal_scanner.engines.scanner.evaluate_class``
            falls back to when its primary lookup misses.
        min_support: Minimum number of non-``None`` ``current_values``
            entries required to proceed to the posterior. Defaults to
            :data:`DEFAULT_MIN_SUPPORT` (``1``); the replay loop will
            thread this from run configuration.
        n_samples: Monte Carlo sample count forwarded to
            :func:`posterior_with_ci`. ``None`` uses the scanner default
            (``signal_scanner.engines.posterior.DEFAULT_MONTE_CARLO_SAMPLES``).
        co_occurrence_correction: Log-odds adjustment forwarded to
            :func:`posterior_with_ci`; defaults to ``0.0`` (no
            correction) matching the v1 scanner default.

    Returns:
        ``(model_p, trace)`` — ``model_p`` is the posterior point
        estimate as a ``float`` in ``[1e-6, 1 - 1e-6]`` (the posterior
        engine clips its output); ``trace`` is the JSON-serialisable
        dict from :func:`build_trace`, ready for downstream encoding by
        :mod:`razor_rooster.calibration_backtest.engines.trace_codec`.

    Raises:
        BacktestConfigError: If ``class_id`` is not registered, if
            ``min_support < 1``, or if no base rate is persisted for the
            class (a class with no base rate cannot produce a meaningful
            posterior; surfacing this as a configuration error rather
            than ``InsufficientPrecursorData`` keeps the closed
            ``skip_reason`` enumeration intact — missing base rates map
            to ``exception`` in the replay loop, not ``insufficient_data``).
        InsufficientPrecursorData: If fewer than ``min_support`` non-``None``
            entries were obtained from
            :func:`evaluate_precursors_at_time`.
    """
    if min_support < 1:
        raise BacktestConfigError(
            f"evaluate_class_at_frozen_time: min_support must be >= 1, got {min_support!r}"
        )

    cls = _resolve_class(class_id)
    base_rate, signatures, resolved_library_version = _load_pattern_library_artefacts(
        store=store, class_id=class_id, library_version=library_version
    )

    current_values, _source_stale = _evaluate_precursors_at_time(
        store, cls, signatures, as_of_ts=prediction_ts
    )
    support = _support_count(current_values)
    if support < min_support:
        raise InsufficientPrecursorData(
            "fewer non-null precursor values than min_support: "
            f"class_id={class_id!r} prediction_ts={prediction_ts.isoformat()!r} "
            f"observed={support} required={min_support} "
            f"frozen_boundary={frozen.source_publication_ts_boundary.isoformat()!r}"
        )

    posterior = _compute_posterior(
        base_rate=base_rate,
        signatures=signatures,
        current_values=current_values,
        n_samples=n_samples,
        co_occurrence_correction=co_occurrence_correction,
    )
    model_p = float(posterior.posterior)

    trace = build_trace(
        cls=cls,
        base_rate=base_rate,
        signatures=signatures,
        current_values=current_values,
        posterior=posterior,
        is_candidate=False,
        candidate_direction=None,
        warnings=(),
        library_version=resolved_library_version,
        data_as_of=prediction_ts,
    )
    return model_p, trace


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_class(class_id: str) -> EventClass:
    """Return the registered :class:`EventClass`, raising config errors clearly.

    The pattern_library registry holds the live in-Python class objects
    (with their query callables, prior parameters, and precursor
    metadata). A missing class typically means the run was configured
    against a class id that has been removed from the library; surfacing
    this as :class:`BacktestConfigError` lets the CLI exit with a
    structured message rather than letting a bare ``KeyError`` escape.
    """
    if not registry.is_registered(class_id):
        raise BacktestConfigError(
            f"evaluate_class_at_frozen_time: class_id {class_id!r} is not registered "
            "in pattern_library.registry"
        )
    return registry.get(class_id)


def _load_pattern_library_artefacts(
    *,
    store: DuckDBStore,
    class_id: str,
    library_version: int | None,
) -> tuple[BaseRateResult, tuple[SignatureResult, ...], int]:
    """Load persisted base rate + signatures, pinned to ``library_version`` when given.

    Mirrors the fallback path used by
    ``signal_scanner.engines.scanner.evaluate_class``: when the version-
    pinned lookup misses (e.g. a class that has not been re-evaluated at
    the requested version), we retry without a version pin and use the
    latest persisted row. The returned ``resolved_library_version`` is
    the version actually carried by the base rate, so the trace records
    the truthful version — not the operator's request — and downstream
    consumers can detect drift.
    """
    base_rate = library.base_rate(store, class_id, library_version=library_version)
    if base_rate is None and library_version is not None:
        base_rate = library.base_rate(store, class_id)
    if base_rate is None:
        raise BacktestConfigError(
            f"evaluate_class_at_frozen_time: no persisted base rate for class_id={class_id!r}"
        )
    resolved_version = base_rate.library_version
    signatures = library.signature(store, class_id, library_version=resolved_version)
    return base_rate, tuple(signatures), resolved_version


def _support_count(current_values: Mapping[str, float | None]) -> int:
    """Return the count of non-``None`` entries in ``current_values``.

    This is the operative "rows" definition for the
    ``insufficient_data`` skip reason: a precursor whose query returned
    no admissible row (``None``) cannot influence the posterior, so it
    does not count toward minimum support. Variables that returned a
    value of ``0.0`` *do* count — zero is a valid observation, only
    ``None`` indicates absence.
    """
    return sum(1 for value in current_values.values() if value is not None)


def _compute_posterior(
    *,
    base_rate: BaseRateResult,
    signatures: tuple[SignatureResult, ...],
    current_values: Mapping[str, float | None],
    n_samples: int | None,
    co_occurrence_correction: float,
) -> PosteriorResult:
    """Forward to :func:`posterior_with_ci` honouring optional ``n_samples`` override.

    Centralises the ``None``-vs-default handling so the call site stays
    free of conditional kwarg construction; if a future task adds another
    optional knob (e.g. seeded RNG threading from the replay run), this
    helper is the single edit point.
    """
    kwargs: dict[str, Any] = {"co_occurrence_correction": co_occurrence_correction}
    if n_samples is not None:
        kwargs["n_samples"] = n_samples
    return posterior_with_ci(
        base_rate,
        signatures,
        current_values=current_values,
        **kwargs,
    )


# ===========================================================================
# T-CB-018 — Main replay loop
# ===========================================================================
#
# The replay loop walks ``polymarket_resolutions`` in ascending
# ``resolution_ts`` (pre-filtered to in-scope ``class_ids`` via a JOIN on
# ``class_market_mappings``), and for each (resolution, mapping) pair:
#
#   1. Honours ``resolution.invalidated`` — skips with ``invalid_resolution``.
#   2. Derives ``prediction_ts`` via :func:`derive_prediction_ts` and
#      validates the lag floor via :func:`validate_lag`.
#   3. Calls :func:`freezer.freeze` to acquire a :class:`FrozenState`; a
#      ``None`` return short-circuits with ``source_data_not_frozen``.
#   4. Resolves polarity via :func:`polarity.resolve` (Tier 1 / Tier 2 /
#      Tier 3); :class:`NoPolarityError` short-circuits with
#      ``no_polarity_resolution``.
#   5. Evaluates the class at frozen time via
#      :func:`evaluate_class_at_frozen_time`;
#      :class:`InsufficientPrecursorData` short-circuits with
#      ``insufficient_data``.
#   6. Polarity-corrects the Polymarket outcome via :func:`polarity_correct`
#      to derive the ``observed`` bit.
#
# Per REQ-CB-RUN-005, every per-prediction call site is wrapped in a
# ``try/except Exception`` so an uncaught error becomes
# ``skip_reason='exception'`` and never aborts the run.
#
# This task (T-CB-018) intentionally does **not** wire the persistence
# layer — predictions are collected in an in-memory buffer on
# :class:`ReplayResult` and a :class:`BacktestRun` is synthesised from
# the buffered counters. T-CB-019 promotes the buffer to
# ``persistence.insert_prediction``/``insert_trace`` calls and wires
# ``persistence.complete_run`` for summary aggregation. Keeping the
# loop persistence-free here lets the integration test exercise the
# orchestration semantics (skip-reason routing, recent-window guard,
# parallel fanout) without depending on the calibration-backtest schema
# being applied to the test connection.


# ---------------------------------------------------------------------------
# Resolution + mapping iterator (design §3.5)
# ---------------------------------------------------------------------------

_ITER_MAPPED_RESOLUTIONS_SQL: Final[str] = """
SELECT
  r.condition_id,
  r.resolution_ts,
  r.invalidated,
  r.winning_outcome_label,
  cm.class_id,
  cm.polarity,
  cm.venue
FROM polymarket_resolutions AS r
JOIN class_market_mappings AS cm
  ON cm.condition_id = r.condition_id
WHERE r.resolution_ts >= ?
  AND r.resolution_ts <= ?
  AND r.superseded_at IS NULL
  AND cm.removed_at IS NULL
""".strip()


@dataclass(frozen=True, slots=True)
class MappedResolution:
    """One ``(polymarket_resolutions, class_market_mappings)`` JOIN row.

    Yielded by :func:`iter_mapped_resolutions` per-resolution-per-mapping
    so the replay loop can drive its inner per-prediction work without
    re-issuing SQL. ``polarity`` is the raw mapping polarity string
    stored on ``class_market_mappings`` (``'aligned'``/``'inverted'`` in
    the v1 schema); the resolver may override this with a
    contemporaneous polarity from ``comparison_resolutions`` (Tier 1).
    """

    condition_id: str
    resolution_ts: datetime
    invalidated: bool
    winning_outcome_label: str | None
    class_id: str
    mapping_polarity: str
    venue: str


def iter_mapped_resolutions(
    conn: duckdb.DuckDBPyConnection,
    since_ts: datetime,
    until_ts: datetime,
    venues: Sequence[str],
    class_ids: Sequence[str],
) -> Iterator[MappedResolution]:
    """Yield in-scope ``(resolution, mapping)`` rows ordered by ``resolution_ts``.

    Pre-filters ``polymarket_resolutions`` to the requested replay
    window via a JOIN on ``class_market_mappings`` so resolutions
    without an active mapping into ``class_ids`` are dropped at the SQL
    layer (design §3.5: "the inner mapping loop never runs zero times
    for a yielded resolution"). ``superseded_at IS NULL`` and
    ``removed_at IS NULL`` enforce the "current truth" semantics for
    both sides of the JOIN.

    DuckDB's parameterised ``IN (?)`` syntax does not accept a Python
    sequence directly, so we splice ``len(venues)`` and ``len(class_ids)``
    placeholders inline; the values themselves are still bound as
    parameters so SQL injection is impossible. The trailing
    ``ORDER BY r.resolution_ts ASC`` matches design §3.5's
    "ascending resolution_ts" iteration contract.

    Args:
        conn: Open DuckDB connection on which ``polymarket_resolutions``
            (from ``polymarket_connector``) and ``class_market_mappings``
            (from ``mispricing_detector``) are visible.
        since_ts, until_ts: Inclusive replay window (timezone-aware).
        venues: Non-empty sequence of venue identifiers; the JOIN filters
            ``cm.venue IN (...)``.
        class_ids: Non-empty sequence of pattern_library class
            identifiers; the JOIN filters ``cm.class_id IN (...)``.

    Yields:
        :class:`MappedResolution` for every JOIN row, ordered by
        ``resolution_ts`` ASC. Empty sequence when nothing matches.

    Raises:
        BacktestConfigError: If ``venues`` or ``class_ids`` is empty.
    """
    if not venues:
        raise BacktestConfigError("iter_mapped_resolutions: venues must be non-empty")
    if not class_ids:
        raise BacktestConfigError("iter_mapped_resolutions: class_ids must be non-empty")

    venue_placeholders = ", ".join(["?"] * len(venues))
    class_placeholders = ", ".join(["?"] * len(class_ids))
    sql = (
        f"{_ITER_MAPPED_RESOLUTIONS_SQL}\n"
        f"  AND cm.venue IN ({venue_placeholders})\n"
        f"  AND cm.class_id IN ({class_placeholders})\n"
        "ORDER BY r.resolution_ts ASC"
    )
    params: list[Any] = [since_ts, until_ts, *venues, *class_ids]
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        yield MappedResolution(
            condition_id=str(row[0]),
            resolution_ts=row[1],
            invalidated=bool(row[2]),
            winning_outcome_label=(str(row[3]) if row[3] is not None else None),
            class_id=str(row[4]),
            mapping_polarity=str(row[5]),
            venue=str(row[6]),
        )


# ---------------------------------------------------------------------------
# Polarity correction (design §3.5)
# ---------------------------------------------------------------------------

_POLARITY_DIRECT: Final[frozenset[str]] = frozenset({"aligned", "direct", "forward"})
"""Tokens treated as direct polarity (yes-resolution => observed=1.0).

Multiple synonymous strings exist across the upstream tables / specs:
``aligned`` is what ``mispricing_detector`` writes today; ``direct``
is what the v1 calibration schema CHECK constraint expects; ``forward``
is the in-Python :class:`PolarityValue.FORWARD` enum value. Accepting
all three keeps the replay loop tolerant of in-flight schema drift.
"""

_POLARITY_INVERTED: Final[frozenset[str]] = frozenset({"inverted"})
"""Token treated as inverted polarity (no-resolution => observed=1.0)."""


def polarity_correct(winning_outcome_label: str | None, polarity_value: str) -> float:
    """Return the polarity-corrected ``observed`` bit for one prediction.

    Implements the table from ``mispricing_detector.engines.linkage``:

    * ``polarity == 'aligned'`` (or ``'direct'``/``'forward'``):
      ``'yes'`` -> ``1.0``, ``'no'`` -> ``0.0``.
    * ``polarity == 'inverted'``: ``'yes'`` -> ``0.0``,
      ``'no'`` -> ``1.0``.
    * ``winning_outcome_label is None`` (a void/unsettled resolution
      that escaped the ``invalidated`` filter) -> ``0.0`` regardless
      of polarity. The replay loop's ``invalidated`` check usually
      catches these upstream; the defensive zero here keeps the
      Brier scoring engine total-honest.

    Raises :class:`BacktestConfigError` for unknown polarity strings so
    a schema rename surfaces immediately rather than silently mapping
    every row to ``0.0``.
    """
    if winning_outcome_label is None:
        return 0.0
    label = winning_outcome_label.strip().lower()
    if polarity_value in _POLARITY_DIRECT:
        return 1.0 if label == "yes" else 0.0
    if polarity_value in _POLARITY_INVERTED:
        return 1.0 if label == "no" else 0.0
    raise BacktestConfigError(
        f"polarity_correct: unrecognised polarity_value {polarity_value!r}; "
        f"expected one of {sorted(_POLARITY_DIRECT | _POLARITY_INVERTED)!r}"
    )


def _polarity_value_enum(polarity_value: str) -> PolarityValue:
    """Map a raw polarity string to the :class:`PolarityValue` enum.

    Persistence-layer type alignment: ``BacktestPrediction.polarity`` is
    typed as :class:`PolarityValue | None`. The replay loop normalises
    the upstream ``aligned``/``inverted`` strings to the in-Python enum
    so the scored row can be constructed cleanly. ``aligned`` and
    ``direct`` both map to :data:`PolarityValue.FORWARD` since the v1
    enum uses the ``forward`` literal for the non-inverted bucket;
    when the schema CHECK migrates to ``direct``/``inverted`` the enum
    can be renamed without changing this mapping (T-CB-019 follow-up).
    """
    if polarity_value in _POLARITY_DIRECT:
        return PolarityValue.FORWARD
    if polarity_value in _POLARITY_INVERTED:
        return PolarityValue.INVERTED
    raise BacktestConfigError(
        f"_polarity_value_enum: unrecognised polarity_value {polarity_value!r}"
    )


def _polarity_source_enum(source: str) -> PolaritySource:
    """Map a raw polarity-source sentinel to the :class:`PolaritySource` enum."""
    if source == polarity_module.SOURCE_COMPARISON_RESOLUTIONS:
        return PolaritySource.COMPARISON_RESOLUTIONS
    if source == polarity_module.SOURCE_CURRENT_MAPPING_FALLBACK:
        return PolaritySource.CURRENT_MAPPING_FALLBACK
    raise BacktestConfigError(f"_polarity_source_enum: unrecognised polarity source {source!r}")


# ---------------------------------------------------------------------------
# Per-prediction work + result aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PredictionOutcome:
    """In-memory result carrier produced by :func:`_evaluate_one_resolution`.

    Holds either a fully-scored prediction (``status=SCORED``,
    ``model_p`` and ``observed`` populated, ``trace`` non-``None``) or a
    skip row (``status=SKIPPED``, ``skip_reason`` populated). T-CB-019
    promotes the ``trace`` field into compressed-blob persistence; for
    T-CB-018 it stays in-memory on the buffered :class:`ReplayResult`.
    """

    prediction: BacktestPrediction
    trace: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Final output of :func:`run_backtest` (T-CB-018).

    Bundles the synthesised :class:`BacktestRun` row with the
    per-prediction buffer and the per-trace buffer so callers (tests,
    T-CB-019 wiring) can inspect the full replay outcome without
    requiring the persistence layer. The ``traces`` mapping is keyed
    by ``prediction_id`` and only contains entries for ``status=SCORED``
    rows; skipped predictions carry no trace.
    """

    run: BacktestRun
    predictions: tuple[BacktestPrediction, ...]
    traces: Mapping[str, dict[str, Any]]


def _make_prediction_id(run_id: str, condition_id: str, class_id: str, venue: str) -> str:
    """Compose a deterministic ``prediction_id`` for one replay attempt.

    Hashes ``(run_id, condition_id, class_id, venue)`` so two invocations
    with identical params produce identical ``prediction_id`` values;
    this keeps the persistence-layer composite PK ``(run_id, prediction_id)``
    stable across re-runs (REQ-CB-RUN-004). The 16-char prefix is short
    enough to fit comfortably in CLI tables yet long enough to avoid
    collisions across the v1 seed library's projected prediction count
    (~4000 attempts; collision probability under 2^-32).
    """
    digest = hashlib.sha256(f"{run_id}|{condition_id}|{class_id}|{venue}".encode()).hexdigest()
    return digest[:32]


def _skip_prediction(
    *,
    run_id: str,
    resolution: MappedResolution,
    prediction_ts: datetime,
    reason: SkipReason,
    polarity_source: PolaritySource = PolaritySource.NO_POLARITY,
    polarity: PolarityValue | None = None,
    mapping_mismatch_warning: bool = False,
    definition_version: int = 1,
    sector: str = "",
) -> _PredictionOutcome:
    """Build a ``status=SKIPPED`` :class:`BacktestPrediction` for one row."""
    prediction_id = _make_prediction_id(
        run_id, resolution.condition_id, resolution.class_id, resolution.venue
    )
    prediction = BacktestPrediction(
        run_id=run_id,
        prediction_id=prediction_id,
        class_id=resolution.class_id,
        condition_id=resolution.condition_id,
        venue=resolution.venue,
        sector=sector or resolution.class_id,
        prediction_ts=prediction_ts,
        resolution_ts=resolution.resolution_ts,
        model_p=None,
        observed=None,
        polarity=polarity,
        polarity_source=polarity_source,
        mapping_mismatch_warning=mapping_mismatch_warning,
        definition_version=definition_version,
        status=PredictionStatus.SKIPPED,
        skip_reason=reason,
        brier_contribution=None,
    )
    return _PredictionOutcome(prediction=prediction, trace=None)


def _resolve_sector(class_id: str) -> str:
    """Best-effort sector lookup via the pattern_library registry.

    The replay loop must populate ``BacktestPrediction.sector`` (a
    non-empty field per the dataclass contract). T-CB-018 keeps the
    lookup defensive: when the class is not registered (which only
    happens during early bootstrap, since the run-id machinery would
    normally reject an unregistered class upstream) we fall back to
    the class id itself so the row still validates. T-CB-026 promotes
    sector resolution into the proper bin-count machinery.
    """
    if registry.is_registered(class_id):
        cls = registry.get(class_id)
        sector_value = getattr(cls, "sector", None)
        if sector_value is not None:
            return str(sector_value)
    return class_id


def _evaluate_one_resolution(
    *,
    run_id: str,
    params: RunParameters,
    resolution: MappedResolution,
    conn: duckdb.DuckDBPyConnection,
    store: DuckDBStore,
    library_version: int | None,
    min_support: int,
) -> _PredictionOutcome:
    """Drive the per-prediction pipeline for a single mapped resolution.

    All recoverable failures map to a ``status=SKIPPED`` outcome with
    the appropriate :class:`SkipReason`; an uncaught exception becomes
    ``skip_reason='exception'`` so the run never aborts (REQ-CB-RUN-005).
    """
    sector = _resolve_sector(resolution.class_id)
    prediction_ts = derive_prediction_ts(resolution.resolution_ts, params.lag_days)

    try:
        # Step 1 — invalidated resolutions short-circuit before any work.
        if resolution.invalidated:
            return _skip_prediction(
                run_id=run_id,
                resolution=resolution,
                prediction_ts=prediction_ts,
                reason=SkipReason.INVALID_RESOLUTION,
                sector=sector,
            )

        # Step 2 — lag floor.
        if not validate_lag(resolution.resolution_ts, prediction_ts, params.lag_days):
            return _skip_prediction(
                run_id=run_id,
                resolution=resolution,
                prediction_ts=prediction_ts,
                reason=SkipReason.INSUFFICIENT_LAG,
                sector=sector,
            )

        # Step 3 — frozen state.
        frozen = freezer_module.freeze(conn, prediction_ts)
        if frozen is None:
            return _skip_prediction(
                run_id=run_id,
                resolution=resolution,
                prediction_ts=prediction_ts,
                reason=SkipReason.SOURCE_DATA_NOT_FROZEN,
                sector=sector,
            )

        # Step 4 — polarity resolution (Tier 1 / Tier 2 / Tier 3).
        try:
            polarity_value, polarity_source_raw = polarity_module.resolve(
                conn,
                prediction_ts,
                resolution.condition_id,
                resolution.class_id,
                venue=resolution.venue,
            )
        except NoPolarityError:
            return _skip_prediction(
                run_id=run_id,
                resolution=resolution,
                prediction_ts=prediction_ts,
                reason=SkipReason.NO_POLARITY_RESOLUTION,
                sector=sector,
            )

        polarity_enum = _polarity_value_enum(polarity_value)
        polarity_source_enum = _polarity_source_enum(polarity_source_raw)
        mapping_mismatch_warning = polarity_source_enum is PolaritySource.CURRENT_MAPPING_FALLBACK

        # Step 5 — class evaluation at frozen time.
        try:
            model_p, trace = evaluate_class_at_frozen_time(
                resolution.class_id,
                prediction_ts,
                frozen,
                store=store,
                library_version=library_version,
                min_support=min_support,
            )
        except InsufficientPrecursorData:
            return _skip_prediction(
                run_id=run_id,
                resolution=resolution,
                prediction_ts=prediction_ts,
                reason=SkipReason.INSUFFICIENT_PRECURSOR_DATA,
                polarity_source=polarity_source_enum,
                polarity=polarity_enum,
                mapping_mismatch_warning=mapping_mismatch_warning,
                sector=sector,
            )

        # Step 6 — polarity-correct the outcome and emit a SCORED row.
        observed = polarity_correct(resolution.winning_outcome_label, polarity_value)
        definition_version = _resolve_definition_version(trace)
        prediction_id = _make_prediction_id(
            run_id, resolution.condition_id, resolution.class_id, resolution.venue
        )
        prediction = BacktestPrediction(
            run_id=run_id,
            prediction_id=prediction_id,
            class_id=resolution.class_id,
            condition_id=resolution.condition_id,
            venue=resolution.venue,
            sector=sector,
            prediction_ts=prediction_ts,
            resolution_ts=resolution.resolution_ts,
            model_p=model_p,
            observed=observed,
            polarity=polarity_enum,
            polarity_source=polarity_source_enum,
            mapping_mismatch_warning=mapping_mismatch_warning,
            definition_version=definition_version,
            status=PredictionStatus.SCORED,
            skip_reason=None,
            brier_contribution=(model_p - observed) ** 2,
        )
        return _PredictionOutcome(prediction=prediction, trace=trace)

    except CalibrationBacktestError:
        # Subsystem-specific recoverable errors are already mapped above;
        # anything still escaping here indicates a configuration/wiring
        # bug (e.g. missing base rate, unknown polarity string). Fall
        # through to the generic exception path so the run records a
        # skip rather than crashing.
        raise
    except Exception as exc:  # REQ-CB-RUN-005 failure isolation
        _LOGGER.exception(
            "calibration_backtest.replay.exception",
            extra={
                "event": "per_prediction_exception",
                "run_id": run_id,
                "condition_id": resolution.condition_id,
                "class_id": resolution.class_id,
                "venue": resolution.venue,
                "exc_type": type(exc).__name__,
            },
        )
        return _skip_prediction(
            run_id=run_id,
            resolution=resolution,
            prediction_ts=prediction_ts,
            reason=SkipReason.EXCEPTION,
            sector=sector,
        )


def _resolve_definition_version(trace: Mapping[str, Any]) -> int:
    """Pull ``definition_version`` from a scanner trace dict.

    The :func:`signal_scanner.engines.trace.build_trace` payload nests
    ``library_version`` and ``definition_version`` under the class
    metadata block. T-CB-018 reads ``definition_version`` from the
    trace so the persisted prediction row pins the version that was
    actually used for the posterior, satisfying REQ-CB-FREEZE-003.
    A defensive default of ``1`` is returned when the trace shape is
    unexpected — the synthesised :class:`BacktestPrediction` validator
    requires ``definition_version >= 1`` and the run-id hashing has
    already pinned the canonical value at the run level.
    """
    cls_meta = trace.get("class") if isinstance(trace, Mapping) else None
    if isinstance(cls_meta, Mapping):
        version = cls_meta.get("definition_version")
        if isinstance(version, int) and version >= 1:
            return version
    return 1


# ---------------------------------------------------------------------------
# Configuration loader for the per-run ``max_workers`` knob
# ---------------------------------------------------------------------------

DEFAULT_MAX_WORKERS: Final[int] = 4
"""Fallback worker count when ``config/backtest.yaml`` is unavailable.

Mirrors the ``max_workers: 4`` line in ``backtest.yaml`` so the
in-Python default and the on-disk config never drift. The full config
loader lands in T-CB-026; T-CB-018 keeps the resolution narrow and
self-contained.
"""

_BACKTEST_YAML_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "config" / "backtest.yaml"
)


def _load_max_workers() -> int:
    """Return the configured ``max_workers`` knob or :data:`DEFAULT_MAX_WORKERS`.

    Parses the project's tiny ``backtest.yaml`` without pulling a
    dependency on PyYAML — the file is a flat ``key: value`` list and
    line-by-line parsing is sufficient for the four scalars the
    bootstrap config exposes. Any parse failure falls back to the
    default rather than raising; the replay loop is robust to a missing
    or malformed file by design (the default is the on-disk value
    anyway).
    """
    try:
        text = _BACKTEST_YAML_PATH.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_MAX_WORKERS
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip() != "max_workers":
            continue
        try:
            parsed = int(value.strip())
        except ValueError:
            return DEFAULT_MAX_WORKERS
        if parsed < 1:
            return DEFAULT_MAX_WORKERS
        return parsed
    return DEFAULT_MAX_WORKERS


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_backtest(
    params: RunParameters,
    *,
    conn: duckdb.DuckDBPyConnection,
    store: DuckDBStore,
    library_version: int | None = None,
    min_support: int = DEFAULT_MIN_SUPPORT,
    now: datetime | None = None,
    max_workers: int | None = None,
    persistence_conn: duckdb.DuckDBPyConnection | None = None,
) -> ReplayResult:
    """Run the calibration replay loop against ``params`` (T-CB-018).

    Pipeline (design §3.5; closely follows the §3.5 pseudocode):

    1. **Recent-window guard (REQ-CB-RUN-002).** Compute
       ``cutoff = now - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS)``;
       when ``params.until_ts > cutoff`` and ``params.allow_recent`` is
       ``False``, raise :class:`RecentWindowError` **before** any
       persistence or evaluation work fires. The error carries
       ``until_ts``, ``cutoff``, and ``recommended_until_ts=cutoff`` so
       the CLI can render a deterministic remediation hint. When
       ``allow_recent=True`` the run proceeds and the synthesised
       :class:`BacktestRun` records ``allow_recent=True`` for
       auditability.
    2. **Resolution iteration.** :func:`iter_mapped_resolutions` yields
       every in-scope ``(resolution, mapping)`` pair pre-filtered by
       SQL. Per-resolution work is dispatched onto a bounded
       :class:`ThreadPoolExecutor` (default four workers; resolved from
       ``config/backtest.yaml`` ``max_workers`` and overridable via
       ``max_workers`` kwarg for tests).
    3. **Per-prediction work.** :func:`_evaluate_one_resolution` walks
       the §3.5 sequence (invalidated -> lag -> freeze -> polarity ->
       evaluate -> polarity-correct). Each per-prediction worker
       returns a :class:`_PredictionOutcome` carrying either a
       ``SCORED`` :class:`BacktestPrediction` (with its trace dict) or
       a ``SKIPPED`` row with the appropriate :class:`SkipReason`.
    4. **Buffered aggregation.** Predictions are deduplicated by
       ``prediction_id`` (deterministic per ``(run_id, condition_id,
       class_id, venue)``) so a duplicate yield from the iterator does
       not produce a double-score; the first outcome wins.

    Persistence wiring (T-CB-019, design §3.5, §3.11). When
    ``persistence_conn`` is supplied:

    * Before the inner loop, an ``IN_PROGRESS`` :class:`BacktestRun`
      row is inserted via :func:`persistence.insert_run` so the row is
      visible to concurrent observers (operator dashboards, run-history
      CLIs) while the replay is still running.
    * After every per-prediction outcome, the row is appended to
      ``backtest_predictions`` via :func:`persistence.insert_prediction`.
      Scored rows additionally trigger
      :func:`persistence.insert_trace` with the zstd-compressed trace
      blob (:func:`trace_codec.encode_trace`); skipped rows have no
      trace per design §3.11.
    * After the loop completes successfully, the row transitions to
      ``COMPLETE`` via :func:`persistence.complete_run` and the final
      counter / summary fields are populated.
    * If any uncaught exception escapes the loop, the row transitions
      to ``FAILED`` via :func:`persistence.complete_run` with
      ``error_summary=str(exc)`` and the exception is re-raised
      (REQ-CB-RUN-005 — the run row records the failure even when the
      operator-facing crash propagates).

    When ``persistence_conn`` is ``None`` the function is a pure
    in-memory orchestrator: nothing is written to disk and the
    returned :class:`ReplayResult` carries the same shape it always
    has. This keeps the orchestration tests in
    ``tests/calibration_backtest/test_replay.py`` self-contained
    (they do not need to apply the calibration_backtest schema).

    Args:
        params: Validated :class:`RunParameters` instance.
        conn: Open DuckDB connection on which the upstream tables
            (``polymarket_resolutions``, ``class_market_mappings``,
            ``comparisons``, ``comparison_resolutions``, the four
            canonical data_ingest tables, and the ``sources`` registry)
            are visible.
        store: :class:`DuckDBStore` forwarded to
            :func:`evaluate_class_at_frozen_time` for the
            signal_scanner posterior pipeline.
        library_version: Optional library version pin forwarded to the
            class evaluation wrapper (T-CB-017 contract).
        min_support: Minimum non-``None`` precursor count required to
            proceed to the posterior (T-CB-017 contract); defaults to
            :data:`DEFAULT_MIN_SUPPORT`.
        now: Override for ``datetime.now(UTC)`` so the recent-window
            guard is testable without freezegun. Defaults to live wall
            clock.
        max_workers: Override for the ``max_workers`` config knob so
            tests can pin the executor pool to one worker for
            deterministic ordering.
        persistence_conn: Optional DuckDB connection on which the
            calibration_backtest schema (``backtest_runs``,
            ``backtest_predictions``, ``backtest_traces``) is applied.
            When ``None`` the function does not write any rows; when
            provided, the wiring described above engages. May be the
            same connection object as ``conn`` when the upstream and
            persistence schemas live in the same DuckDB database.

    Returns:
        :class:`ReplayResult` carrying the synthesised
        :class:`BacktestRun`, the buffered predictions tuple, and the
        per-prediction trace mapping. When ``persistence_conn`` is
        provided the persisted ``backtest_runs`` row is byte-equivalent
        to ``result.run`` (same status, same counters).

    Raises:
        RecentWindowError: When the recent-window guard trips and
            ``params.allow_recent`` is ``False``. Raised before any
            iterator or persistence work fires (REQ-CB-RUN-002).
        Exception: Any uncaught exception raised inside the loop body
            is re-raised after the run row is transitioned to
            ``FAILED`` (when ``persistence_conn`` is provided).
    """
    # Step 1 — recent-window guard (REQ-CB-RUN-002).
    current_now = now if now is not None else datetime.now(UTC)
    cutoff = current_now - timedelta(days=DEFAULT_RECENT_WINDOW_DAYS)
    if params.until_ts > cutoff and not params.allow_recent:
        raise RecentWindowError(
            until_ts=params.until_ts,
            cutoff=cutoff,
            recommended_until_ts=cutoff,
        )

    # Capture the system revision once at the top so the IN_PROGRESS row
    # and the COMPLETE/FAILED row record the same value (T-CB-026 clears
    # Phase 3 review advisory F). Hard-coded ``"unversioned"`` strings
    # are gone; the resolver falls back to
    # :data:`version.SYSTEM_REVISION_FALLBACK` when git is unavailable.
    system_revision = version_module.resolve_system_revision()

    # Resolve the pattern-library version once. The replay loop already
    # accepts a ``library_version`` override (used by tests to pin a
    # specific version); when omitted we resolve from
    # :func:`pattern_library.version.current_version`. The resolved
    # value flows into both the canonical run-id hash (REQ-CB-FREEZE-003)
    # and the persisted ``backtest_runs.library_version`` column.
    resolved_library_version = (
        library_version if library_version is not None else _pattern_library_current_version()
    )

    # Resolve per-class definition versions for the run-id hash
    # (REQ-CB-FREEZE-003). The lookup is best-effort: when the store is
    # not a real DuckDBStore (e.g. integration tests passing
    # ``object()``) or the ``pl_event_classes`` table has not been
    # populated, we fall back to ``definition_version=1`` per class.
    # This keeps the run-id stable across processes for the same
    # configuration without forcing every test to seed pattern_library.
    class_definition_versions = _resolve_class_definition_versions(
        store=store, class_ids=tuple(params.class_ids)
    )

    # Compute the canonical, deterministic run_id (T-CB-026 clears
    # Phase 3 review advisory E). Two runs with identical
    # :class:`RunParameters`, identical pattern-library state, and the
    # same captured ``system_revision`` produce the same 64-char SHA-256
    # hex digest — replacing the previous ``uuid.uuid4().hex``
    # placeholder. ``params.bin_count`` / ``params.bin_count_per_sector``
    # are deliberately excluded from the hash (display-only per design
    # §3.4), so a bin-count change does NOT invalidate prior caches.
    run_id = compute_run_id_for_params(
        params,
        library_version=resolved_library_version,
        system_revision=system_revision,
        class_definition_versions=class_definition_versions,
    )

    started_at = current_now
    workers = max_workers if max_workers is not None else _load_max_workers()
    if workers < 1:
        workers = 1

    # Resolve the bin counts that go onto the persisted run row.
    # Resolution does not affect the run_id hash (excluded by design)
    # and does not validate the report.yaml in tests that pass nothing
    # — a missing file falls back to defaults with a warning logged.
    bin_count_global, bin_count_per_sector = _resolve_run_bin_counts(params)

    # Step 1b — persist the IN_PROGRESS run row before any per-prediction
    # work fires. Operators tailing ``backtest_runs`` see the row appear
    # immediately so a long-running replay is observable while it is
    # still working through the inner loop. The row is mutated in place
    # by :func:`persistence.complete_run` after the loop concludes
    # (``in_progress -> complete | failed`` per
    # :data:`persistence.operations._ALLOWED_TRANSITIONS`).
    if persistence_conn is not None:
        in_progress_run = BacktestRun(
            run_id=run_id,
            since_ts=params.since_ts,
            until_ts=params.until_ts,
            lag_days=params.lag_days,
            class_ids=tuple(params.class_ids),
            sectors=tuple(params.sectors),
            venues=tuple(params.venues),
            library_version=resolved_library_version,
            system_revision=system_revision,
            started_at=started_at,
            completed_at=None,
            status=BacktestStatus.IN_PROGRESS,
            error_summary=None,
            predictions_total=0,
            predictions_scored=0,
            predictions_skipped=0,
            overall_brier=None,
            summary_json=None,
            bin_count_global=bin_count_global,
            bin_count_per_sector=bin_count_per_sector,
            fallback_polarity_count=0,
            allow_recent=params.allow_recent,
            disclaimer_version="v1",
        )
        persistence.insert_run(persistence_conn, in_progress_run)

    try:
        # Step 2 — resolution iteration. Materialise the iterator into a
        # list so the executor sees a fixed work set (cheaper than
        # chunked streaming for the v1 seed library's ~500 resolutions).
        resolutions: list[MappedResolution] = list(
            iter_mapped_resolutions(
                conn,
                params.since_ts,
                params.until_ts,
                params.venues,
                params.class_ids,
            )
        )

        # Step 3 — per-prediction work. The bounded ThreadPoolExecutor
        # mirrors signal_scanner's pattern; DuckDB connections are not
        # thread-safe, so when ``workers > 1`` callers must pass distinct
        # connections per worker. T-CB-019 documents the constraint and
        # keeps the default at 4 workers per design §3.5 /
        # config/backtest.yaml.
        outcomes: list[_PredictionOutcome] = []
        if workers == 1 or not resolutions:
            # Fast-path: single-threaded iteration. Avoids spinning up
            # the executor when the per-call overhead would dominate
            # (e.g. tests that seed only a handful of rows).
            for resolution in resolutions:
                outcomes.append(
                    _evaluate_one_resolution(
                        run_id=run_id,
                        params=params,
                        resolution=resolution,
                        conn=conn,
                        store=store,
                        library_version=library_version,
                        min_support=min_support,
                    )
                )
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(
                        _evaluate_one_resolution,
                        run_id=run_id,
                        params=params,
                        resolution=resolution,
                        conn=conn,
                        store=store,
                        library_version=library_version,
                        min_support=min_support,
                    )
                    for resolution in resolutions
                ]
                for future in futures:
                    outcomes.append(future.result())

        # Step 4 — deduplicate by prediction_id.
        # ``iter_mapped_resolutions`` may yield duplicate JOIN rows when
        # the upstream tables hold multiple active mappings for the
        # same (class_id, condition_id, venue) tuple (the active-mapping
        # uniqueness invariant is application-enforced in
        # mispricing_detector, not a hard SQL constraint). The first
        # outcome for a prediction_id wins so the buffer matches the
        # persistence-layer composite PK semantics.
        by_prediction_id: dict[str, _PredictionOutcome] = {}
        for outcome in outcomes:
            prediction_id = outcome.prediction.prediction_id
            if prediction_id in by_prediction_id:
                continue
            by_prediction_id[prediction_id] = outcome

        deduplicated = tuple(by_prediction_id.values())
        predictions = tuple(outcome.prediction for outcome in deduplicated)
        traces: dict[str, dict[str, Any]] = {
            outcome.prediction.prediction_id: outcome.trace
            for outcome in deduplicated
            if outcome.trace is not None
        }

        # Step 5 — persistence wiring (T-CB-019, design §3.5, §3.11).
        # Each scored row drops a (prediction, trace) pair; each
        # skipped row drops only a prediction. The trace blob is
        # produced by ``trace_codec.encode`` (zstd at level 3) and the
        # decompressed size is captured for the disk-budget guard.
        if persistence_conn is not None:
            for outcome in deduplicated:
                persistence.insert_prediction(persistence_conn, outcome.prediction)
                if outcome.trace is not None:
                    blob, decompressed_size_bytes = trace_codec.encode_trace(outcome.trace)
                    persistence.insert_trace(
                        persistence_conn,
                        BacktestTrace(
                            run_id=run_id,
                            prediction_id=outcome.prediction.prediction_id,
                            trace_json_compressed=blob,
                            compression_algorithm=CompressionAlgorithm.ZSTD,
                            decompressed_size_bytes=decompressed_size_bytes,
                        ),
                    )

        scored_count = sum(1 for p in predictions if p.status is PredictionStatus.SCORED)
        skipped_count = sum(1 for p in predictions if p.status is PredictionStatus.SKIPPED)
        fallback_count = sum(
            1
            for p in predictions
            if p.status is PredictionStatus.SCORED
            and p.polarity_source is PolaritySource.CURRENT_MAPPING_FALLBACK
        )

        completed_at = datetime.now(UTC) if now is None else current_now
        if completed_at < started_at:
            completed_at = started_at

        run = BacktestRun(
            run_id=run_id,
            since_ts=params.since_ts,
            until_ts=params.until_ts,
            lag_days=params.lag_days,
            class_ids=tuple(params.class_ids),
            sectors=tuple(params.sectors),
            venues=tuple(params.venues),
            library_version=resolved_library_version,
            system_revision=system_revision,
            started_at=started_at,
            completed_at=completed_at,
            status=BacktestStatus.COMPLETE,
            error_summary=None,
            predictions_total=len(predictions),
            predictions_scored=scored_count,
            predictions_skipped=skipped_count,
            overall_brier=None,
            summary_json=None,
            bin_count_global=bin_count_global,
            bin_count_per_sector=bin_count_per_sector,
            fallback_polarity_count=fallback_count,
            allow_recent=params.allow_recent,
            disclaimer_version="v1",
        )

        # Step 6 — transition the persisted run row to COMPLETE.
        if persistence_conn is not None:
            summary_json: dict[str, Any] = {
                "predictions_total": len(predictions),
                "predictions_scored": scored_count,
                "predictions_skipped": skipped_count,
                "fallback_polarity_count": fallback_count,
            }
            persistence.complete_run(
                persistence_conn,
                run_id,
                status=BacktestStatus.COMPLETE,
                completed_at=completed_at,
                summary_json=summary_json,
                predictions_total=len(predictions),
                predictions_scored=scored_count,
                predictions_skipped=skipped_count,
                fallback_polarity_count=fallback_count,
            )

        return ReplayResult(run=run, predictions=predictions, traces=traces)
    except Exception as exc:
        # Uncaught failure path (REQ-CB-RUN-005, T-CB-019). Per-prediction
        # exceptions are already routed to ``skip_reason='exception'`` by
        # :func:`_evaluate_one_resolution`; anything still escaping here
        # is a loop-level failure (executor, persistence driver, or a
        # misuse of the public surface). When persistence is wired the
        # run row is transitioned to FAILED with ``error_summary=str(exc)``
        # so the operator sees the failure recorded in ``backtest_runs``;
        # the exception is then re-raised so the caller's stack trace is
        # preserved.
        if persistence_conn is not None:
            failed_at = datetime.now(UTC) if now is None else current_now
            if failed_at < started_at:
                failed_at = started_at
            try:
                persistence.complete_run(
                    persistence_conn,
                    run_id,
                    status=BacktestStatus.FAILED,
                    completed_at=failed_at,
                    error_summary=str(exc),
                )
            except Exception:
                _LOGGER.exception(
                    "calibration_backtest.replay.failed_run_record_failure",
                    extra={"event": "complete_run_failed", "run_id": run_id},
                )
        raise


def _validate_iterable_non_empty(values: Iterable[str], field: str) -> None:
    """Defensive guard reused by future T-CB-019 wiring (currently unused)."""
    if not list(values):
        raise BacktestConfigError(f"{field} must be non-empty")


# ---------------------------------------------------------------------------
# Run-id input resolution (T-CB-026; design §3.4)
# ---------------------------------------------------------------------------


def _resolve_class_definition_versions(*, store: Any, class_ids: tuple[str, ...]) -> dict[str, int]:
    """Best-effort lookup of per-class ``definition_version`` from pattern_library.

    Hashes into the canonical ``run_id`` so any class's
    ``definition_version`` bump propagates into a fresh run_id
    (REQ-CB-FREEZE-003). The lookup is *defensive* by design: when the
    supplied ``store`` is not a real :class:`DuckDBStore` (tests often
    pass a bare :class:`object`) or the ``pl_event_classes`` table has
    not been populated, every class falls back to
    ``definition_version=1`` so the run-id is still computable.

    The fallback keeps the orchestration tests in
    ``tests/calibration_backtest/test_replay.py`` self-contained — they
    do not need to seed the pattern_library schema just to compute a
    deterministic ``run_id``. Production wiring (T-CB-018+) supplies a
    populated :class:`DuckDBStore` and the lookup returns truthful
    versions.
    """
    versions: dict[str, int] = {class_id: 1 for class_id in class_ids}
    try:
        summaries = library.list_classes(store, include_removed=True)
    except Exception:
        # ``object()`` store, missing schema, or any other lookup
        # failure — fall back to the default mapping. The replay loop
        # logs the exception via the per-prediction error path; we
        # deliberately do not log here because the run-id computation
        # is best-effort and a missing pattern_library is the
        # bootstrap-test default.
        return versions
    for summary in summaries:
        if summary.class_id in versions:
            versions[summary.class_id] = summary.definition_version
    return versions


def _resolve_run_bin_counts(params: RunParameters) -> tuple[int, dict[str, int]]:
    """Best-effort bin-count resolution for the persisted ``backtest_runs`` row.

    Wraps :func:`engines.scoring.resolve_bin_counts` so the replay loop
    never crashes on a malformed ``config/report.yaml`` — operators can
    surface a backtest run row even when the report config is broken
    upstream. Any resolver error falls back to the CLI override (when
    set) or :data:`DEFAULT_FALLBACK_BIN_COUNT`.
    """
    # Local import so the replay module's import surface stays narrow
    # (the scoring module has heavier transitive imports).
    from razor_rooster.calibration_backtest.engines.scoring import resolve_bin_counts

    try:
        return resolve_bin_counts(params)
    except BacktestConfigError:
        # Validation failure: the CLI override or the report.yaml
        # produced an out-of-range bin count. Re-raise so the operator
        # sees the configuration error rather than persisting a row
        # with a silently-defaulted bin count.
        raise
    except Exception:
        # Any other failure (loader I/O, transitive import, etc.) falls
        # back to the CLI override or the module default. The replay
        # row still records a coherent bin count for auditability.
        global_bin_count = (
            params.bin_count if params.bin_count is not None else DEFAULT_FALLBACK_BIN_COUNT
        )
        if global_bin_count < 2:
            global_bin_count = DEFAULT_FALLBACK_BIN_COUNT
        per_sector_overrides = {
            sector: count
            for sector, count in params.bin_count_per_sector.items()
            if count >= 2 and count != global_bin_count
        }
        return global_bin_count, per_sector_overrides


DEFAULT_FALLBACK_BIN_COUNT: Final[int] = 10
"""Final fallback when ``resolve_bin_counts`` raises a non-config error.

Mirrors :data:`engines.scoring.DEFAULT_BIN_COUNT` so the replay row's
bin count matches the scoring engine's default when both fall through
to module defaults.
"""
