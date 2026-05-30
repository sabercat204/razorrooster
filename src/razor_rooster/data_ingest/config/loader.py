"""Pydantic-validated config loaders for ``data_ingest`` (T-022).

The two YAML files driving the ingest cycle are defined here:

- :class:`IngestScheduleConfig` from ``config/ingest_schedule.yaml``.
- :class:`SourceCapsConfig` from ``config/source_caps.yaml``.

Loaders return frozen Pydantic models. Invalid configs raise
:class:`ConfigError` with the underlying validation issues attached so
operators see exactly which field tripped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ConfigError(ValueError):
    """Raised when a config file fails to parse or fails validation."""


_FROZEN_CONFIG: Final[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class _Frozen(BaseModel):
    model_config = _FROZEN_CONFIG


# --- ingest_schedule.yaml -------------------------------------------------


CadenceLiteral = Literal["daily", "weekly", "monthly", "annual"]
DayOfWeekLiteral = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]


class _ScheduleDefaults(_Frozen):
    """Top-level scheduler defaults."""

    max_workers: Annotated[int, Field(ge=1, le=64)] = 4
    batch_size: Annotated[int, Field(ge=1, le=1_000_000)] = 10_000


class SourceSchedule(_Frozen):
    """Per-source schedule entry (design §7.1)."""

    cadence: CadenceLiteral
    time_of_day: str | None = None  # "HH:MM"
    day_of_week: DayOfWeekLiteral | None = None
    freshness_threshold_seconds: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _validate_time_of_day(self) -> SourceSchedule:
        if self.time_of_day is not None:
            parts = self.time_of_day.split(":")
            if len(parts) != 2:
                raise ValueError(f"time_of_day must be HH:MM, got {self.time_of_day!r}")
            try:
                hour = int(parts[0])
                minute = int(parts[1])
            except ValueError as exc:
                raise ValueError(
                    f"time_of_day must contain integers, got {self.time_of_day!r}"
                ) from exc
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError(
                    f"time_of_day must be a valid wall-clock time, got {self.time_of_day!r}"
                )
        return self


class IngestScheduleConfig(_Frozen):
    """Top-level shape of ``config/ingest_schedule.yaml``."""

    version: Annotated[int, Field(ge=1)]
    defaults: _ScheduleDefaults = _ScheduleDefaults()
    sources: dict[str, SourceSchedule]

    @model_validator(mode="after")
    def _require_at_least_one_source(self) -> IngestScheduleConfig:
        if not self.sources:
            raise ValueError("at least one source schedule must be configured")
        return self


def load_ingest_schedule(path: Path | str) -> IngestScheduleConfig:
    """Load and validate ``ingest_schedule.yaml``."""
    return _load_yaml_model(path, IngestScheduleConfig)


class GlobalCaps(_Frozen):
    """Global corpus cap and threshold percentages (design §7.2)."""

    max_corpus_bytes: Annotated[int, Field(ge=1)]
    warn_at_pct: Annotated[float, Field(gt=0.0, le=100.0)] = 80.0
    pause_backfill_at_pct: Annotated[float, Field(gt=0.0, le=100.0)] = 95.0

    @model_validator(mode="after")
    def _warn_below_pause(self) -> GlobalCaps:
        if self.warn_at_pct >= self.pause_backfill_at_pct:
            raise ValueError("warn_at_pct must be strictly less than pause_backfill_at_pct")
        return self


class PerSourceCaps(_Frozen):
    """Per-source byte and depth caps (REQ-BACKFILL-003)."""

    max_backfill_years: Annotated[int, Field(ge=1, le=200)] | None = None
    max_bytes: Annotated[int, Field(ge=1)] | None = None


class SourceCapsConfig(_Frozen):
    """Top-level shape of ``config/source_caps.yaml``."""

    version: Annotated[int, Field(ge=1)]
    global_caps: GlobalCaps = Field(alias="global")
    per_source: dict[str, PerSourceCaps] = Field(default_factory=dict)


def load_source_caps(path: Path | str) -> SourceCapsConfig:
    """Load and validate ``source_caps.yaml``."""
    return _load_yaml_model(path, SourceCapsConfig)


# --- internals -------------------------------------------------------------


_M = TypeVar("_M", bound=BaseModel)


def _load_yaml_model(path: Path | str, model: type[_M]) -> _M:
    p = Path(path) if not isinstance(path, Path) else path
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        with p.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {p}: {exc}") from exc
    if raw is None:
        raise ConfigError(f"config file is empty: {p}")
    if not isinstance(raw, dict):
        raise ConfigError(
            f"config file must contain a top-level mapping, got {type(raw).__name__}: {p}"
        )
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"validation failed for {p}: {exc}") from exc
