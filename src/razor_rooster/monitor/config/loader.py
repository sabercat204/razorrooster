"""Monitor configuration loader (T-MON-020)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("config") / "monitor.yaml"


@dataclass(frozen=True, slots=True)
class ShiftBandConfig:
    """Per-sector magnitude classification thresholds."""

    minor_threshold: float = 0.01
    material_threshold: float = 0.05
    major_threshold: float = 0.15


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    """Aggregate monitor config."""

    default_bands: ShiftBandConfig
    per_sector_bands: dict[str, ShiftBandConfig] = field(default_factory=dict)
    time_decay_alert_days: int = 7
    material_shift_alert_threshold: float = 0.10

    def bands_for(self, sector: str) -> ShiftBandConfig:
        return self.per_sector_bands.get(sector, self.default_bands)


def load_config(path: Path | None = None) -> MonitorConfig:
    target = path or DEFAULT_CONFIG_PATH
    payload: dict[str, Any] = {}
    if target.exists():
        with target.open("r", encoding="utf-8") as handle:
            decoded = yaml.safe_load(handle)
            if isinstance(decoded, dict):
                payload = decoded
    bands = payload.get("shift_bands") or {}
    default_payload = bands.get("default") or {}
    per_sector_raw = bands.get("per_sector") or {}
    return MonitorConfig(
        default_bands=ShiftBandConfig(
            minor_threshold=float(default_payload.get("minor_threshold", 0.01)),
            material_threshold=float(default_payload.get("material_threshold", 0.05)),
            major_threshold=float(default_payload.get("major_threshold", 0.15)),
        ),
        per_sector_bands={
            str(sector): ShiftBandConfig(
                minor_threshold=float(values.get("minor_threshold", 0.01)),
                material_threshold=float(values.get("material_threshold", 0.05)),
                major_threshold=float(values.get("major_threshold", 0.15)),
            )
            for sector, values in per_sector_raw.items()
            if isinstance(values, dict)
        },
        time_decay_alert_days=int(payload.get("time_decay_alert_days", 7)),
        material_shift_alert_threshold=float(payload.get("material_shift_alert_threshold", 0.10)),
    )


__all__ = ["MonitorConfig", "ShiftBandConfig", "load_config"]
