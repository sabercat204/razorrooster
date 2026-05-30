"""Per-cycle threshold-distribution measurements (T-RG-COMPAT-MEAS-001).

Operators tune the multi-venue thresholds in
``config/report.yaml`` (DEFER-RG-COMPAT-001) and per-sector
overrides (DEFER-RG-COMPAT-002). Once tuned, they need a way to
see whether the threshold is well-calibrated for the corpus
they're running. This module captures the full distribution of
each cycle's underlying signal so the operator can compare their
threshold to the empirical distribution over time.

v0.40.0 shipped one measurement kind: ``cross_venue_spread_bps``.
v0.41.0 adds two more:

- ``single_venue_dominance_share`` — for each class mapped to
  more than one venue, the maximum venue's share of the combined
  24h volume across that class's venues. Compare against
  ``thresholds.single_venue_dominance_pct``.
- ``brier_per_sector`` — for each sector with at least one
  scoreable resolution in the rolling window, that sector's
  rolling Brier score. Compare against
  ``thresholds.brier_miscalibration``.

Future kinds follow the same shape so the renderer and CLI
inspectors don't need new code paths per kind.

The measurements table is informational only. No section
assembler reads from it; no rendering changes. The CLI
``razor-rooster report measurements`` subcommand is the
operator-facing surface.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any, Final

logger = logging.getLogger(__name__)


# Quantile cuts captured for every measurement kind. Median plus
# the operator-relevant tail percentiles. Picked once, persisted
# as JSON, so future readers can compute percentile-based
# threshold suggestions without re-running over the source data.
DEFAULT_PERCENTILES: Final[tuple[float, ...]] = (
    0.10,
    0.25,
    0.50,
    0.75,
    0.90,
    0.95,
    0.99,
)


# Bounded enum of measurement kinds. The CLI's ``--kind`` flag
# accepts any string for forward-compat with operator-defined
# kinds, but every kind shipped by the project is listed here so
# call sites can refer to it without typos.
MEASUREMENT_KIND_CROSS_VENUE_SPREAD_BPS: Final[str] = "cross_venue_spread_bps"
MEASUREMENT_KIND_SINGLE_VENUE_DOMINANCE_SHARE: Final[str] = "single_venue_dominance_share"
MEASUREMENT_KIND_BRIER_PER_SECTOR: Final[str] = "brier_per_sector"


SHIPPED_MEASUREMENT_KINDS: Final[tuple[str, ...]] = (
    MEASUREMENT_KIND_CROSS_VENUE_SPREAD_BPS,
    MEASUREMENT_KIND_SINGLE_VENUE_DOMINANCE_SHARE,
    MEASUREMENT_KIND_BRIER_PER_SECTOR,
)


def compute_distribution(
    values: list[float],
    *,
    threshold: float,
    percentiles: tuple[float, ...] = DEFAULT_PERCENTILES,
) -> dict[str, Any]:
    """Compute distribution stats for one measurement kind.

    Args:
        values: the population of observations (e.g. one
            ``spread_bps`` value per (class, venue-pair) for
            this cycle).
        threshold: the global threshold value at the time of
            measurement. Persisted alongside the distribution
            so historical records survive future config edits.
        percentiles: which quantiles to capture in the
            distribution payload. Defaults to
            :data:`DEFAULT_PERCENTILES`.

    Returns:
        A dict with the shape persisted into
        ``report_threshold_measurements.distribution_json``::

            {
              "n": int,
              "n_above_threshold": int,
              "configured_threshold": float,
              "min": float | None,
              "max": float | None,
              "mean": float | None,
              "stddev": float | None,
              "percentiles": {"0.10": ..., ..., "0.99": ...},
            }

        Empty input produces all-None numeric fields and
        ``n=0`` / ``n_above_threshold=0``. ``percentiles`` is
        emitted as a dict keyed by string for JSON
        round-tripping cleanliness.
    """
    n = len(values)
    n_above = sum(1 for v in values if v > threshold)
    if n == 0:
        return {
            "n": 0,
            "n_above_threshold": 0,
            "configured_threshold": float(threshold),
            "min": None,
            "max": None,
            "mean": None,
            "stddev": None,
            "percentiles": {f"{p:.2f}": None for p in percentiles},
        }
    sorted_values = sorted(float(v) for v in values)
    mean_value = sum(sorted_values) / n
    variance = sum((v - mean_value) ** 2 for v in sorted_values) / n
    stddev = math.sqrt(variance)
    return {
        "n": n,
        "n_above_threshold": n_above,
        "configured_threshold": float(threshold),
        "min": sorted_values[0],
        "max": sorted_values[-1],
        "mean": mean_value,
        "stddev": stddev,
        "percentiles": {f"{p:.2f}": _percentile(sorted_values, p) for p in percentiles},
    }


def cross_venue_spread_observations(content: Mapping[str, Any]) -> list[float]:
    """Extract spread_bps values from a cross_venue section content dict.

    The cross_venue assembler produces one item per class with
    ``spread_bps``; this helper pulls those out so the generator
    can record the distribution without re-running the SQL.

    Returns an empty list when the content is missing items or
    is in an unexpected shape — measurement is best-effort and
    must never break report generation.
    """
    items = content.get("items") if isinstance(content, Mapping) else None
    if not isinstance(items, list):
        return []
    out: list[float] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        raw = item.get("spread_bps")
        if raw is None:
            continue
        try:
            out.append(float(raw))
        except (TypeError, ValueError):
            continue
    return out


def single_venue_dominance_observations(content: Mapping[str, Any]) -> list[float]:
    """Extract per-class max-venue share from a surfaced section content dict.

    The surfaced assembler attaches a ``venue_shares`` mapping per
    comparison; for classes mapped to more than one venue, this
    helper extracts the maximum share (the value that the
    dominance-warning threshold compares against).

    Dedups by ``class_id`` so a class with two surfaced comparisons
    (one per venue) only contributes a single observation. Classes
    with a single venue (or no recorded shares) are skipped — the
    threshold is only meaningful when at least two venues exist.

    Returns an empty list on malformed content. Best-effort; never
    raises.
    """
    comparisons = content.get("comparisons") if isinstance(content, Mapping) else None
    if not isinstance(comparisons, list):
        return []
    seen: dict[str, float] = {}
    for cmp_ in comparisons:
        if not isinstance(cmp_, Mapping):
            continue
        class_id_raw = cmp_.get("class_id")
        shares_raw = cmp_.get("venue_shares")
        if class_id_raw is None or not isinstance(shares_raw, Mapping):
            continue
        if len(shares_raw) < 2:
            continue
        max_share: float | None = None
        for value in shares_raw.values():
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if max_share is None or v > max_share:
                max_share = v
        if max_share is None:
            continue
        class_id = str(class_id_raw)
        if class_id in seen:
            # Same class showing up twice — keep the higher share so
            # we don't under-report a dominance signal that a later
            # comparison may have updated.
            if max_share > seen[class_id]:
                seen[class_id] = max_share
        else:
            seen[class_id] = max_share
    return list(seen.values())


def brier_per_sector_observations(content: Mapping[str, Any]) -> list[float]:
    """Extract per-sector Brier scores from a calibration section content dict.

    One observation per sector that has at least one scoreable
    resolution in the rolling window. Sectors with zero
    resolutions don't appear in ``sector_brier_scores`` so we
    don't need to filter them here; the assembler already does it.

    Returns an empty list on malformed content. Best-effort; never
    raises.
    """
    scores = content.get("sector_brier_scores") if isinstance(content, Mapping) else None
    if not isinstance(scores, list):
        return []
    out: list[float] = []
    for entry in scores:
        if not isinstance(entry, Mapping):
            continue
        raw = entry.get("brier_score")
        if raw is None:
            continue
        try:
            out.append(float(raw))
        except (TypeError, ValueError):
            continue
    return out


def threshold_percentile_rank(
    distribution: Mapping[str, Any], *, threshold: float | None = None
) -> float | None:
    """Return the percentile rank of ``threshold`` within a recorded distribution.

    Reads the ``percentiles`` field of a distribution payload and
    returns the highest percentile cut whose value is at or below
    ``threshold``. For example, if the percentiles are
    ``{"0.50": 300, "0.75": 500, "0.90": 700}`` and the threshold
    is 600, the threshold is between p75 (500) and p90 (700) so
    the function returns 0.75.

    When the threshold is below the lowest recorded percentile,
    returns 0.0 (threshold is in the bottom tail). When at or
    above the highest recorded percentile, returns 1.0.

    Returns ``None`` when:
    - The distribution has no observations (``n == 0``)
    - The percentile mapping is missing or malformed
    - All percentile values are None

    If ``threshold`` is omitted, falls back to the
    ``configured_threshold`` field of the distribution payload.
    Used by the ``explain-thresholds`` CLI subcommand to show
    operators where their configured threshold sits in the
    empirical distribution.
    """
    if not isinstance(distribution, Mapping):
        return None
    n = distribution.get("n")
    if not isinstance(n, int) or n == 0:
        return None
    if threshold is None:
        configured = distribution.get("configured_threshold")
        if configured is None:
            return None
        try:
            threshold = float(configured)
        except (TypeError, ValueError):
            return None
    raw_percentiles = distribution.get("percentiles")
    if not isinstance(raw_percentiles, Mapping):
        return None
    items: list[tuple[float, float]] = []
    for key, value in raw_percentiles.items():
        if value is None:
            continue
        try:
            q = float(key)
            v = float(value)
        except (TypeError, ValueError):
            continue
        items.append((q, v))
    if not items:
        return None
    items.sort()
    # If threshold is at or above the highest percentile value,
    # return 1.0 (top tail).
    if threshold >= items[-1][1]:
        return 1.0
    # If threshold is below the lowest percentile value, return 0.0.
    if threshold < items[0][1]:
        return 0.0
    # Walk forward and return the highest q whose value is <= threshold.
    last_match = items[0][0]
    for q, v in items:
        if v <= threshold:
            last_match = q
        else:
            break
    return last_match


# -- internals --------------------------------------------------------------


def _percentile(sorted_values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile. ``sorted_values`` must be sorted."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    if q <= 0:
        return float(sorted_values[0])
    if q >= 1:
        return float(sorted_values[-1])
    rank = q * (len(sorted_values) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(sorted_values[lo])
    weight = rank - lo
    return float(sorted_values[lo] * (1 - weight) + sorted_values[hi] * weight)


__all__ = [
    "DEFAULT_PERCENTILES",
    "MEASUREMENT_KIND_BRIER_PER_SECTOR",
    "MEASUREMENT_KIND_CROSS_VENUE_SPREAD_BPS",
    "MEASUREMENT_KIND_SINGLE_VENUE_DOMINANCE_SHARE",
    "SHIPPED_MEASUREMENT_KINDS",
    "brier_per_sector_observations",
    "compute_distribution",
    "cross_venue_spread_observations",
    "single_venue_dominance_observations",
    "threshold_percentile_rank",
]
