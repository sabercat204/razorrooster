"""Reliability-diagram section (DEFER-RG-COMPAT-003 resolution; v0.41.0).

A reliability diagram is the per-bin companion to a Brier score. For
each ``domain_sector``, we group the resolutions in a rolling window
into probability bins (default 10 equal-width bins from 0 to 1),
compute the mean predicted probability and the empirical hit rate
within each bin, and emit those for the renderer to display.

A perfectly calibrated model has ``mean_predicted == empirical_rate``
in every bin. The ``calibration_gap`` field — ``empirical_rate -
mean_predicted`` — is the operator-readable signal: positive means
the model is *under-confident* in that bin (events happen more often
than predicted), negative means *over-confident*.

Returns a content dict shaped like::

    {
      "type": "reliability",
      "window_days": 90,
      "bins": [(0.0, 0.1), (0.1, 0.2), ..., (0.9, 1.0)],
      "min_resolutions_per_bin": 5,
      "sectors": [
        {
          "sector": "macroeconomic",
          "n_resolutions": 47,
          "window_days": 90,
          "bins": [
            {
              "bin_lo": 0.0,
              "bin_hi": 0.1,
              "n": 8,
              "mean_predicted": 0.07,
              "empirical_rate": 0.12,
              "calibration_gap": 0.05,
              "sparse": false,
            },
            ...
          ],
        },
        ...
      ],
    }

The default 10 bins of width 0.1 resolve OQ-RG-COMPAT-004 in the
supplement. Sectors with zero scoreable resolutions across all bins
are omitted entirely. Bins with fewer than ``min_resolutions_per_bin``
(default 5) get a ``sparse: True`` flag — operators should treat
those as noisy.

This section is **opt-in via** ``config/report.yaml`` —
``enabled_sections`` must list ``reliability`` for it to render.
The base spec triple keeps it off by default since v1 sectors will
typically not have enough resolutions to populate every bin
meaningfully in the first months. As the corpus grows, operators
can enable the section.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final

import duckdb

logger = logging.getLogger(__name__)


# 10 equal-width bins in [0, 1] resolve OQ-RG-COMPAT-004. Operators
# can override by passing a custom ``bins=`` to ``assemble`` (the
# config-file knob lives in ``thresholds.reliability_bin_count`` and
# generates the equal-width bins; non-equal-width bins are not yet a
# config option).
DEFAULT_BIN_COUNT: Final[int] = 10
DEFAULT_WINDOW_DAYS: Final[int] = 90
DEFAULT_MIN_RESOLUTIONS_PER_BIN: Final[int] = 5


def assemble(
    conn: duckdb.DuckDBPyConnection,
    *,
    since_ts: datetime,
    until_ts: datetime,
    bin_count: int = DEFAULT_BIN_COUNT,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_resolutions_per_bin: int = DEFAULT_MIN_RESOLUTIONS_PER_BIN,
    window_days_per_sector: Mapping[str, int] | None = None,
    bin_count_per_sector: Mapping[str, int] | None = None,
    min_resolutions_per_bin_per_sector: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Assemble per-sector reliability bins.

    The ``since_ts`` and ``until_ts`` parameters mirror every other
    section assembler's interface. ``since_ts`` is unused by the
    reliability computation (which always reads the rolling
    ``window_days`` ending at ``until_ts``) but is accepted so the
    generator dispatch can call this assembler the same way as the
    others.

    Per-sector overrides (DEFER-RG-COMPAT-002 follow-on; v0.40.0)
    let an operator widen the bin count for sectors with many
    resolutions per window or tighten ``min_resolutions_per_bin``
    where small samples are still informative. Sectors without an
    override use the global value transparently.
    """
    del since_ts  # rolling window only depends on window_days + until_ts

    per_sector_window = dict(window_days_per_sector or {})
    per_sector_bin_count = dict(bin_count_per_sector or {})
    per_sector_min_per_bin = dict(min_resolutions_per_bin_per_sector or {})

    # Read across the broadest window so we can apply per-sector
    # narrower windows in Python without running multiple queries.
    broadest_window = max([window_days, *per_sector_window.values()])
    window_start = until_ts - timedelta(days=broadest_window)
    rows = conn.execute(
        "SELECT ec.domain_sector, "
        "r.model_probability_at_comparison, r.outcome_observed, "
        "r.resolution_outcome, r.resolution_ts "
        "FROM comparison_resolutions r "
        "LEFT JOIN comparisons c ON r.comparison_id = c.comparison_id "
        "LEFT JOIN pl_event_classes ec ON c.class_id = ec.class_id "
        "WHERE r.resolution_ts >= ? AND r.resolution_ts <= ? "
        "AND r.resolution_outcome != 'invalid'",
        [window_start, until_ts],
    ).fetchall()

    by_sector: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for sector, model_p, outcome_observed, _outcome, resolution_ts in rows:
        if sector is None or model_p is None or outcome_observed is None or resolution_ts is None:
            continue
        sector_str = str(sector)
        applicable_window = per_sector_window.get(sector_str, window_days)
        if resolution_ts < until_ts - timedelta(days=applicable_window):
            continue
        by_sector[sector_str].append((float(model_p), int(outcome_observed)))

    sectors: list[dict[str, Any]] = []
    for sector_name in sorted(by_sector.keys()):
        observations = by_sector[sector_name]
        if not observations:
            continue
        applicable_window = per_sector_window.get(sector_name, window_days)
        applicable_bin_count = per_sector_bin_count.get(sector_name, bin_count)
        applicable_min_per_bin = per_sector_min_per_bin.get(sector_name, min_resolutions_per_bin)
        sector_bins = _equal_width_bins(applicable_bin_count)
        bin_summaries = _compute_bin_summaries(
            observations,
            bins=sector_bins,
            min_resolutions_per_bin=applicable_min_per_bin,
        )
        sectors.append(
            {
                "sector": sector_name,
                "n_resolutions": len(observations),
                "window_days": applicable_window,
                "bin_count": applicable_bin_count,
                "min_resolutions_per_bin": applicable_min_per_bin,
                "bins": bin_summaries,
            }
        )

    return {
        "type": "reliability",
        "window_days": window_days,
        "bins": _equal_width_bins(bin_count),
        "min_resolutions_per_bin": min_resolutions_per_bin,
        "sectors": sectors,
    }


# -- internals --------------------------------------------------------------


def _equal_width_bins(bin_count: int) -> list[tuple[float, float]]:
    """Build ``bin_count`` equal-width bins covering [0.0, 1.0].

    Bin ranges are half-open ``[lo, hi)`` except the last bin which
    is fully closed ``[lo, hi]`` so a probability of exactly 1.0
    lands in the top bin instead of going off the end.
    """
    if bin_count < 1:
        bin_count = 1
    width = 1.0 / bin_count
    return [(round(i * width, 4), round((i + 1) * width, 4)) for i in range(bin_count)]


def _compute_bin_summaries(
    observations: list[tuple[float, int]],
    *,
    bins: list[tuple[float, float]],
    min_resolutions_per_bin: int,
) -> list[dict[str, Any]]:
    """For each bin, compute n, mean_predicted, empirical_rate, gap."""
    summaries: list[dict[str, Any]] = []
    for bin_index, (bin_lo, bin_hi) in enumerate(bins):
        is_top_bin = bin_index == len(bins) - 1
        bin_obs = [
            (p, outcome)
            for (p, outcome) in observations
            if (bin_lo <= p < bin_hi) or (is_top_bin and p == bin_hi)
        ]
        if not bin_obs:
            summaries.append(
                {
                    "bin_lo": bin_lo,
                    "bin_hi": bin_hi,
                    "n": 0,
                    "mean_predicted": None,
                    "empirical_rate": None,
                    "calibration_gap": None,
                    "sparse": True,
                }
            )
            continue
        n = len(bin_obs)
        mean_p = sum(p for (p, _) in bin_obs) / n
        empirical = sum(outcome for (_, outcome) in bin_obs) / n
        summaries.append(
            {
                "bin_lo": bin_lo,
                "bin_hi": bin_hi,
                "n": n,
                "mean_predicted": round(mean_p, 4),
                "empirical_rate": round(empirical, 4),
                "calibration_gap": round(empirical - mean_p, 4),
                "sparse": n < min_resolutions_per_bin,
            }
        )
    return summaries


__all__ = [
    "DEFAULT_BIN_COUNT",
    "DEFAULT_MIN_RESOLUTIONS_PER_BIN",
    "DEFAULT_WINDOW_DAYS",
    "assemble",
]
