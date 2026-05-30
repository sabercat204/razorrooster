"""Configuration loader for position_engine (T-PE-020).

Reads ``config/position_engine.yaml`` and exposes typed bounds and
defaults for validation in the bankroll-config CLI plus the
analyzer engines. All knobs have sensible fallback values matching
the design defaults so the system works without the YAML file
present (smoke tests, ad-hoc invocations).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config") / "position_engine.yaml"


@dataclass(frozen=True, slots=True)
class BankrollDefaults:
    """Seed values used when no bankroll_config row exists yet."""

    analytical_bankroll_usd: float = 1000.0
    max_single_position_pct: float = 0.05
    kelly_fraction_default: float = 0.5
    min_edge_threshold: float = 0.03


@dataclass(frozen=True, slots=True)
class BankrollValidationBounds:
    """Validation bounds for bankroll_config inputs (OQ-PE-001 resolution)."""

    kelly_fraction_default_min: float = 0.0
    kelly_fraction_default_max: float = 0.5
    max_single_position_pct_min: float = 0.0
    max_single_position_pct_max: float = 0.25
    min_edge_threshold_min: float = 0.0
    min_edge_threshold_max: float = 0.5


@dataclass(frozen=True, slots=True)
class LiquidityFeasibility:
    """Per-sector liquidity-feasibility threshold (REQ-PE-CMP-006)."""

    default_pct_of_24h_volume: float = 0.05
    per_sector: dict[str, float] | None = None

    def threshold_for(self, sector: str) -> float:
        if self.per_sector and sector in self.per_sector:
            return float(self.per_sector[sector])
        return self.default_pct_of_24h_volume


@dataclass(frozen=True, slots=True)
class PositionEngineConfig:
    """Aggregate config view used by the engines."""

    bankroll_defaults: BankrollDefaults
    bankroll_validation: BankrollValidationBounds
    liquidity_feasibility: LiquidityFeasibility
    long_resolution_days_threshold: int = 365
    sensitivity_perturbations: tuple[float, ...] = (0.10, 0.20)
    bankroll_survival_scenarios: tuple[int, ...] = (1, 3, 5)


class BankrollValidationError(ValueError):
    """Raised when a bankroll-config update violates the validation bounds."""


def load_config(path: Path | None = None) -> PositionEngineConfig:
    """Load position_engine config from disk, or fall back to defaults."""
    target = path or DEFAULT_CONFIG_PATH
    payload: dict[str, Any] = {}
    if target.exists():
        with target.open("r", encoding="utf-8") as handle:
            decoded = yaml.safe_load(handle)
            if isinstance(decoded, dict):
                payload = decoded

    bankroll_defaults = _bankroll_defaults_from(payload.get("bankroll_defaults") or {})
    bankroll_validation = _validation_from(payload.get("bankroll_validation") or {})
    liquidity = _liquidity_from(payload.get("liquidity_feasibility") or {})
    long_resolution = int(payload.get("long_resolution_days_threshold", 365))
    sensitivity_raw = payload.get("sensitivity_perturbations") or [0.10, 0.20]
    sensitivity = tuple(float(p) for p in sensitivity_raw)
    survival_raw = payload.get("bankroll_survival_scenarios") or [1, 3, 5]
    survival = tuple(int(s) for s in survival_raw)
    return PositionEngineConfig(
        bankroll_defaults=bankroll_defaults,
        bankroll_validation=bankroll_validation,
        liquidity_feasibility=liquidity,
        long_resolution_days_threshold=long_resolution,
        sensitivity_perturbations=sensitivity,
        bankroll_survival_scenarios=survival,
    )


def validate_bankroll_inputs(
    *,
    analytical_bankroll_usd: float,
    max_single_position_pct: float,
    kelly_fraction_default: float,
    min_edge_threshold: float,
    bounds: BankrollValidationBounds | None = None,
) -> None:
    """Raise :class:`BankrollValidationError` on any out-of-bounds input.

    Bankroll itself just has to be positive; the four risk knobs have
    explicit upper bounds from OQ-PE-001 resolution.
    """
    b = bounds or BankrollValidationBounds()
    if analytical_bankroll_usd <= 0.0:
        raise BankrollValidationError(
            f"analytical_bankroll_usd must be > 0; got {analytical_bankroll_usd!r}"
        )
    if not (b.kelly_fraction_default_min <= kelly_fraction_default <= b.kelly_fraction_default_max):
        raise BankrollValidationError(
            f"kelly_fraction_default must be in "
            f"[{b.kelly_fraction_default_min}, {b.kelly_fraction_default_max}]; "
            f"got {kelly_fraction_default!r}. Half-Kelly (0.5) is the conservative ceiling."
        )
    if not (
        b.max_single_position_pct_min <= max_single_position_pct <= b.max_single_position_pct_max
    ):
        raise BankrollValidationError(
            f"max_single_position_pct must be in "
            f"[{b.max_single_position_pct_min}, {b.max_single_position_pct_max}]; "
            f"got {max_single_position_pct!r}."
        )
    if not (b.min_edge_threshold_min <= min_edge_threshold <= b.min_edge_threshold_max):
        raise BankrollValidationError(
            f"min_edge_threshold must be in "
            f"[{b.min_edge_threshold_min}, {b.min_edge_threshold_max}]; "
            f"got {min_edge_threshold!r}."
        )


# -- internals --------------------------------------------------------------


def _bankroll_defaults_from(payload: dict[str, Any]) -> BankrollDefaults:
    return BankrollDefaults(
        analytical_bankroll_usd=float(payload.get("analytical_bankroll_usd", 1000.0)),
        max_single_position_pct=float(payload.get("max_single_position_pct", 0.05)),
        kelly_fraction_default=float(payload.get("kelly_fraction_default", 0.5)),
        min_edge_threshold=float(payload.get("min_edge_threshold", 0.03)),
    )


def _validation_from(payload: dict[str, Any]) -> BankrollValidationBounds:
    return BankrollValidationBounds(
        kelly_fraction_default_min=float(payload.get("kelly_fraction_default_min", 0.0)),
        kelly_fraction_default_max=float(payload.get("kelly_fraction_default_max", 0.5)),
        max_single_position_pct_min=float(payload.get("max_single_position_pct_min", 0.0)),
        max_single_position_pct_max=float(payload.get("max_single_position_pct_max", 0.25)),
        min_edge_threshold_min=float(payload.get("min_edge_threshold_min", 0.0)),
        min_edge_threshold_max=float(payload.get("min_edge_threshold_max", 0.5)),
    )


def _liquidity_from(payload: dict[str, Any]) -> LiquidityFeasibility:
    default = float(payload.get("default_pct_of_24h_volume", 0.05))
    per_sector_raw = payload.get("per_sector") or {}
    per_sector = {str(k): float(v) for k, v in per_sector_raw.items()}
    return LiquidityFeasibility(
        default_pct_of_24h_volume=default,
        per_sector=per_sector or None,
    )


__all__ = [
    "BankrollDefaults",
    "BankrollValidationBounds",
    "BankrollValidationError",
    "LiquidityFeasibility",
    "PositionEngineConfig",
    "load_config",
    "validate_bankroll_inputs",
]
