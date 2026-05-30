"""Threshold-suggestion engine (T-RG-COMPAT-SUGG-001 v0.41.0).

Reads the historical ``report_threshold_measurements`` table and
produces percentile-target → suggested-threshold mappings for
each shipped measurement kind. Operators use this to see what
threshold value would make a section land at, say, the p70 of
their corpus's distribution.

Strictly descriptive in its read path. The suggestions tell the
operator where percentile cuts fall in the empirical
distribution; they never direct the operator to apply them.

v0.42.0 adds an opt-in *write* path
(``apply_threshold_suggestion``) that lets the operator commit
a suggestion back to ``config/report.yaml``. The write path is
gated by an explicit ``--apply`` CLI flag and operator
confirmation, and always writes a timestamped backup before
modifying the config. The CLI prompt phrases the change as
"Apply suggested value X to <knob>?" — the operator decides
whether the suggestion fits their framing.

Algorithm (read path):

1. Read the most recent ``lookback_cycles`` measurements for
   the requested kind.
2. Pool every percentile cut across every cycle into one
   merged sample (each cycle contributes its own percentile
   values, weighted equally).
3. Compute summary stats over the pooled sample (mean of each
   percentile cut across cycles).
4. For each requested target percentile (default 0.50, 0.70,
   0.90), look up the corresponding pooled value and emit a
   ``SuggestedThreshold``.

We intentionally do *not* re-aggregate the underlying
observations across cycles (that data is in
``distribution_json.percentiles`` only — the raw observations
aren't persisted by design to keep the table small). Instead we
average the recorded percentile cuts. This is a reasonable
approximation for stable distributions; the engine surfaces
``cycles`` and ``cycles_with_data`` so the operator can see
how stable the distribution was during the lookback window.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import duckdb
import yaml

from razor_rooster.report_generator.engines.measurements import (
    MEASUREMENT_KIND_BRIER_PER_SECTOR,
    MEASUREMENT_KIND_CROSS_VENUE_SPREAD_BPS,
    MEASUREMENT_KIND_SINGLE_VENUE_DOMINANCE_SHARE,
)
from razor_rooster.report_generator.persistence.operations import (
    ThresholdMeasurementRecord,
    list_threshold_measurements,
)

logger = logging.getLogger(__name__)


DEFAULT_LOOKBACK_CYCLES: Final[int] = 30
DEFAULT_TARGET_PERCENTILES: Final[tuple[float, ...]] = (0.50, 0.70, 0.90)
# Stability threshold: when the average coefficient of variation
# across percentile cuts exceeds this value, the suggestion engine
# flags the result as ``unstable=True`` so operators don't tune to
# noise. 0.5 means the percentile values are bouncing by half their
# own value cycle-to-cycle. (T-RG-COMPAT-SUGG-003 v0.42.0).
DEFAULT_STABILITY_CV_THRESHOLD: Final[float] = 0.5


# Map measurement kinds → config-file knob names. Only the four
# global knobs are writable from the suggest-thresholds CLI;
# per-sector overrides are out of scope for ``--apply`` (operators
# edit those by hand).
KIND_TO_CONFIG_KNOB: Final[dict[str, str]] = {
    MEASUREMENT_KIND_CROSS_VENUE_SPREAD_BPS: "cross_venue_spread_bps",
    MEASUREMENT_KIND_SINGLE_VENUE_DOMINANCE_SHARE: "single_venue_dominance_pct",
    MEASUREMENT_KIND_BRIER_PER_SECTOR: "brier_miscalibration",
}


# Measurement kinds whose config knobs are integer-typed. Used by
# the apply path to coerce the suggested float value to int before
# writing it back.
INTEGER_VALUED_KNOBS: Final[frozenset[str]] = frozenset({"cross_venue_spread_bps"})


@dataclass(frozen=True, slots=True)
class SuggestedThreshold:
    """One percentile-target → suggested-threshold pair."""

    target_percentile: float
    suggested_value: float
    cycles: int
    cycles_with_data: int


@dataclass(frozen=True, slots=True)
class ThresholdSuggestionReport:
    """Result of ``suggest_thresholds`` for one measurement kind."""

    measurement_kind: str
    cycles_inspected: int
    cycles_with_data: int
    current_threshold: float | None
    suggestions: tuple[SuggestedThreshold, ...]
    stability_cv: float | None = None
    unstable: bool = False


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Result of an ``apply_threshold_suggestion`` write.

    ``backup_path`` is the file the previous config was copied to
    before the new value was written. ``previous_value`` echoes
    the value that was overwritten so callers can render a
    diff-shaped confirmation in their output.
    """

    config_path: Path
    backup_path: Path
    knob: str
    previous_value: float | None
    new_value: float


def suggest_thresholds(
    conn: duckdb.DuckDBPyConnection,
    *,
    measurement_kind: str,
    lookback_cycles: int = DEFAULT_LOOKBACK_CYCLES,
    target_percentiles: tuple[float, ...] = DEFAULT_TARGET_PERCENTILES,
    stability_cv_threshold: float = DEFAULT_STABILITY_CV_THRESHOLD,
) -> ThresholdSuggestionReport:
    """Suggest thresholds for one measurement kind.

    Reads the most recent ``lookback_cycles`` measurements,
    averages their per-percentile values across cycles, and emits
    one ``SuggestedThreshold`` per ``target_percentile``. Cycles
    with zero observations are counted in ``cycles_inspected``
    but contribute nothing to ``cycles_with_data`` and are
    skipped during averaging.

    When no cycle has data, the returned report has empty
    ``suggestions`` and ``cycles_with_data == 0``.

    The most recent measurement's ``configured_threshold`` is
    echoed back as ``current_threshold`` so the caller can show
    where the operator currently sits.

    A stability metric (``stability_cv``) is computed alongside
    the suggestions: per percentile cut, take the standard
    deviation across cycles and divide by the mean to get the
    coefficient of variation; then average across cuts. Higher
    values mean the percentile values are bouncing more relative
    to their typical magnitude. When ``stability_cv >
    stability_cv_threshold`` (default 0.5), ``unstable`` is set
    true so the CLI can flag the result. With fewer than 2
    cycles of data, ``stability_cv`` is ``None``.
    """
    rows = list_threshold_measurements(
        conn,
        measurement_kind=measurement_kind,
        limit=lookback_cycles,
    )
    cycles_inspected = len(rows)
    cycles_with_data = sum(1 for r in rows if r.n_observations > 0)
    current_threshold = rows[0].configured_threshold if rows else None

    if cycles_with_data == 0:
        return ThresholdSuggestionReport(
            measurement_kind=measurement_kind,
            cycles_inspected=cycles_inspected,
            cycles_with_data=0,
            current_threshold=current_threshold,
            suggestions=(),
            stability_cv=None,
            unstable=False,
        )

    averaged = _average_percentiles(rows)
    stability_cv = _stability_cv(rows)
    unstable = stability_cv is not None and stability_cv > stability_cv_threshold
    suggestions: list[SuggestedThreshold] = []
    for target in target_percentiles:
        suggested = _interpolate_at_target(averaged, target=target)
        if suggested is None:
            continue
        suggestions.append(
            SuggestedThreshold(
                target_percentile=target,
                suggested_value=suggested,
                cycles=cycles_inspected,
                cycles_with_data=cycles_with_data,
            )
        )

    return ThresholdSuggestionReport(
        measurement_kind=measurement_kind,
        cycles_inspected=cycles_inspected,
        cycles_with_data=cycles_with_data,
        current_threshold=current_threshold,
        suggestions=tuple(suggestions),
        stability_cv=stability_cv,
        unstable=unstable,
    )


# -- internals --------------------------------------------------------------


def _average_percentiles(
    rows: tuple[ThresholdMeasurementRecord, ...],
) -> list[tuple[float, float]]:
    """Average each percentile cut across the rows that have data.

    Returns a sorted list of (q, mean_value) pairs. Cycles with
    zero observations are skipped; cycles missing a particular
    percentile cut are skipped for that cut only (so a single
    malformed row doesn't pollute the average).
    """
    sums: dict[float, float] = {}
    counts: dict[float, int] = {}
    for record in rows:
        if record.n_observations == 0:
            continue
        percentiles = record.distribution.get("percentiles") or {}
        if not isinstance(percentiles, dict):
            continue
        for key, value in percentiles.items():
            if value is None:
                continue
            try:
                q = float(key)
                v = float(value)
            except (TypeError, ValueError):
                continue
            sums[q] = sums.get(q, 0.0) + v
            counts[q] = counts.get(q, 0) + 1
    averaged: list[tuple[float, float]] = []
    for q in sorted(sums):
        n = counts.get(q, 0)
        if n == 0:
            continue
        averaged.append((q, sums[q] / n))
    return averaged


def _stability_cv(
    rows: tuple[ThresholdMeasurementRecord, ...],
) -> float | None:
    """Compute the average coefficient of variation across percentile cuts.

    For each percentile cut (e.g. p50), gather the cut's values
    across cycles with data; compute mean and population standard
    deviation; divide stddev by mean to get the CV. Average the
    per-cut CVs to get one stability number per kind.

    Returns ``None`` when fewer than 2 cycles have data (you need
    at least two points to measure variation), or when every
    per-cut mean is zero (avoid divide-by-zero).
    """
    by_q: dict[float, list[float]] = {}
    for record in rows:
        if record.n_observations == 0:
            continue
        percentiles = record.distribution.get("percentiles") or {}
        if not isinstance(percentiles, dict):
            continue
        for key, value in percentiles.items():
            if value is None:
                continue
            try:
                q = float(key)
                v = float(value)
            except (TypeError, ValueError):
                continue
            by_q.setdefault(q, []).append(v)

    if not by_q:
        return None
    cvs: list[float] = []
    for values in by_q.values():
        n = len(values)
        if n < 2:
            continue
        mean = sum(values) / n
        if mean == 0.0:
            # Skip cuts whose mean is zero (the CV is undefined).
            continue
        variance = sum((v - mean) ** 2 for v in values) / n
        stddev = variance**0.5
        cvs.append(stddev / abs(mean))
    if not cvs:
        return None
    return round(sum(cvs) / len(cvs), 4)


def _interpolate_at_target(averaged: list[tuple[float, float]], *, target: float) -> float | None:
    """Linear interpolation between recorded percentile cuts.

    For ``target = 0.7`` and recorded percentiles at
    ``[(0.5, 100), (0.75, 200), (0.9, 350)]``, the function
    interpolates between p50 and p75: ``100 + (0.7 - 0.5) /
    (0.75 - 0.5) * (200 - 100) = 180``.

    When the target is outside the recorded range, returns the
    nearest available value (clamped to the bottom or top
    percentile).
    """
    if not averaged:
        return None
    target = max(0.0, min(1.0, float(target)))
    # Below or at the lowest q.
    if target <= averaged[0][0]:
        return averaged[0][1]
    # At or above the highest q.
    if target >= averaged[-1][0]:
        return averaged[-1][1]
    # Walk forward to find the bracketing pair.
    for i in range(len(averaged) - 1):
        q_lo, v_lo = averaged[i]
        q_hi, v_hi = averaged[i + 1]
        if q_lo <= target <= q_hi:
            if q_hi == q_lo:
                return v_lo
            weight = (target - q_lo) / (q_hi - q_lo)
            return v_lo + weight * (v_hi - v_lo)
    return None


# -- write path (T-RG-COMPAT-SUGG-002 v0.42.0) -----------------------------


class ApplyError(RuntimeError):
    """Raised when ``apply_threshold_suggestion`` cannot proceed.

    Reasons include: unknown measurement kind, refusal to silence
    a guard rail (e.g. ``target_pct >= 1.0`` for the dominance
    threshold), missing config file, malformed YAML, or unwritable
    backup target. The CLI surfaces the error message verbatim.
    """


def apply_threshold_suggestion(
    *,
    config_path: Path,
    measurement_kind: str,
    new_value: float,
    target_percentile: float | None = None,
    now: datetime | None = None,
) -> ApplyResult:
    """Write a suggested threshold value back to ``config/report.yaml``.

    Behavior:

    - Refuses to act on measurement kinds that aren't writable
      (only the four global knobs are wired; per-sector
      overrides remain operator-edited by hand).
    - For ``single_venue_dominance_share`` specifically, refuses
      ``target_percentile >= 1.0`` because a value of 1.0 would
      effectively silence the dominance warning entirely (no
      venue holds *strictly* greater than 100% of volume).
      Operators who genuinely want that posture can edit the
      YAML by hand.
    - Refuses if the config file does not exist; the operator
      should run the standard install steps before tuning.
    - Coerces integer-valued knobs (e.g. ``cross_venue_spread_bps``)
      to int with rounding so the YAML stays clean.
    - Saves the existing config to
      ``config/report.yaml.bak.<ISO timestamp>`` before
      overwriting so the change is reversible.
    - Preserves the existing structure: only the targeted nested
      key inside ``thresholds:`` is changed; other sections of
      the YAML survive untouched. Comments are *not* preserved
      (PyYAML doesn't round-trip them); the timestamped backup
      retains the original including comments.

    Returns the :class:`ApplyResult` describing the write so the
    CLI can echo a confirmation diff.
    """
    if measurement_kind not in KIND_TO_CONFIG_KNOB:
        raise ApplyError(
            f"measurement_kind {measurement_kind!r} is not writable; "
            f"only {sorted(KIND_TO_CONFIG_KNOB)} can be applied"
        )
    knob = KIND_TO_CONFIG_KNOB[measurement_kind]
    if (
        measurement_kind == MEASUREMENT_KIND_SINGLE_VENUE_DOMINANCE_SHARE
        and target_percentile is not None
        and target_percentile >= 1.0
    ):
        raise ApplyError(
            "refusing to apply target_percentile >= 1.0 to "
            "single_venue_dominance_share — would silence the "
            "dominance warning entirely. Edit config/report.yaml "
            "by hand if that's the intended posture."
        )
    if not config_path.exists():
        raise ApplyError(
            f"config file not found at {config_path}. Run "
            f"`razor-rooster ingest init` first if this is a fresh "
            f"install."
        )
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            decoded = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise ApplyError(f"failed to parse {config_path}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ApplyError(
            f"unexpected top-level shape in {config_path}: "
            f"expected mapping, got {type(decoded).__name__}"
        )
    thresholds_section = decoded.get("thresholds")
    if not isinstance(thresholds_section, dict):
        thresholds_section = {}
        decoded["thresholds"] = thresholds_section
    previous_raw = thresholds_section.get(knob)
    previous_value: float | None
    if previous_raw is None:
        previous_value = None
    else:
        try:
            previous_value = float(previous_raw)
        except (TypeError, ValueError):
            previous_value = None
    coerced_new: float
    if knob in INTEGER_VALUED_KNOBS:
        coerced_new = float(round(new_value))
        thresholds_section[knob] = round(new_value)
    else:
        coerced_new = float(new_value)
        thresholds_section[knob] = coerced_new

    timestamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = config_path.with_suffix(config_path.suffix + f".bak.{timestamp}")
    shutil.copy2(config_path, backup_path)
    try:
        with config_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(
                decoded,
                handle,
                sort_keys=False,
                default_flow_style=False,
            )
    except OSError as exc:
        # Restore from backup so a failed write doesn't leave the
        # operator with a half-written config.
        shutil.copy2(backup_path, config_path)
        raise ApplyError(f"failed to write {config_path}: {exc}") from exc

    return ApplyResult(
        config_path=config_path,
        backup_path=backup_path,
        knob=knob,
        previous_value=previous_value,
        new_value=coerced_new,
    )


def compute_apply_diff(
    *,
    config_path: Path,
    measurement_kind: str,
    new_value: float,
) -> str:
    """Return a unified-diff-style preview of an upcoming ``--apply`` write.

    Used by the ``--diff`` flag on ``suggest-thresholds --apply`` so
    operators see the YAML-level change before they confirm. Pure
    function — does not touch the live config.

    Output format::

        --- config/report.yaml
        +++ config/report.yaml (proposed)
        @@ thresholds.<knob> @@
        - thresholds.<knob>: <old>
        + thresholds.<knob>: <new>

    When the knob is missing entirely, the ``-`` line shows
    ``(unset)``. When the file doesn't exist, returns a one-line
    explanation rather than raising — the caller will fail later
    via ``apply_threshold_suggestion`` itself.
    """
    if measurement_kind not in KIND_TO_CONFIG_KNOB:
        return f"(diff unavailable: {measurement_kind} is not writable)"
    knob = KIND_TO_CONFIG_KNOB[measurement_kind]
    if not config_path.exists():
        return f"(diff unavailable: {config_path} does not exist)"
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            decoded = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        return f"(diff unavailable: {exc})"
    previous_value: object | None = None
    if isinstance(decoded, dict):
        thresholds_section = decoded.get("thresholds")
        if isinstance(thresholds_section, dict):
            previous_value = thresholds_section.get(knob)
    if knob in INTEGER_VALUED_KNOBS:
        new_rendered: object = round(new_value)
    else:
        new_rendered = float(new_value)
    old_rendered = "(unset)" if previous_value is None else _format_yaml_scalar(previous_value)
    new_str = _format_yaml_scalar(new_rendered)
    return (
        f"--- {config_path}\n"
        f"+++ {config_path} (proposed)\n"
        f"@@ thresholds.{knob} @@\n"
        f"- thresholds.{knob}: {old_rendered}\n"
        f"+ thresholds.{knob}: {new_str}"
    )


def _format_yaml_scalar(value: object) -> str:
    """Render a Python value as it would appear in the YAML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return str(value)


# -- undo path (T-RG-COMPAT-UNDO-001 v0.44.0) ------------------------------


@dataclass(frozen=True, slots=True)
class UndoResult:
    """Result of a successful ``undo_tuning_log_entry`` call.

    ``current_backup_path`` is the timestamped backup of the
    pre-undo config we wrote before applying the historical
    backup; the operator can revert the undo by copying it
    back. ``restored_from`` is the historical backup that was
    used as the source.
    """

    config_path: Path
    current_backup_path: Path
    restored_from: Path
    log_id_undone: str


def undo_tuning_log_entry(
    *,
    config_path: Path,
    backup_path: Path,
    log_id: str,
    now: datetime | None = None,
) -> UndoResult:
    """Restore a config file from a tuning-log backup.

    The flow:

    1. The current ``config/report.yaml`` is copied to a fresh
       ``.bak.<timestamp>`` file so the undo itself is reversible
       (you can re-apply by copying the new backup back over the
       live config).
    2. The historical ``backup_path`` (recorded in the
       tuning-log entry) is copied over the live config.

    Refuses if the historical backup file is missing or the live
    config doesn't exist. Refuses if the backup_path argument
    is None — entries written before v0.43.0's tuning-log shipped
    don't have a backup pointer.
    """
    if not config_path.exists():
        raise ApplyError(f"config file not found at {config_path}; nothing to undo")
    if not backup_path.exists():
        raise ApplyError(
            f"backup file {backup_path} from tuning-log entry "
            f"{log_id!r} no longer exists; cannot undo"
        )
    timestamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    pre_undo_backup = config_path.with_suffix(config_path.suffix + f".bak.{timestamp}")
    shutil.copy2(config_path, pre_undo_backup)
    try:
        shutil.copy2(backup_path, config_path)
    except OSError as exc:
        # Restore from the pre-undo backup so a failed copy
        # doesn't leave the operator with a half-applied undo.
        shutil.copy2(pre_undo_backup, config_path)
        raise ApplyError(f"failed to copy {backup_path} → {config_path}: {exc}") from exc
    return UndoResult(
        config_path=config_path,
        current_backup_path=pre_undo_backup,
        restored_from=backup_path,
        log_id_undone=log_id,
    )


__all__ = [
    "DEFAULT_LOOKBACK_CYCLES",
    "DEFAULT_STABILITY_CV_THRESHOLD",
    "DEFAULT_TARGET_PERCENTILES",
    "INTEGER_VALUED_KNOBS",
    "KIND_TO_CONFIG_KNOB",
    "ApplyError",
    "ApplyResult",
    "SuggestedThreshold",
    "ThresholdSuggestionReport",
    "UndoResult",
    "apply_threshold_suggestion",
    "compute_apply_diff",
    "suggest_thresholds",
    "undo_tuning_log_entry",
]
