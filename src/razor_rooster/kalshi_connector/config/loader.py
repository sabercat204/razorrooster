"""Pydantic-validated config loaders for ``kalshi_connector`` (T-KSI-002).

Three YAML files drive the connector:

- :class:`KalshiConfig` from ``config/kalshi.yaml`` — base URL, tier,
  sync cadences, rate-limit envelope (tier-aware), freshness thresholds,
  watched-markets list, ToS URL.
- :class:`KalshiSectorKeywordsConfig` from
  ``config/kalshi_sector_keywords.yaml`` — the keyword catalogue the
  sector heuristic mapper consults. DEFER-KSI-001 acknowledges this list
  expands over time as triage feedback arrives. Includes the
  ``out_of_scope`` bucket per OQ-KSI-001.
- :class:`KalshiAllowedJurisdictionsConfig` from
  ``config/kalshi_allowed_jurisdictions.yaml`` — the eligibility gate's
  allow-list (inverts the Polymarket deny-list pattern). Lives in config
  so updates do not require a code change.

Loaders return frozen Pydantic models. Validation failures raise
:class:`KalshiConfigError` with the underlying issues attached so
operators see exactly which field tripped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class KalshiConfigError(ValueError):
    """Raised when a Kalshi config file fails to parse or validate."""


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


# --- shared literals ------------------------------------------------------


CadenceLiteral = Literal["every_cycle", "every_30min", "hourly", "daily", "weekly", "monthly"]

TierLiteral = Literal["Basic", "Advanced", "Premier", "Paragon", "Prime"]

# The eight Razor-Rooster downstream-eligible sectors plus the
# Kalshi-specific ``out_of_scope`` enum value (OQ-KSI-001 resolution).
KalshiRazorSector = Literal[
    "public_health",
    "geopolitical",
    "regulatory",
    "commodity",
    "climate",
    "infrastructure_energy",
    "macroeconomic",
    "cross_cutting",
    "out_of_scope",
]

_ALLOWED_KALSHI_SECTORS: Final[frozenset[str]] = frozenset(
    {
        "public_health",
        "geopolitical",
        "regulatory",
        "commodity",
        "climate",
        "infrastructure_energy",
        "macroeconomic",
        "cross_cutting",
        "out_of_scope",
    }
)


# --- kalshi.yaml ----------------------------------------------------------


class _CutoffSync(_Frozen):
    cadence: Literal["every_cycle"] = "every_cycle"


class _SeriesSync(_Frozen):
    cadence: Literal["daily"] = "daily"
    time_of_day: str | None = "08:30"

    @model_validator(mode="after")
    def _validate_time_of_day(self) -> _SeriesSync:
        if self.time_of_day is not None:
            _validate_hhmm(self.time_of_day, "series.time_of_day")
        return self


class _EventsSync(_Frozen):
    cadence: Literal["daily"] = "daily"
    time_of_day: str | None = "08:35"

    @model_validator(mode="after")
    def _validate_time_of_day(self) -> _EventsSync:
        if self.time_of_day is not None:
            _validate_hhmm(self.time_of_day, "events.time_of_day")
        return self


class _MarketsSync(_Frozen):
    cadence: Literal["daily"] = "daily"
    time_of_day: str | None = "08:40"

    @model_validator(mode="after")
    def _validate_time_of_day(self) -> _MarketsSync:
        if self.time_of_day is not None:
            _validate_hhmm(self.time_of_day, "markets.time_of_day")
        return self


class _PricesSync(_Frozen):
    default_cadence: Literal["every_30min", "hourly"] = "every_30min"
    minimum_interval_seconds: Annotated[int, Field(ge=60)] = 60
    watched_markets: list[str] = Field(default_factory=list)


class _SettlementsSync(_Frozen):
    cadence: Literal["daily"] = "daily"
    time_of_day: str | None = "08:45"

    @model_validator(mode="after")
    def _validate_time_of_day(self) -> _SettlementsSync:
        if self.time_of_day is not None:
            _validate_hhmm(self.time_of_day, "settlements.time_of_day")
        return self


class _TradesSync(_Frozen):
    cadence: Literal["daily"] = "daily"
    time_of_day: str | None = "09:00"

    @model_validator(mode="after")
    def _validate_time_of_day(self) -> _TradesSync:
        if self.time_of_day is not None:
            _validate_hhmm(self.time_of_day, "trades.time_of_day")
        return self


class _SyncConfig(_Frozen):
    cutoff: _CutoffSync = _CutoffSync()
    series: _SeriesSync = _SeriesSync()
    events: _EventsSync = _EventsSync()
    markets: _MarketsSync = _MarketsSync()
    prices: _PricesSync = _PricesSync()
    settlements: _SettlementsSync = _SettlementsSync()
    trades: _TradesSync = _TradesSync()


def _default_tier_budgets() -> dict[TierLiteral, int]:
    return {
        "Basic": 200,
        "Advanced": 300,
        "Premier": 1000,
        "Paragon": 2000,
        "Prime": 4000,
    }


class _RateLimitConfig(_Frozen):
    """Tier-aware token-bucket envelope.

    The limiter sizes its bucket capacity and refill rate to
    ``headroom_pct * tier_budget_tokens_per_sec[tier]``. Per-endpoint
    token costs (default 10 per Kalshi docs) are loaded by the rate
    limiter at startup; this config is the budget envelope.
    """

    tier_budget_tokens_per_sec: dict[TierLiteral, Annotated[int, Field(ge=1)]] = Field(
        default_factory=_default_tier_budgets
    )
    headroom_pct: Annotated[float, Field(gt=0.0, le=1.0)] = 0.5
    backoff_base_seconds: Annotated[float, Field(gt=0.0)] = 1.0
    backoff_max_seconds: Annotated[float, Field(gt=0.0)] = 60.0
    max_retries: Annotated[int, Field(ge=0, le=20)] = 5

    @model_validator(mode="after")
    def _validate(self) -> _RateLimitConfig:
        if self.backoff_base_seconds > self.backoff_max_seconds:
            raise ValueError("backoff_base_seconds must be <= backoff_max_seconds")
        # Confirm all five tier keys are present.
        required = {"Basic", "Advanced", "Premier", "Paragon", "Prime"}
        missing = required - set(self.tier_budget_tokens_per_sec.keys())
        if missing:
            raise ValueError(
                f"tier_budget_tokens_per_sec must declare all tiers; missing: {sorted(missing)}"
            )
        return self


class _FreshnessConfig(_Frozen):
    markets_threshold_seconds: Annotated[int, Field(ge=1)] = 172_800  # 48h
    prices_threshold_seconds: Annotated[int, Field(ge=1)] = 10_800  # 3h
    settlements_threshold_seconds: Annotated[int, Field(ge=1)] = 172_800


class _SectorMappingConfig(_Frozen):
    heuristic_version: Annotated[int, Field(ge=1)] = 1
    keywords_file: str = "config/kalshi_sector_keywords.yaml"


class KalshiConfig(_Frozen):
    """Top-level shape of ``config/kalshi.yaml``."""

    version: Annotated[int, Field(ge=1)]
    base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    tier: TierLiteral = "Basic"
    sync: _SyncConfig = _SyncConfig()
    rate_limit: _RateLimitConfig = _RateLimitConfig()
    freshness: _FreshnessConfig = _FreshnessConfig()
    sector_mapping: _SectorMappingConfig = _SectorMappingConfig()
    tos_url: str = "https://kalshi.com/docs/kalshi-terms-of-service"

    @model_validator(mode="after")
    def _validate_base_url(self) -> KalshiConfig:
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError(f"base_url must start with https:// or http://, got {self.base_url!r}")
        # Production-only in v1; the demo URL is a v2 trading concern.
        if "demo.kalshi" in self.base_url:
            raise ValueError(
                "base_url points at the demo environment; v1 reads only "
                "production public data. Demo is reserved for v2 trading work."
            )
        return self

    def headroom_tokens_per_sec(self) -> float:
        """Return the rate-limit headroom for the configured tier."""
        budget = self.rate_limit.tier_budget_tokens_per_sec[self.tier]
        return float(budget) * self.rate_limit.headroom_pct


def load_kalshi_config(path: Path | str) -> KalshiConfig:
    """Load and validate ``kalshi.yaml``."""
    return _load_yaml_model(path, KalshiConfig)


# --- kalshi_sector_keywords.yaml ------------------------------------------


class KalshiSectorKeywordsConfig(_Frozen):
    """Per-sector keyword catalogue used by the Kalshi heuristic mapper.

    Each sector maps to a list of case-insensitive keywords. Includes
    the ``out_of_scope`` bucket per OQ-KSI-001 — markets whose titles
    or categories match these keywords are persisted but excluded from
    downstream consumption.
    """

    version: Annotated[int, Field(ge=1)]
    sectors: dict[str, list[str]]

    @model_validator(mode="after")
    def _validate_sector_names(self) -> KalshiSectorKeywordsConfig:
        unknown = set(self.sectors.keys()) - _ALLOWED_KALSHI_SECTORS
        if unknown:
            raise ValueError(
                f"kalshi_sector_keywords.sectors contains unknown sector names: "
                f"{sorted(unknown)}; allowed: {sorted(_ALLOWED_KALSHI_SECTORS)}"
            )
        for sector_name, keyword_list in self.sectors.items():
            if not keyword_list:
                raise ValueError(
                    f"kalshi_sector_keywords.sectors[{sector_name!r}] must not be empty"
                )
            seen: set[str] = set()
            for keyword in keyword_list:
                lowered = keyword.lower()
                if lowered in seen:
                    raise ValueError(
                        f"kalshi_sector_keywords.sectors[{sector_name!r}] contains "
                        f"duplicate keyword (case-insensitive): {keyword!r}"
                    )
                seen.add(lowered)
        return self


def load_kalshi_sector_keywords(path: Path | str) -> KalshiSectorKeywordsConfig:
    """Load and validate ``kalshi_sector_keywords.yaml``."""
    return _load_yaml_model(path, KalshiSectorKeywordsConfig)


# --- kalshi_allowed_jurisdictions.yaml -----------------------------------


class KalshiAllowedJurisdictionsConfig(_Frozen):
    """Eligibility allow-list (REQ-KSI-ELIG-001).

    Inverts the Polymarket deny-list pattern: the gate refuses on
    jurisdictions NOT in this list. ISO 3166-1 alpha-2 country codes;
    matching is case-insensitive. The list lives in config so updates
    do not require a code change.
    """

    version: Annotated[int, Field(ge=1)]
    notes: str | None = None
    allowed: list[str]

    @model_validator(mode="after")
    def _validate_allowed(self) -> KalshiAllowedJurisdictionsConfig:
        if not self.allowed:
            raise ValueError("allowed list must contain at least one entry")
        seen: set[str] = set()
        for entry in self.allowed:
            stripped = entry.strip()
            if not stripped:
                raise ValueError("allowed entries must not be empty strings")
            normalized = stripped.upper()
            if normalized in seen:
                raise ValueError(
                    f"allowed list contains duplicate entry (case-insensitive): {entry!r}"
                )
            seen.add(normalized)
        return self


def load_kalshi_allowed_jurisdictions(
    path: Path | str,
) -> KalshiAllowedJurisdictionsConfig:
    """Load and validate ``kalshi_allowed_jurisdictions.yaml``."""
    return _load_yaml_model(path, KalshiAllowedJurisdictionsConfig)


# --- helpers --------------------------------------------------------------


_M = TypeVar("_M", bound=BaseModel)


def _load_yaml_model(path: Path | str, model: type[_M]) -> _M:
    p = Path(path) if not isinstance(path, Path) else path
    if not p.exists():
        raise KalshiConfigError(f"config file not found: {p}")
    try:
        with p.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise KalshiConfigError(f"invalid YAML in {p}: {exc}") from exc
    if raw is None:
        raise KalshiConfigError(f"config file is empty: {p}")
    if not isinstance(raw, dict):
        raise KalshiConfigError(
            f"config file must contain a top-level mapping, got {type(raw).__name__}: {p}"
        )
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise KalshiConfigError(f"validation failed for {p}: {exc}") from exc


__all__ = [
    "CadenceLiteral",
    "KalshiAllowedJurisdictionsConfig",
    "KalshiConfig",
    "KalshiConfigError",
    "KalshiRazorSector",
    "KalshiSectorKeywordsConfig",
    "TierLiteral",
    "load_kalshi_allowed_jurisdictions",
    "load_kalshi_config",
    "load_kalshi_sector_keywords",
]
