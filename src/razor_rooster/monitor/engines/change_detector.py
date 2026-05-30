"""Change-detection engine (T-MON-020; design §3.5).

Pure functions over numeric inputs. Magnitude classification follows
REQ-MON-DETECT-001 with per-sector thresholds:

- ``none`` when ``|delta| < minor_threshold``
- ``minor`` when ``minor_threshold <= |delta| < material_threshold``
- ``material`` when ``material_threshold <= |delta| < major_threshold``
- ``major`` when ``|delta| >= major_threshold``
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from razor_rooster.monitor.config.loader import ShiftBandConfig
from razor_rooster.monitor.models import (
    PrecursorSnapshot,
    ShiftBand,
    ShiftResult,
)


def classify_band(magnitude: float, bands: ShiftBandConfig) -> ShiftBand:
    """Map an absolute shift magnitude to a band label."""
    abs_mag = abs(magnitude)
    if abs_mag < bands.minor_threshold:
        return "none"
    if abs_mag < bands.material_threshold:
        return "minor"
    if abs_mag < bands.major_threshold:
        return "material"
    return "major"


def compute_model_shift(
    *,
    analysis_model_p: float,
    current_model_p: float | None,
    bands: ShiftBandConfig,
) -> ShiftResult:
    """Difference between current and analysis-time model probabilities."""
    if current_model_p is None:
        return ShiftResult(value=None, band=None)
    delta = float(current_model_p - analysis_model_p)
    return ShiftResult(value=delta, band=classify_band(delta, bands))


def compute_market_shift(
    *,
    analysis_market_p: float | None,
    current_market_p: float | None,
    bands: ShiftBandConfig,
) -> ShiftResult:
    """Difference between current and analysis-time market probabilities."""
    if analysis_market_p is None or current_market_p is None:
        return ShiftResult(value=None, band=None)
    delta = float(current_market_p - analysis_market_p)
    return ShiftResult(value=delta, band=classify_band(delta, bands))


def snapshot_precursors(
    *,
    analysis_precursors: Sequence[Mapping[str, Any]] | None,
    current_precursors: Sequence[Mapping[str, Any]] | None,
) -> list[PrecursorSnapshot]:
    """Build per-precursor snapshots comparing analysis-time to current.

    The ``analysis_precursors`` come from the embedded scanner trace
    captured at the time the analysis was produced; ``current_precursors``
    come from the latest scan trace for the same class. The function
    pairs them by ``variable_id`` and tags each pair with whether the
    threshold-crossing state changed.
    """
    out: list[PrecursorSnapshot] = []
    if not analysis_precursors:
        return out
    current_index = {
        str(entry.get("variable_id")): entry
        for entry in (current_precursors or [])
        if isinstance(entry, Mapping)
    }
    for entry in analysis_precursors:
        if not isinstance(entry, Mapping):
            continue
        variable_id = str(entry.get("variable_id", ""))
        title = str(entry.get("title", variable_id))
        threshold_raw = entry.get("threshold")
        threshold = float(threshold_raw) if threshold_raw is not None else None
        direction = str(entry.get("direction", "high_signals_event"))
        analysis_value_raw = entry.get("current_value")
        analysis_value = float(analysis_value_raw) if analysis_value_raw is not None else None
        analysis_fired = bool(entry.get("fired", False))
        current_entry = current_index.get(variable_id)
        if current_entry is not None:
            current_value_raw = current_entry.get("current_value")
            current_value = float(current_value_raw) if current_value_raw is not None else None
            current_fired = bool(current_entry.get("fired", False))
        else:
            current_value = None
            current_fired = analysis_fired
        out.append(
            PrecursorSnapshot(
                variable_id=variable_id,
                title=title,
                threshold=threshold,
                direction=direction,
                analysis_value=analysis_value,
                current_value=current_value,
                analysis_fired=analysis_fired,
                current_fired=current_fired,
            )
        )
    return out


def precursor_snapshot_to_dict(snapshot: PrecursorSnapshot) -> dict[str, Any]:
    """Serialize a PrecursorSnapshot for JSON storage."""
    return {
        "variable_id": snapshot.variable_id,
        "title": snapshot.title,
        "threshold": snapshot.threshold,
        "direction": snapshot.direction,
        "analysis_value": snapshot.analysis_value,
        "current_value": snapshot.current_value,
        "analysis_fired": snapshot.analysis_fired,
        "current_fired": snapshot.current_fired,
        "threshold_crossed": snapshot.threshold_crossed,
    }


__all__ = [
    "classify_band",
    "compute_market_shift",
    "compute_model_shift",
    "precursor_snapshot_to_dict",
    "snapshot_precursors",
]
