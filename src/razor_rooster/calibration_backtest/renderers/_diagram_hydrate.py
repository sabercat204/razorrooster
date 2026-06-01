"""Shared rehydration of :class:`ReliabilityDiagram` from persisted runs.

Phase 5's HTML renderer (T-CB-029) and the Phase 6 GUI detail view
(T-CB-037) both need to rebuild typed
:class:`razor_rooster.calibration_backtest.models.ReliabilityDiagram`
instances from the dict shape persisted in
``backtest_runs.summary_json`` (the structure produced by
:func:`razor_rooster.calibration_backtest.models._reliability_diagram_to_mapping`).

To avoid two copies of the hydration code drifting, the logic lives
here and both consumers import the helpers via aliased imports.
Module-level constants mirror the
:meth:`ScoreSummary.as_mapping` schema so a future change to the
persistence shape only has to update one site.

The helpers are intentionally tolerant: malformed payload entries (a
non-dict ``reliability_diagrams`` value, a bin missing required keys,
etc.) are silently skipped or return ``None``. The operator surfaces
malformed runs via the JSON renderer instead, which preserves the raw
payload byte-for-byte.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from razor_rooster.calibration_backtest.errors import BacktestConfigError
from razor_rooster.calibration_backtest.models import (
    BacktestRun,
    ReliabilityBin,
    ReliabilityDiagram,
)

__all__ = [
    "hydrate_diagram",
    "reliability_diagrams_from_run",
]


def reliability_diagrams_from_run(run: BacktestRun) -> dict[str, ReliabilityDiagram]:
    """Hydrate ``ReliabilityDiagram`` objects from a persisted summary.

    The summary's reliability-diagram payload is the dict shape produced
    by :func:`models._reliability_diagram_to_mapping`. Rebuilds typed
    :class:`ReliabilityDiagram` instances so downstream consumers
    (HTML renderer, GUI detail view) consume the same value type the
    in-memory scoring path emits. Malformed entries are silently
    skipped — operators surface them via the JSON renderer instead.

    Args:
        run: The :class:`BacktestRun` row whose ``summary_json`` carries
            the persisted reliability diagrams.

    Returns:
        A mapping from sector to typed :class:`ReliabilityDiagram`. The
        mapping is empty if the summary is missing, malformed, or
        carries no ``reliability_diagrams`` key.
    """

    summary = run.summary_json or {}
    if not isinstance(summary, dict):
        return {}
    raw = summary.get("reliability_diagrams")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, ReliabilityDiagram] = {}
    for sector, payload in raw.items():
        diagram = hydrate_diagram(payload)
        if diagram is not None:
            out[str(sector)] = diagram
    return out


def hydrate_diagram(payload: Mapping[str, Any] | Any) -> ReliabilityDiagram | None:
    """Rebuild a :class:`ReliabilityDiagram` from one persisted entry.

    Returns ``None`` when ``payload`` is not a dict, is missing the
    ``bin_count`` / ``bins`` keys, contains a non-dict bin entry, or
    fails the model validators (e.g. ``bin_count < 2`` raises
    :class:`BacktestConfigError`, which is caught here and surfaced as
    ``None``).

    Args:
        payload: One sector's persisted reliability-diagram payload —
            the dict shape produced by
            :func:`models._reliability_diagram_to_mapping`.

    Returns:
        The typed diagram, or ``None`` when the payload is malformed.
    """

    if not isinstance(payload, dict):
        return None
    bin_count = payload.get("bin_count")
    bins_raw = payload.get("bins")
    if not isinstance(bin_count, int) or not isinstance(bins_raw, list):
        return None
    bins: list[ReliabilityBin] = []
    for entry in bins_raw:
        if not isinstance(entry, dict):
            return None
        try:
            bins.append(
                ReliabilityBin(
                    lower_p=float(entry["lower_p"]),
                    upper_p=float(entry["upper_p"]),
                    count=int(entry["count"]),
                    mean_predicted_p=(
                        float(entry["mean_predicted_p"])
                        if entry.get("mean_predicted_p") is not None
                        else None
                    ),
                    empirical_rate=(
                        float(entry["empirical_rate"])
                        if entry.get("empirical_rate") is not None
                        else None
                    ),
                )
            )
        except (KeyError, TypeError, ValueError, BacktestConfigError):
            return None
    try:
        return ReliabilityDiagram(bin_count=bin_count, bins=tuple(bins))
    except BacktestConfigError:
        return None
