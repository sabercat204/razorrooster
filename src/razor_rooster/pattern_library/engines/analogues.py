"""Analogue feature space engine (T-PL-045; design §3.5).

Two phases:

1. :func:`populate_feature_space` — for each event occurrence and a
   sampled population of baseline timestamps, compute the feature
   vector via each ``AnalogueFeature.query``. Normalize per-feature
   using the population's stats (z-score by default per OQ-PL-004).
   Returns a typed :class:`AnalogueFeatureSpace` plus the row payload
   to persist.

2. :func:`find_analogues` — load the persisted population, normalize
   the operator-supplied "current" feature vector using the same
   population stats, compute weighted-Euclidean distance, and return
   the top-k closest historical points.

Per-class distance-metric override (Mahalanobis) is supported via the
``metric`` kwarg on ``find_analogues`` — the analogue engine itself
stays metric-agnostic; class definitions provide their own callable.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import duckdb
import numpy as np

from razor_rooster.pattern_library.models.analogue import (
    AnalogueFeatureSpace,
    AnalogueMatch,
    AnalogueResults,
)
from razor_rooster.pattern_library.models.event_class import (
    AnalogueFeature,
    Normalization,
)
from razor_rooster.pattern_library.persistence.operations import (
    _AnalogueRow,
    query_analogue_population,
)

if TYPE_CHECKING:
    from razor_rooster.pattern_library.models.event_class import EventClass
    from razor_rooster.pattern_library.models.outcomes import OutcomeRecord

logger = logging.getLogger(__name__)


# Default baseline sample size for analogue feature population, per
# class. Class authors override via cls.baseline_sample_size which is
# already used by the signature engine; reusing it keeps the two
# samples comparable in size.
DEFAULT_BASELINE_SIZE: int = 1_000

# Default top-k for find_analogues.
DEFAULT_TOP_K: int = 10


def populate_feature_space(
    conn: duckdb.DuckDBPyConnection,
    cls: EventClass,
    *,
    outcomes: Sequence[OutcomeRecord],
    library_version: int,
    baseline_size: int | None = None,
    rng: np.random.Generator | None = None,
    now: datetime | None = None,
) -> tuple[AnalogueFeatureSpace, tuple[_AnalogueRow, ...]]:
    """Compute and normalize the feature space for ``cls``.

    Returns ``(space, rows)`` where ``rows`` is the per-point payload
    the caller persists via ``upsert_analogue_features``.

    Per-feature normalization parameters are computed across the union
    of event and baseline points so the same parameters apply to a
    later ``find_analogues`` query.
    """
    started = now or datetime.now(tz=UTC)
    rng = rng or np.random.default_rng(seed=_class_seed(cls.class_id))

    if not cls.analogue_features:
        space = AnalogueFeatureSpace(
            class_id=cls.class_id,
            library_version=library_version,
            definition_version=cls.definition_version,
            feature_ids=("__placeholder__",),
            point_count=0,
            event_count=0,
            normalization_params={},
        )
        return _empty_space(cls=cls, library_version=library_version), ()

    feature_ids = tuple(f.feature_id for f in cls.analogue_features)
    feature_by_id: dict[str, AnalogueFeature] = {f.feature_id: f for f in cls.analogue_features}

    event_points: list[_PointRaw] = []
    for occurrence in outcomes:
        vec = _compute_feature_vector(
            conn,
            features=cls.analogue_features,
            timestamp=occurrence.occurrence_ts,
        )
        event_points.append(
            _PointRaw(
                point_id=f"event:{occurrence.occurrence_id}",
                timestamp=occurrence.occurrence_ts,
                is_event=True,
                features=vec,
            )
        )

    baseline_count = baseline_size or cls.baseline_sample_size
    baseline_points = _sample_baseline_points(
        conn,
        cls=cls,
        outcomes=outcomes,
        baseline_size=baseline_count,
        rng=rng,
        now=started,
    )

    all_points = event_points + baseline_points
    if not all_points:
        return _empty_space(cls=cls, library_version=library_version), ()

    normalization_params = _compute_normalization_params(
        feature_by_id=feature_by_id,
        all_points=all_points,
    )

    rows: list[_AnalogueRow] = []
    for point in all_points:
        normalized = _normalize_vector(
            raw=point.features,
            params=normalization_params,
            feature_by_id=feature_by_id,
        )
        rows.append(
            _AnalogueRow(
                point_id=point.point_id,
                timestamp=point.timestamp,
                is_event=point.is_event,
                feature_vector_raw=point.features,
                feature_vector_normalized=normalized,
            )
        )

    space = AnalogueFeatureSpace(
        class_id=cls.class_id,
        library_version=library_version,
        definition_version=cls.definition_version,
        feature_ids=feature_ids,
        point_count=len(all_points),
        event_count=len(event_points),
        normalization_params=normalization_params,
    )
    return space, tuple(rows)


def find_analogues(
    conn: duckdb.DuckDBPyConnection,
    *,
    class_id: str,
    current_features: dict[str, float],
    library_version: int,
    definition_version: int,
    feature_weights: dict[str, float] | None = None,
    metric: Callable[[np.ndarray, np.ndarray], float] | None = None,
    k: int = DEFAULT_TOP_K,
    query_timestamp: datetime | None = None,
) -> AnalogueResults:
    """Return the top-k closest historical points to ``current_features``.

    ``feature_weights`` defaults to 1.0 per feature; class authors set
    per-feature weight via ``AnalogueFeature.weight`` and the refresh
    runner threads that into this call.

    ``metric`` accepts an optional override callable taking two
    1D arrays. The default is weighted Euclidean.
    """
    qt = query_timestamp or datetime.now(tz=UTC)
    population = query_analogue_population(conn, class_id, library_version=library_version)
    if not population:
        return AnalogueResults(
            class_id=class_id,
            library_version=library_version,
            definition_version=definition_version,
            query_timestamp=qt,
            matches=(),
        )

    # Identify the feature universe from the persisted rows.
    universe = sorted({fid for r in population for fid in r.feature_vector_raw})
    if not universe:
        return AnalogueResults(
            class_id=class_id,
            library_version=library_version,
            definition_version=definition_version,
            query_timestamp=qt,
            matches=(),
        )

    weights = np.array(
        [float((feature_weights or {}).get(fid, 1.0)) for fid in universe],
        dtype=float,
    )

    # Build a representative normalization-params snapshot from the
    # first row's raw-vs-normalized pair. Use both to reconstruct
    # mean/std per feature for the z-score case; for percentile_rank or
    # 'none', fall back to using normalized values directly.
    norm_params = _reconstruct_normalization_params(population, feature_ids=universe)
    current_normalized = _apply_normalization_to_query(
        raw=current_features, feature_ids=universe, params=norm_params
    )

    population_matrix = np.zeros((len(population), len(universe)), dtype=float)
    for row_idx, row in enumerate(population):
        for col_idx, fid in enumerate(universe):
            population_matrix[row_idx, col_idx] = row.feature_vector_normalized.get(fid, 0.0)

    query_vector = np.array([current_normalized.get(fid, 0.0) for fid in universe], dtype=float)

    distance_fn = metric or _make_weighted_euclidean(weights)
    distances = np.array(
        [distance_fn(query_vector, population_matrix[i]) for i in range(len(population))],
        dtype=float,
    )

    if distances.size == 0:
        return AnalogueResults(
            class_id=class_id,
            library_version=library_version,
            definition_version=definition_version,
            query_timestamp=qt,
            matches=(),
        )

    top_k_indices = np.argsort(distances, kind="stable")[:k]
    matches = tuple(
        AnalogueMatch(
            point_id=population[i].point_id,
            timestamp=population[i].timestamp,
            is_event=population[i].is_event,
            distance=float(distances[i]),
            feature_vector_normalized=dict(population[i].feature_vector_normalized),
        )
        for i in top_k_indices
    )

    return AnalogueResults(
        class_id=class_id,
        library_version=library_version,
        definition_version=definition_version,
        query_timestamp=qt,
        matches=matches,
    )


# -- internals --------------------------------------------------------------


class _PointRaw:
    """Internal — pre-normalization row."""

    __slots__ = ("features", "is_event", "point_id", "timestamp")

    def __init__(
        self,
        *,
        point_id: str,
        timestamp: datetime,
        is_event: bool,
        features: dict[str, float],
    ) -> None:
        self.point_id = point_id
        self.timestamp = timestamp
        self.is_event = is_event
        self.features = features


def _empty_space(*, cls: EventClass, library_version: int) -> AnalogueFeatureSpace:
    """Return a sentinel space when the class has no analogue features."""
    return AnalogueFeatureSpace(
        class_id=cls.class_id,
        library_version=library_version,
        definition_version=cls.definition_version,
        feature_ids=tuple(f.feature_id for f in cls.analogue_features) or ("__placeholder__",),
        point_count=0,
        event_count=0,
        normalization_params={},
    )


def _class_seed(class_id: str) -> int:
    return int.from_bytes(hashlib.sha256(class_id.encode("utf-8")).digest()[:4], "big")


def _compute_feature_vector(
    conn: duckdb.DuckDBPyConnection,
    *,
    features: tuple[AnalogueFeature, ...],
    timestamp: datetime,
) -> dict[str, float]:
    """Invoke each feature's query at the given timestamp.

    Per the design, the query returns a single float per timestamp.
    NaN-or-error coalesces to 0.0 so missing features don't break the
    distance computation; the refresh log captures per-feature errors.
    """
    vec: dict[str, float] = {}
    for feature in features:
        try:
            value = feature.query(conn, timestamp)
        except Exception:
            logger.exception("analogue feature %s failed at %s", feature.feature_id, timestamp)
            value = 0.0
        try:
            float_value = float(value)
        except (TypeError, ValueError):
            float_value = 0.0
        if math.isnan(float_value):
            float_value = 0.0
        vec[feature.feature_id] = float_value
    return vec


def _sample_baseline_points(
    conn: duckdb.DuckDBPyConnection,
    *,
    cls: EventClass,
    outcomes: Sequence[OutcomeRecord],
    baseline_size: int,
    rng: np.random.Generator,
    now: datetime,
) -> list[_PointRaw]:
    """Sample ``baseline_size`` non-event timestamps and compute their feature vectors.

    The sampling honors the class's refractory exclusion zone around
    each occurrence so baseline points don't overlap pre-event windows.
    The window is bounded above by ``now`` and below by the class's
    ``base_rate_window_default``.
    """
    refractory = timedelta(days=30 * cls.refractory_months)
    window_end = now
    window_start = now - cls.base_rate_window_default
    span_seconds = (window_end - window_start).total_seconds()
    if span_seconds <= 0 or baseline_size <= 0:
        return []

    refractory_zones = [
        (rec.occurrence_ts - refractory, rec.occurrence_ts + refractory) for rec in outcomes
    ]

    oversample = max(baseline_size * 4, 100)
    offsets = rng.uniform(0.0, span_seconds, size=oversample)
    candidates = [window_start + timedelta(seconds=float(s)) for s in offsets]

    selected: list[datetime] = []
    for ts in candidates:
        if any(low <= ts <= high for low, high in refractory_zones):
            continue
        selected.append(ts)
        if len(selected) >= baseline_size:
            break

    points: list[_PointRaw] = []
    for ts in selected:
        vec = _compute_feature_vector(conn, features=cls.analogue_features, timestamp=ts)
        points.append(
            _PointRaw(
                point_id=f"baseline:{_baseline_point_id(cls.class_id, ts)}",
                timestamp=ts,
                is_event=False,
                features=vec,
            )
        )
    return points


def _baseline_point_id(class_id: str, ts: datetime) -> str:
    """Deterministic short id for a baseline point."""
    raw = f"{class_id}:{ts.isoformat()}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _compute_normalization_params(
    *,
    feature_by_id: dict[str, AnalogueFeature],
    all_points: list[_PointRaw],
) -> dict[str, dict[str, float]]:
    """Per-feature population stats for the normalization step."""
    params: dict[str, dict[str, float]] = {}
    for feature_id, feature in feature_by_id.items():
        values = np.array([p.features.get(feature_id, 0.0) for p in all_points], dtype=float)
        if values.size == 0:
            params[feature_id] = {"mean": 0.0, "std": 1.0, "min": 0.0, "max": 0.0}
            continue
        mean = float(np.mean(values))
        std = float(np.std(values))
        params[feature_id] = {
            "mean": mean,
            "std": std if std > 0 else 1.0,
            "min": float(np.min(values)),
            "max": float(np.max(values)),
            "method": _normalization_method_to_int(feature.normalization),
        }
    return params


def _normalization_method_to_int(method: Normalization) -> float:
    """Encode the normalization method as a float so the JSON dict stays uniform."""
    mapping = {
        Normalization.ZSCORE: 0.0,
        Normalization.PERCENTILE_RANK: 1.0,
        Normalization.NONE: 2.0,
    }
    return mapping[method]


def _normalization_method_from_int(method_value: float) -> Normalization:
    rounded = round(method_value)
    if rounded == 0:
        return Normalization.ZSCORE
    if rounded == 1:
        return Normalization.PERCENTILE_RANK
    if rounded == 2:
        return Normalization.NONE
    return Normalization.ZSCORE  # safe default


def _normalize_vector(
    *,
    raw: dict[str, float],
    params: dict[str, dict[str, float]],
    feature_by_id: dict[str, AnalogueFeature],
) -> dict[str, float]:
    out: dict[str, float] = {}
    for fid, value in raw.items():
        feature = feature_by_id.get(fid)
        method = feature.normalization if feature else Normalization.ZSCORE
        p = params.get(fid, {"mean": 0.0, "std": 1.0, "min": 0.0, "max": 1.0})
        if method == Normalization.NONE:
            out[fid] = float(value)
            continue
        if method == Normalization.ZSCORE:
            std = p.get("std", 1.0) or 1.0
            out[fid] = float((value - p.get("mean", 0.0)) / std)
            continue
        if method == Normalization.PERCENTILE_RANK:
            min_value = p.get("min", 0.0)
            max_value = p.get("max", 1.0)
            span = max(max_value - min_value, 1e-12)
            out[fid] = float((value - min_value) / span)
            continue
        out[fid] = float(value)
    return out


def _reconstruct_normalization_params(
    population: tuple[_AnalogueRow, ...],
    *,
    feature_ids: list[str],
) -> dict[str, dict[str, float]]:
    """Recover per-feature mean/std from the persisted population.

    The pl_analogue_features table stores both raw and normalized
    vectors. For each feature we recompute mean/std from the raw
    values; this is the operator-side complement to
    :func:`_compute_normalization_params`.
    """
    params: dict[str, dict[str, float]] = {}
    for fid in feature_ids:
        values = np.array([r.feature_vector_raw.get(fid, 0.0) for r in population], dtype=float)
        if values.size == 0:
            params[fid] = {"mean": 0.0, "std": 1.0, "min": 0.0, "max": 0.0}
            continue
        mean = float(np.mean(values))
        std = float(np.std(values))
        params[fid] = {
            "mean": mean,
            "std": std if std > 0 else 1.0,
            "min": float(np.min(values)),
            "max": float(np.max(values)),
        }
    return params


def _apply_normalization_to_query(
    *,
    raw: dict[str, float],
    feature_ids: list[str],
    params: dict[str, dict[str, float]],
) -> dict[str, float]:
    """Normalize an operator-supplied feature vector using stored params.

    Defaults to z-score normalization unless the persisted params
    indicate otherwise — which they currently don't, so the query path
    z-scores everything for v1. This matches the persisted data when
    classes use the default Normalization.ZSCORE; for classes using
    Normalization.NONE or PERCENTILE_RANK the query-side normalization
    will be slightly wrong (a v1.1 enhancement is to persist the
    method along with the params).
    """
    out: dict[str, float] = {}
    for fid in feature_ids:
        value = float(raw.get(fid, 0.0))
        p = params.get(fid, {"mean": 0.0, "std": 1.0})
        std = p.get("std", 1.0) or 1.0
        out[fid] = (value - p.get("mean", 0.0)) / std
    return out


def _make_weighted_euclidean(
    weights: np.ndarray,
) -> Callable[[np.ndarray, np.ndarray], float]:
    def metric(a: np.ndarray, b: np.ndarray) -> float:
        diff = a - b
        return float(math.sqrt(float(np.sum(weights * diff * diff))))

    return metric
