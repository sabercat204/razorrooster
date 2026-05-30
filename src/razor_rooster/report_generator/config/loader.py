"""Report-generator configuration loader (T-RG-001; design §3.4).

Threshold knobs added in v0.39.0 (DEFER-RG-COMPAT-001 resolution):
``thresholds.cross_venue_spread_bps``,
``thresholds.single_venue_dominance_pct``,
``thresholds.brier_window_days``,
``thresholds.brier_miscalibration``.

Per-sector overrides added in v0.40.0 (DEFER-RG-COMPAT-002
resolution): each of the four global knobs can be shadowed per
``domain_sector`` via ``thresholds.<knob>_per_sector: {sector: value}``.
A missing per-sector entry falls back to the global value; an
out-of-range or invalid per-sector value falls back to the global
value with a warning logged.

When a knob is missing or invalid, the loader falls back to the
module-level constants in the corresponding section assembler so
existing operator setups keep working without any config edit.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from razor_rooster.report_generator.engines.section_assemblers.calibration import (
    DEFAULT_BRIER_WINDOW_DAYS,
    DEFAULT_MISCALIBRATION_THRESHOLD,
)
from razor_rooster.report_generator.engines.section_assemblers.cross_venue import (
    DEFAULT_SPREAD_THRESHOLD_BPS,
)
from razor_rooster.report_generator.engines.section_assemblers.reliability import (
    DEFAULT_BIN_COUNT,
    DEFAULT_MIN_RESOLUTIONS_PER_BIN,
)

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("config") / "report.yaml"

# Mirrors the in-module default in surfaced.py. Kept here as well so
# the loader can read it without importing the surfaced assembler
# (which has heavier transitive imports).
DEFAULT_SINGLE_VENUE_DOMINANCE_PCT: float = 0.80


# All sections supported by the renderer. Order is the rendering order
# (the header and footer always wrap the others); the toggle list in
# ``ReportConfig.enabled_sections`` only controls the body sections.
#
# ``reliability`` (DEFER-RG-COMPAT-003) is opt-in: it sits between
# ``calibration`` and ``watchlist`` when enabled. Default config does
# not enable it because v1 sectors typically lack enough resolutions
# to populate every bin meaningfully in the first months.
#
# ``recent_tuning`` (T-RG-COMPAT-RECENT-001) is opt-in too: it sits
# between ``system_health`` and ``surfaced`` when enabled. Most days
# have no tuning to show, so enabling it by default would be noise.
#
# ``at_a_glance`` (T-RG-COMPAT-GLANCE-001) is also opt-in. It sits
# at the very top of the body and lifts the top item from each
# major section's already-ordered list. Default workspace config
# does not enable it because the same items already appear at the
# top of their own sections; the at-a-glance section is for
# operators who want a one-screen navigation view.
ALL_SECTIONS: tuple[str, ...] = (
    "at_a_glance",
    "system_health",
    "recent_tuning",
    "surfaced",
    "cross_venue",
    "watched",
    "calibration",
    "reliability",
    "watchlist",
)

VerbosityLevel = Literal["full", "compact"]


@dataclass(frozen=True, slots=True)
class ReportThresholds:
    """Multi-venue calibration thresholds (DEFER-RG-COMPAT-001 + 002).

    The four global knobs are operator-tunable in
    ``config/report.yaml`` under the ``thresholds:`` block. Per-sector
    overrides shadow the global value for one ``domain_sector`` at a
    time and are accessed via the ``*_for_sector`` helpers, which
    transparently fall back to the global value when no override is
    present.

    Defaults match the module-level constants in the matching section
    assembler so operators who don't set them explicitly get the same
    v0.38.0 behavior.
    """

    cross_venue_spread_bps: int = DEFAULT_SPREAD_THRESHOLD_BPS
    single_venue_dominance_pct: float = DEFAULT_SINGLE_VENUE_DOMINANCE_PCT
    brier_window_days: int = DEFAULT_BRIER_WINDOW_DAYS
    brier_miscalibration: float = DEFAULT_MISCALIBRATION_THRESHOLD
    reliability_bin_count: int = DEFAULT_BIN_COUNT
    reliability_min_resolutions_per_bin: int = DEFAULT_MIN_RESOLUTIONS_PER_BIN

    cross_venue_spread_bps_per_sector: Mapping[str, int] = field(default_factory=dict)
    single_venue_dominance_pct_per_sector: Mapping[str, float] = field(default_factory=dict)
    brier_window_days_per_sector: Mapping[str, int] = field(default_factory=dict)
    brier_miscalibration_per_sector: Mapping[str, float] = field(default_factory=dict)
    reliability_bin_count_per_sector: Mapping[str, int] = field(default_factory=dict)
    reliability_min_resolutions_per_bin_per_sector: Mapping[str, int] = field(default_factory=dict)

    def cross_venue_spread_bps_for_sector(self, sector: str | None) -> int:
        if sector and sector in self.cross_venue_spread_bps_per_sector:
            return self.cross_venue_spread_bps_per_sector[sector]
        return self.cross_venue_spread_bps

    def single_venue_dominance_pct_for_sector(self, sector: str | None) -> float:
        if sector and sector in self.single_venue_dominance_pct_per_sector:
            return self.single_venue_dominance_pct_per_sector[sector]
        return self.single_venue_dominance_pct

    def brier_window_days_for_sector(self, sector: str | None) -> int:
        if sector and sector in self.brier_window_days_per_sector:
            return self.brier_window_days_per_sector[sector]
        return self.brier_window_days

    def brier_miscalibration_for_sector(self, sector: str | None) -> float:
        if sector and sector in self.brier_miscalibration_per_sector:
            return self.brier_miscalibration_per_sector[sector]
        return self.brier_miscalibration

    def reliability_bin_count_for_sector(self, sector: str | None) -> int:
        if sector and sector in self.reliability_bin_count_per_sector:
            return self.reliability_bin_count_per_sector[sector]
        return self.reliability_bin_count

    def reliability_min_resolutions_per_bin_for_sector(self, sector: str | None) -> int:
        if sector and sector in self.reliability_min_resolutions_per_bin_per_sector:
            return self.reliability_min_resolutions_per_bin_per_sector[sector]
        return self.reliability_min_resolutions_per_bin


@dataclass(frozen=True, slots=True)
class ReportConfig:
    """Aggregate report-generator config."""

    enabled_sections: tuple[str, ...] = field(default_factory=lambda: ALL_SECTIONS)
    verbosity: dict[str, VerbosityLevel] = field(default_factory=dict)
    calibration_first_run_lookback_days: int = 30
    thresholds: ReportThresholds = field(default_factory=ReportThresholds)
    auto_prune: AutoPruneConfig = field(default_factory=lambda: AutoPruneConfig())

    def is_enabled(self, section: str) -> bool:
        return section in self.enabled_sections

    def verbosity_for(self, section: str) -> VerbosityLevel:
        return self.verbosity.get(section, "full")


@dataclass(frozen=True, slots=True)
class AutoPruneConfig:
    """Auto-prune knobs for ``report_threshold_measurements``.

    When ``enabled`` is True, every successful report cycle
    deletes measurement rows that match either of two
    optional retention strategies:

    - ``older_than_days``: delete rows whose ``measured_at``
      is older than ``now - older_than_days``.
    - ``keep_last``: keep only the newest ``keep_last`` rows
      per measurement kind; older rows die.

    Both strategies stack: rows are deleted when *either*
    condition fires. ``enabled`` defaults to False so the
    table grows unbounded by default — operators opt in.
    """

    enabled: bool = False
    older_than_days: int | None = 365
    keep_last: int | None = None


def load_config(path: Path | None = None) -> ReportConfig:
    target = path or DEFAULT_CONFIG_PATH
    payload: dict[str, Any] = {}
    if target.exists():
        with target.open("r", encoding="utf-8") as handle:
            decoded = yaml.safe_load(handle)
            if isinstance(decoded, dict):
                payload = decoded
    raw_enabled: Iterable[Any] = payload.get("enabled_sections") or ALL_SECTIONS
    enabled = tuple(str(s) for s in raw_enabled if isinstance(s, str) and s in ALL_SECTIONS)
    verbosity_raw = payload.get("verbosity") or {}
    verbosity: dict[str, VerbosityLevel] = {}
    if isinstance(verbosity_raw, dict):
        for section, level in verbosity_raw.items():
            if not isinstance(section, str):
                continue
            level_str = str(level).strip().lower()
            if level_str in ("full", "compact"):
                # Cast: the literal narrowing is what mypy wants.
                lvl: VerbosityLevel = "full" if level_str == "full" else "compact"
                verbosity[section] = lvl
    lookback = int(payload.get("calibration_first_run_lookback_days", 30))
    thresholds = _load_thresholds(payload.get("thresholds"))
    auto_prune = _load_auto_prune(payload.get("auto_prune"))
    return ReportConfig(
        enabled_sections=enabled,
        verbosity=verbosity,
        calibration_first_run_lookback_days=lookback,
        thresholds=thresholds,
        auto_prune=auto_prune,
    )


# -- internals --------------------------------------------------------------


def _load_thresholds(raw: Any) -> ReportThresholds:
    """Parse the optional ``thresholds:`` block.

    Bad values fall back to the matching section-assembler default
    silently with a warning log. We don't refuse a report just
    because one knob is wrong; the rest of the config can still be
    valid.
    """
    if not isinstance(raw, dict):
        return ReportThresholds()

    spread = _coerce_int_in_range(
        raw.get("cross_venue_spread_bps"),
        default=DEFAULT_SPREAD_THRESHOLD_BPS,
        lo=0,
        hi=10_000,
        label="thresholds.cross_venue_spread_bps",
    )
    dominance = _coerce_float_in_range(
        raw.get("single_venue_dominance_pct"),
        default=DEFAULT_SINGLE_VENUE_DOMINANCE_PCT,
        lo=0.0,
        hi=1.0,
        label="thresholds.single_venue_dominance_pct",
    )
    window = _coerce_int_in_range(
        raw.get("brier_window_days"),
        default=DEFAULT_BRIER_WINDOW_DAYS,
        lo=1,
        hi=3650,
        label="thresholds.brier_window_days",
    )
    miscal = _coerce_float_in_range(
        raw.get("brier_miscalibration"),
        default=DEFAULT_MISCALIBRATION_THRESHOLD,
        lo=0.0,
        hi=1.0,
        label="thresholds.brier_miscalibration",
    )
    spread_per_sector = _load_per_sector_int(
        raw.get("cross_venue_spread_bps_per_sector"),
        global_value=spread,
        lo=0,
        hi=10_000,
        label="thresholds.cross_venue_spread_bps_per_sector",
    )
    dominance_per_sector = _load_per_sector_float(
        raw.get("single_venue_dominance_pct_per_sector"),
        global_value=dominance,
        lo=0.0,
        hi=1.0,
        label="thresholds.single_venue_dominance_pct_per_sector",
    )
    window_per_sector = _load_per_sector_int(
        raw.get("brier_window_days_per_sector"),
        global_value=window,
        lo=1,
        hi=3650,
        label="thresholds.brier_window_days_per_sector",
    )
    miscal_per_sector = _load_per_sector_float(
        raw.get("brier_miscalibration_per_sector"),
        global_value=miscal,
        lo=0.0,
        hi=1.0,
        label="thresholds.brier_miscalibration_per_sector",
    )
    bin_count = _coerce_int_in_range(
        raw.get("reliability_bin_count"),
        default=DEFAULT_BIN_COUNT,
        lo=2,
        hi=50,
        label="thresholds.reliability_bin_count",
    )
    min_per_bin = _coerce_int_in_range(
        raw.get("reliability_min_resolutions_per_bin"),
        default=DEFAULT_MIN_RESOLUTIONS_PER_BIN,
        lo=1,
        hi=1000,
        label="thresholds.reliability_min_resolutions_per_bin",
    )
    bin_count_per_sector = _load_per_sector_int(
        raw.get("reliability_bin_count_per_sector"),
        global_value=bin_count,
        lo=2,
        hi=50,
        label="thresholds.reliability_bin_count_per_sector",
    )
    min_per_bin_per_sector = _load_per_sector_int(
        raw.get("reliability_min_resolutions_per_bin_per_sector"),
        global_value=min_per_bin,
        lo=1,
        hi=1000,
        label="thresholds.reliability_min_resolutions_per_bin_per_sector",
    )
    return ReportThresholds(
        cross_venue_spread_bps=spread,
        single_venue_dominance_pct=dominance,
        brier_window_days=window,
        brier_miscalibration=miscal,
        reliability_bin_count=bin_count,
        reliability_min_resolutions_per_bin=min_per_bin,
        cross_venue_spread_bps_per_sector=spread_per_sector,
        single_venue_dominance_pct_per_sector=dominance_per_sector,
        brier_window_days_per_sector=window_per_sector,
        brier_miscalibration_per_sector=miscal_per_sector,
        reliability_bin_count_per_sector=bin_count_per_sector,
        reliability_min_resolutions_per_bin_per_sector=min_per_bin_per_sector,
    )


def _load_auto_prune(raw: Any) -> AutoPruneConfig:
    """Parse the optional ``auto_prune:`` block.

    Default is disabled. When enabled, ``older_than_days`` defaults
    to 365; ``keep_last`` defaults to None. Operators can set both
    (they stack — rows die under either condition). Bad values fall
    back to the default with a warning logged.
    """
    if raw is None:
        return AutoPruneConfig()
    if not isinstance(raw, dict):
        logger.warning(
            "report_generator: auto_prune is not a mapping (got %r); ignoring",
            type(raw).__name__,
        )
        return AutoPruneConfig()
    enabled = bool(raw.get("enabled", False))
    older_raw = raw.get("older_than_days", 365)
    older_than_days: int | None
    if older_raw is None:
        older_than_days = None
    else:
        older_than_days = _coerce_int_in_range(
            older_raw,
            default=365,
            lo=1,
            hi=36500,
            label="auto_prune.older_than_days",
        )
    keep_last_raw = raw.get("keep_last", None)
    keep_last: int | None
    if keep_last_raw is None:
        keep_last = None
    else:
        keep_last = _coerce_int_in_range(
            keep_last_raw,
            default=365,
            lo=0,
            hi=1_000_000,
            label="auto_prune.keep_last",
        )
    return AutoPruneConfig(
        enabled=enabled,
        older_than_days=older_than_days,
        keep_last=keep_last,
    )


def _coerce_int_in_range(raw: Any, *, default: int, lo: int, hi: int, label: str) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "report_generator: %s could not be coerced to int (got %r); falling back to default %d",
            label,
            raw,
            default,
        )
        return default
    if value < lo or value > hi:
        logger.warning(
            "report_generator: %s out of range [%d, %d] (got %d); falling back to default %d",
            label,
            lo,
            hi,
            value,
            default,
        )
        return default
    return value


def _coerce_float_in_range(raw: Any, *, default: float, lo: float, hi: float, label: str) -> float:
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "report_generator: %s could not be coerced to float (got %r); "
            "falling back to default %.4f",
            label,
            raw,
            default,
        )
        return default
    if value < lo or value > hi:
        logger.warning(
            "report_generator: %s out of range [%.4f, %.4f] (got %.4f); "
            "falling back to default %.4f",
            label,
            lo,
            hi,
            value,
            default,
        )
        return default
    return value


def _load_per_sector_int(
    raw: Any, *, global_value: int, lo: int, hi: int, label: str
) -> Mapping[str, int]:
    """Parse a per-sector override block of integer values.

    Skips non-string keys, non-coercible values, and out-of-range
    values with a warning logged. Per-sector entries that fall back
    are simply absent from the result dict; the caller's lookup
    helper will return the global value for those sectors.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "report_generator: %s is not a mapping (got %r); ignoring",
            label,
            type(raw).__name__,
        )
        return {}
    out: dict[str, int] = {}
    for sector, value in raw.items():
        if not isinstance(sector, str) or not sector:
            logger.warning(
                "report_generator: %s skipped entry with non-string sector %r",
                label,
                sector,
            )
            continue
        coerced = _coerce_int_in_range(
            value,
            default=global_value,
            lo=lo,
            hi=hi,
            label=f"{label}[{sector}]",
        )
        # If the coercion fell back to the global value for any reason,
        # we still want to record that (operators may want the global
        # value to apply). But we only want to *skip* the entry when
        # the value was invalid AND the global value would apply
        # naturally via the lookup helper. Distinguish by whether the
        # raw input was valid.
        if value is None:
            continue
        out[sector] = coerced
    return out


def _load_per_sector_float(
    raw: Any, *, global_value: float, lo: float, hi: float, label: str
) -> Mapping[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "report_generator: %s is not a mapping (got %r); ignoring",
            label,
            type(raw).__name__,
        )
        return {}
    out: dict[str, float] = {}
    for sector, value in raw.items():
        if not isinstance(sector, str) or not sector:
            logger.warning(
                "report_generator: %s skipped entry with non-string sector %r",
                label,
                sector,
            )
            continue
        coerced = _coerce_float_in_range(
            value,
            default=global_value,
            lo=lo,
            hi=hi,
            label=f"{label}[{sector}]",
        )
        if value is None:
            continue
        out[sector] = coerced
    return out


__all__ = [
    "ALL_SECTIONS",
    "DEFAULT_SINGLE_VENUE_DOMINANCE_PCT",
    "AutoPruneConfig",
    "ReportConfig",
    "ReportThresholds",
    "VerbosityLevel",
    "load_config",
]
