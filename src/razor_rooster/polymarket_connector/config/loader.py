"""Pydantic-validated config loaders for ``polymarket_connector`` (T-PMC-002).

Three YAML files drive the connector:

- :class:`PolymarketConfig` from ``config/polymarket.yaml`` — sync cadences,
  rate-limit envelope, freshness thresholds, watched-markets list.
- :class:`SectorKeywordsConfig` from ``config/sector_keywords.yaml`` — the
  keyword catalogue the sector heuristic mapper consults (DEFER-PMC-001
  acknowledges this list expands over time as triage feedback arrives).
- :class:`RestrictedJurisdictionsConfig` from
  ``config/restricted_jurisdictions.yaml`` — the geo-restriction gate's
  refusal list. Lives in config so updates do not require a code change.

Loaders return frozen Pydantic models. Validation failures raise
:class:`ConfigError` with the underlying issues attached so operators see
exactly which field tripped.
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


# --- helpers (declared early so model validators can reference them) -----


def _validate_hhmm(value: str, field_name: str) -> None:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"{field_name} must be HH:MM, got {value!r}")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"{field_name} must contain integers, got {value!r}") from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"{field_name} must be a valid wall-clock time, got {value!r}")


# --- polymarket.yaml ------------------------------------------------------


CadenceLiteral = Literal["daily", "weekly", "monthly", "hourly"]


# The six Razor-Rooster sectors plus the explicit "unmapped" sentinel used
# by the heuristic when nothing matches. Centralized here so other modules
# can import this single source of truth.
RazorSector = Literal[
    "public_health",
    "geopolitical",
    "regulatory",
    "commodity",
    "climate",
    "infrastructure_energy",
]


class _MarketsSync(_Frozen):
    cadence: CadenceLiteral = "daily"
    time_of_day: str | None = "08:30"

    @model_validator(mode="after")
    def _validate_time_of_day(self) -> _MarketsSync:
        if self.time_of_day is not None:
            _validate_hhmm(self.time_of_day, "markets.time_of_day")
        return self


class _PricesSync(_Frozen):
    default_cadence: CadenceLiteral = "hourly"
    minimum_interval_seconds: Annotated[int, Field(ge=60)] = 60
    watched_markets: list[str] = Field(default_factory=list)


class _ResolutionsSync(_Frozen):
    cadence: CadenceLiteral = "daily"
    time_of_day: str | None = "08:45"

    @model_validator(mode="after")
    def _validate_time_of_day(self) -> _ResolutionsSync:
        if self.time_of_day is not None:
            _validate_hhmm(self.time_of_day, "resolutions.time_of_day")
        return self


class _TradesSync(_Frozen):
    cadence: CadenceLiteral = "daily"
    time_of_day: str | None = "09:00"

    @model_validator(mode="after")
    def _validate_time_of_day(self) -> _TradesSync:
        if self.time_of_day is not None:
            _validate_hhmm(self.time_of_day, "trades.time_of_day")
        return self


class _SyncConfig(_Frozen):
    markets: _MarketsSync = _MarketsSync()
    prices: _PricesSync = _PricesSync()
    resolutions: _ResolutionsSync = _ResolutionsSync()
    trades: _TradesSync = _TradesSync()


class _RateLimitConfig(_Frozen):
    bucket_capacity: Annotated[int, Field(ge=1, le=10_000)] = 50
    refill_per_second: Annotated[float, Field(gt=0.0, le=10_000.0)] = 50.0
    backoff_base_seconds: Annotated[float, Field(gt=0.0)] = 1.0
    backoff_max_seconds: Annotated[float, Field(gt=0.0)] = 60.0
    max_retries: Annotated[int, Field(ge=0, le=20)] = 5

    @model_validator(mode="after")
    def _backoff_ordering(self) -> _RateLimitConfig:
        if self.backoff_base_seconds > self.backoff_max_seconds:
            raise ValueError("backoff_base_seconds must be <= backoff_max_seconds")
        return self


class _FreshnessConfig(_Frozen):
    markets_threshold_seconds: Annotated[int, Field(ge=1)] = 172_800  # 48h
    prices_threshold_seconds: Annotated[int, Field(ge=1)] = 21_600  # 6h
    resolutions_threshold_seconds: Annotated[int, Field(ge=1)] = 172_800


class _SectorMappingConfig(_Frozen):
    heuristic_version: Annotated[int, Field(ge=1)] = 1
    keywords_file: str = "config/sector_keywords.yaml"


class PolymarketConfig(_Frozen):
    """Top-level shape of ``config/polymarket.yaml``."""

    version: Annotated[int, Field(ge=1)]
    sync: _SyncConfig = _SyncConfig()
    rate_limit: _RateLimitConfig = _RateLimitConfig()
    freshness: _FreshnessConfig = _FreshnessConfig()
    sector_mapping: _SectorMappingConfig = _SectorMappingConfig()


def load_polymarket_config(path: Path | str) -> PolymarketConfig:
    """Load and validate ``polymarket.yaml``."""
    return _load_yaml_model(path, PolymarketConfig)


# --- sector_keywords.yaml -------------------------------------------------


class SectorKeywordsConfig(_Frozen):
    """Per-sector keyword catalogue used by the heuristic mapper.

    Each sector maps to a list of case-insensitive keywords. A market is
    classified by counting keyword hits in its question / description /
    tags; the highest-scoring sector wins, with ties surfacing the market
    for operator review (returns ``razor_sector = None``).
    """

    version: Annotated[int, Field(ge=1)]
    sectors: dict[str, list[str]]

    @model_validator(mode="after")
    def _validate_sector_names(self) -> SectorKeywordsConfig:
        allowed = {
            "public_health",
            "geopolitical",
            "regulatory",
            "commodity",
            "climate",
            "infrastructure_energy",
        }
        unknown = set(self.sectors.keys()) - allowed
        if unknown:
            raise ValueError(
                f"sector_keywords.sectors contains unknown sector names: {sorted(unknown)}; "
                f"allowed: {sorted(allowed)}"
            )
        for sector_name, keyword_list in self.sectors.items():
            if not keyword_list:
                raise ValueError(f"sector_keywords.sectors[{sector_name!r}] must not be empty")
            seen: set[str] = set()
            for keyword in keyword_list:
                lowered = keyword.lower()
                if lowered in seen:
                    raise ValueError(
                        f"sector_keywords.sectors[{sector_name!r}] contains duplicate "
                        f"keyword (case-insensitive): {keyword!r}"
                    )
                seen.add(lowered)
        return self


def load_sector_keywords(path: Path | str) -> SectorKeywordsConfig:
    """Load and validate ``sector_keywords.yaml``."""
    return _load_yaml_model(path, SectorKeywordsConfig)


# --- restricted_jurisdictions.yaml ----------------------------------------


class RestrictedJurisdictionsConfig(_Frozen):
    """Geo-gate refusal list (REQ-PMC-GEO-001).

    ISO 3166-1 alpha-2 / alpha-3 country codes plus optional sub-national
    codes for jurisdictions Polymarket explicitly geoblocks. The gate
    matches case-insensitively. The list lives in config so updates do not
    require a code change.
    """

    version: Annotated[int, Field(ge=1)]
    notes: str | None = None
    restricted: list[str]

    @model_validator(mode="after")
    def _validate_restricted(self) -> RestrictedJurisdictionsConfig:
        if not self.restricted:
            raise ValueError("restricted list must contain at least one entry")
        seen: set[str] = set()
        for entry in self.restricted:
            stripped = entry.strip()
            if not stripped:
                raise ValueError("restricted entries must not be empty strings")
            normalized = stripped.upper()
            if normalized in seen:
                raise ValueError(
                    f"restricted list contains duplicate entry (case-insensitive): {entry!r}"
                )
            seen.add(normalized)
        return self


def load_restricted_jurisdictions(path: Path | str) -> RestrictedJurisdictionsConfig:
    """Load and validate ``restricted_jurisdictions.yaml``."""
    return _load_yaml_model(path, RestrictedJurisdictionsConfig)


# --- helpers --------------------------------------------------------------


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
