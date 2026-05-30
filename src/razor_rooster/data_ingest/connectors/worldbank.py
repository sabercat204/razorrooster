"""World Bank Indicators connector (T-051).

Pulls indicator values from the World Bank's public API at
``https://api.worldbank.org/v2/country/{country}/indicator/{indicator}``.

Configuration is in ``config/worldbank_indicators.yaml``: each entry
specifies an indicator code, a human-readable title, and a country scope
(``all`` for the world-wide aggregate, or a list of ISO 3166-1 alpha-3
codes).

The World Bank API has no published per-key rate limit but documents that
abusive use will be blocked. The connector applies a conservative
client-side rate limiter at 1 request / 0.4 seconds.

The API uses ``page=1`` based pagination; a response page reports
``total`` and ``pages``, and the connector iterates until ``page == pages``.

Resume tokens are ``<indicator>:<country>:<page>`` of the most recently
committed page; the next run resumes from page+1 of the same
(indicator, country) pair, or rolls forward to the next indicator/country
when the current page is the last.

This connector is unauthenticated; ``credentials`` is always ``None``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, Final

import httpx
import yaml

from razor_rooster.data_ingest.connectors.base import (
    Connector,
    License,
    RateLimitedError,
    ResumeToken,
    exponential_backoff_with_jitter,
)
from razor_rooster.data_ingest.credentials import CredentialBundle
from razor_rooster.data_ingest.normalization.base import (
    NormalizedRecord,
    RawRecord,
    TimeSeriesRecord,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import register

logger = logging.getLogger(__name__)


_WORLDBANK_BASE_URL: Final[str] = "https://api.worldbank.org/v2"
_WORLDBANK_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0
_WORLDBANK_MIN_INTERVAL_SECONDS: Final[float] = 0.4
_WORLDBANK_MAX_RETRIES: Final[int] = 5
_WORLDBANK_PAGE_SIZE: Final[int] = 1000

_DEFAULT_CONFIG_PATH: Final[Path] = (
    Path(__file__).resolve().parents[4] / "config" / "worldbank_indicators.yaml"
)


@dataclass(frozen=True, slots=True)
class WorldBankIndicator:
    """One World Bank indicator the connector should ingest."""

    id: str
    title: str
    countries: str | tuple[str, ...]  # 'all' or a tuple of ISO-3 codes


def load_worldbank_config(path: Path | str | None = None) -> tuple[WorldBankIndicator, ...]:
    """Load the World Bank indicator config from YAML."""
    config_path = Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"World Bank config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict) or "indicators" not in raw:
        raise ValueError(f"World Bank config must have a top-level 'indicators' key: {config_path}")
    parsed: list[WorldBankIndicator] = []
    for entry in raw["indicators"]:
        countries_raw = entry.get("countries", "all")
        countries: str | tuple[str, ...]
        if countries_raw == "all":
            countries = "all"
        elif isinstance(countries_raw, list):
            countries = tuple(str(c) for c in countries_raw)
        else:
            raise ValueError(
                f"World Bank indicator {entry.get('id')!r}: "
                "countries must be 'all' or a list of ISO-3 codes"
            )
        parsed.append(
            WorldBankIndicator(
                id=str(entry["id"]),
                title=str(entry["title"]),
                countries=countries,
            )
        )
    return tuple(parsed)


@register
class WorldBankConnector(Connector):
    """Pulls World Bank indicator values into the ``time_series`` schema."""

    source_id = "worldbank"
    title = "World Bank — Indicators"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "weekly"
    backfill_supported = True
    connector_version = "worldbank@0.1.0"
    license_noncommercial_required = False

    def __init__(
        self,
        store: DuckDBStore,
        *,
        credentials: CredentialBundle | None = None,
        config_path: Path | str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(store, credentials=credentials)
        self._indicators = load_worldbank_config(config_path)
        self._client = client or httpx.Client(
            timeout=_WORLDBANK_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        self._owns_client = client is None
        self._last_request_at: float = 0.0

    def __del__(self) -> None:
        try:
            if self._owns_client:
                self._client.close()
        except Exception:  # pragma: no cover - cleanup
            pass

    @property
    def indicators(self) -> tuple[WorldBankIndicator, ...]:
        return self._indicators

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        # World Bank doesn't expose a "since X" filter at the indicator
        # level, so the incremental path pulls each (indicator, country)
        # and the persistence layer dedupes via the staging-merge pattern.
        # That's REQ-PERSIST-003 working as intended; the cost is that
        # we re-fetch the full series each cycle. For weekly cadence with
        # only ~5 indicators, this is fine.
        del since
        for indicator in self._indicators:
            yield from self._fetch_indicator(indicator)

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        # Resume token format: "<indicator>:<country>:<page>" where the
        # numbers are the most recently committed page.
        del until
        resume_indicator: str | None = None
        resume_country: str | None = None
        resume_page: int | None = None
        if resume_token is not None:
            parts = resume_token.value.split(":")
            if len(parts) == 3:
                resume_indicator, resume_country, resume_page_str = parts
                resume_page = int(resume_page_str)

        skipping = resume_indicator is not None
        for indicator in self._indicators:
            if skipping and indicator.id != resume_indicator:
                continue
            skipping = False
            for record, page, country in self._fetch_indicator_pages(
                indicator,
                start_country=resume_country,
                start_page=resume_page,
            ):
                token_value = f"{indicator.id}:{country}:{page}"
                yield record, ResumeToken(value=token_value)
            # After finishing one indicator, clear in-indicator resume.
            resume_indicator = None
            resume_country = None
            resume_page = None

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.source_payload_json
        # World Bank "date" is the year, e.g. "2024".
        year = int(payload["date"])
        observation_ts = datetime.combine(date(year, 1, 1), dtime(0, 0), tzinfo=UTC)
        value = payload.get("value")
        numeric: float | None
        if value is None:
            numeric = None
        else:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = None
        return TimeSeriesRecord(
            source_id=self.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=payload,
            series_id=str(payload["indicator_id"]),
            observation_ts=observation_ts,
            value=numeric,
            unit=None,
            frequency="A",
        )

    # --- internals ---------------------------------------------------------

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _WORLDBANK_MIN_INTERVAL_SECONDS:
            time.sleep(_WORLDBANK_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _fetch_indicator(self, indicator: WorldBankIndicator) -> Iterator[RawRecord]:
        for record, _page, _country in self._fetch_indicator_pages(indicator):
            yield record

    def _fetch_indicator_pages(
        self,
        indicator: WorldBankIndicator,
        *,
        start_country: str | None = None,
        start_page: int | None = None,
    ) -> Iterator[tuple[RawRecord, int, str]]:
        """Yield ``(RawRecord, page_number, country_alias)`` triples."""
        countries = self._resolve_countries(indicator)
        skipping_country = start_country is not None
        for country in countries:
            if skipping_country and country != start_country:
                continue
            skipping_country = False
            page = (start_page + 1) if (country == start_country and start_page is not None) else 1
            yield from self._iter_pages(indicator, country, starting_page=page)
            start_country = None
            start_page = None

    def _resolve_countries(self, indicator: WorldBankIndicator) -> tuple[str, ...]:
        if indicator.countries == "all":
            return ("all",)
        if isinstance(indicator.countries, tuple):
            return indicator.countries
        return ("all",)  # defensive fallback

    def _iter_pages(
        self,
        indicator: WorldBankIndicator,
        country: str,
        *,
        starting_page: int = 1,
    ) -> Iterator[tuple[RawRecord, int, str]]:
        url = f"{_WORLDBANK_BASE_URL}/country/{country}/indicator/{indicator.id}"
        page = starting_page
        while True:
            params: dict[str, str] = {
                "format": "json",
                "page": str(page),
                "per_page": str(_WORLDBANK_PAGE_SIZE),
            }
            body = self._request_with_retry(url, params)
            # World Bank's API returns a two-element list: [meta, data].
            if not isinstance(body, list) or len(body) != 2:
                logger.warning(
                    "World Bank returned unexpected body shape for %s/%s page=%d",
                    country,
                    indicator.id,
                    page,
                )
                return
            meta, data = body
            if not isinstance(data, list):
                # API returns a meta-only response when there's nothing to send
                # (e.g., unknown country or empty result).
                return
            for entry in data:
                country_iso3 = (
                    entry.get("country", {}).get("id")
                    if isinstance(entry.get("country"), dict)
                    else None
                )
                yield self._build_record(indicator, entry, country_iso3 or country), page, country
            total_pages = int(meta.get("pages", 1)) if isinstance(meta, dict) else 1
            if page >= total_pages:
                return
            page += 1

    def _build_record(
        self,
        indicator: WorldBankIndicator,
        entry: dict[str, Any],
        country_alias: str,
    ) -> RawRecord:
        record_id = (
            f"{indicator.id}:{entry.get('countryiso3code') or country_alias}:{entry.get('date')}"
        )
        payload = {
            "indicator_id": indicator.id,
            "indicator_title": indicator.title,
            "country_iso3": entry.get("countryiso3code") or country_alias,
            "country_name": entry.get("country", {}).get("value")
            if isinstance(entry.get("country"), dict)
            else None,
            "date": entry.get("date"),
            "value": entry.get("value"),
            "unit": entry.get("unit"),
            "obs_status": entry.get("obs_status"),
            "decimal": entry.get("decimal"),
        }
        return RawRecord(
            source_id=self.source_id,
            source_record_id=record_id,
            source_payload_json=payload,
            source_publication_ts=datetime.now(tz=UTC),
        )

    def _request_with_retry(self, url: str, params: dict[str, str]) -> Any:
        last_exc: Exception | None = None
        for attempt in range(_WORLDBANK_MAX_RETRIES + 1):
            self._rate_limit()
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "World Bank request error attempt=%d url=%s error=%s",
                    attempt,
                    url,
                    type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code == 429 or response.status_code >= 500:
                    logger.warning(
                        "World Bank transient error attempt=%d status=%d",
                        attempt,
                        response.status_code,
                    )
                else:
                    response.raise_for_status()
                last_exc = httpx.HTTPStatusError(
                    f"World Bank returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if attempt < _WORLDBANK_MAX_RETRIES:
                time.sleep(
                    exponential_backoff_with_jitter(attempt, base_seconds=1.0, max_seconds=60.0)
                )
        raise RateLimitedError(
            f"World Bank rate-limited or transient-failed past "
            f"{_WORLDBANK_MAX_RETRIES} retries: {last_exc}"
        )


def construct(
    store: DuckDBStore,
    *,
    config_path: Path | str | None = None,
) -> WorldBankConnector:
    """Convenience constructor (no credentials needed)."""
    return WorldBankConnector(store, credentials=None, config_path=config_path)
