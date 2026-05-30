"""EIA (U.S. Energy Information Administration) connector (T-061).

Pulls observations from EIA's v2 API at ``https://api.eia.gov/v2/``.

Authentication is via single API key passed in the ``api_key`` query
parameter. The free-tier rate limit is documented as 5,000 requests/
hour. The connector applies a conservative client-side limiter at one
request per 0.5 seconds (= 7,200 req/hour ceiling).

EIA's v2 API uses ``offset`` + ``length`` pagination with a maximum
``length`` of 5000 per page. Each route requires:

- A path under ``/data/`` (e.g., ``petroleum/pri/spt/data``).
- A facets object filtering by series, region, etc.
- A frequency parameter (e.g., ``daily``).
- Time bounds via ``start`` and ``end`` ISO date strings.

Configuration is in ``config/eia_series.yaml``: each entry is a
(series_id, route, facets, title, frequency, units) tuple.
Records map to the ``time_series`` canonical schema.

Backfill resume tokens are ``<series_id>:<offset>``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import yaml

from razor_rooster.data_ingest.connectors.base import (
    Connector,
    CredentialMissingError,
    License,
    RateLimitedError,
    ResumeToken,
    exponential_backoff_with_jitter,
)
from razor_rooster.data_ingest.credentials import (
    ApiKeyBundle,
    CredentialBundle,
)
from razor_rooster.data_ingest.normalization.base import (
    NormalizedRecord,
    RawRecord,
    TimeSeriesRecord,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import register

logger = logging.getLogger(__name__)


_EIA_BASE_URL: Final[str] = "https://api.eia.gov/v2"
_EIA_TIMEOUT_SECONDS: Final[float] = 30.0
_EIA_MIN_INTERVAL_SECONDS: Final[float] = 0.5
_EIA_MAX_RETRIES: Final[int] = 5
_EIA_PAGE_SIZE: Final[int] = 5000

_DEFAULT_CONFIG_PATH: Final[Path] = (
    Path(__file__).resolve().parents[4] / "config" / "eia_series.yaml"
)


@dataclass(frozen=True, slots=True)
class EiaSeries:
    id: str
    route: str
    facets: dict[str, tuple[str, ...]]
    title: str
    frequency: str
    units: str | None = None


def load_eia_config(path: Path | str | None = None) -> tuple[EiaSeries, ...]:
    """Load the EIA series config from YAML."""
    config_path = Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"EIA config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict) or "series" not in raw:
        raise ValueError(f"EIA config must have a 'series' key: {config_path}")

    parsed: list[EiaSeries] = []
    for entry in raw["series"]:
        facets_raw = entry.get("facets", {})
        if not isinstance(facets_raw, dict):
            raise ValueError(f"EIA series {entry.get('id')!r}: facets must be a mapping")
        facets: dict[str, tuple[str, ...]] = {}
        for k, v in facets_raw.items():
            if not isinstance(v, list):
                raise ValueError(f"EIA series {entry.get('id')!r}: facet {k!r} must be a list")
            facets[str(k)] = tuple(str(x) for x in v)
        parsed.append(
            EiaSeries(
                id=str(entry["id"]),
                route=str(entry["route"]),
                facets=facets,
                title=str(entry["title"]),
                frequency=str(entry["frequency"]),
                units=entry.get("units"),
            )
        )
    return tuple(parsed)


@register
class EiaConnector(Connector):
    """Pulls EIA v2 API observations into the ``time_series`` schema."""

    source_id = "eia"
    title = "U.S. Energy Information Administration"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = True
    connector_version = "eia@0.1.0"
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
        self._series = load_eia_config(config_path)
        self._client = client or httpx.Client(timeout=_EIA_TIMEOUT_SECONDS, follow_redirects=True)
        self._owns_client = client is None
        self._last_request_at: float = 0.0

    def __del__(self) -> None:
        try:
            if self._owns_client:
                self._client.close()
        except Exception:  # pragma: no cover
            pass

    @property
    def series(self) -> tuple[EiaSeries, ...]:
        return self._series

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        api_key = self._require_api_key()
        for series in self._series:
            for record, _offset in self._iter_series_pages(
                series, api_key=api_key, start_date=since.date()
            ):
                yield record

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        api_key = self._require_api_key()
        until_date = until.date()
        resume_series_id: str | None = None
        resume_offset: int | None = None
        if resume_token is not None:
            parts = resume_token.value.split(":")
            if len(parts) == 2:
                resume_series_id = parts[0]
                resume_offset = int(parts[1])

        skipping = resume_series_id is not None
        for series in self._series:
            if skipping and series.id != resume_series_id:
                continue
            skipping = False
            offset = (
                (resume_offset + _EIA_PAGE_SIZE)
                if (resume_series_id == series.id and resume_offset is not None)
                else 0
            )
            for record, current_offset in self._iter_series_pages(
                series,
                api_key=api_key,
                start_date=date(1970, 1, 1),
                end_date=until_date,
                starting_offset=offset,
            ):
                yield record, ResumeToken(value=f"{series.id}:{current_offset}")
            resume_offset = None

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.source_payload_json
        period = payload.get("period")
        observation_ts = self._parse_period(period, payload.get("frequency"))
        value_raw = payload.get("value")
        numeric: float | None
        if value_raw is None:
            numeric = None
        else:
            try:
                numeric = float(value_raw)
            except (TypeError, ValueError):
                numeric = None
        return TimeSeriesRecord(
            source_id=self.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=payload,
            series_id=str(payload.get("series_id") or ""),
            observation_ts=observation_ts,
            value=numeric,
            unit=payload.get("units"),
            frequency=payload.get("frequency"),
        )

    # --- internals ---------------------------------------------------------

    def _require_api_key(self) -> str:
        if not isinstance(self.credentials, ApiKeyBundle):
            raise CredentialMissingError("EIA connector requires EIA_API_KEY in .env")
        return self.credentials.api_key

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _EIA_MIN_INTERVAL_SECONDS:
            time.sleep(_EIA_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _iter_series_pages(
        self,
        series: EiaSeries,
        *,
        api_key: str,
        start_date: date,
        end_date: date | None = None,
        starting_offset: int = 0,
    ) -> Iterator[tuple[RawRecord, int]]:
        url = f"{_EIA_BASE_URL}/{series.route.strip('/')}/data/"
        end = end_date or datetime.now(tz=UTC).date()
        offset = starting_offset
        frequency_param = self._frequency_param(series.frequency)
        while True:
            params: list[tuple[str, str | int | float | bool | None]] = [
                ("api_key", api_key),
                ("frequency", frequency_param),
                ("start", start_date.isoformat()),
                ("end", end.isoformat()),
                ("offset", str(offset)),
                ("length", str(_EIA_PAGE_SIZE)),
            ]
            for facet_name, facet_values in series.facets.items():
                for value in facet_values:
                    params.append((f"facets[{facet_name}][]", value))
            params.append(("data[]", "value"))

            body = self._request_with_retry(url, params)
            response_data = body.get("response", {}) if isinstance(body, dict) else {}
            data_rows = response_data.get("data", [])
            if not isinstance(data_rows, list):
                return
            for entry in data_rows:
                if not isinstance(entry, dict):
                    continue
                yield self._build_record(series, entry), offset
            total = int(response_data.get("total", 0)) if isinstance(response_data, dict) else 0
            offset += len(data_rows)
            if not data_rows or offset >= total:
                return

    def _build_record(self, series: EiaSeries, entry: dict[str, Any]) -> RawRecord:
        period = entry.get("period") or ""
        record_id = f"{series.id}:{period}"
        payload = {
            "series_id": series.id,
            "series_title": series.title,
            "route": series.route,
            "period": period,
            "value": entry.get("value"),
            "units": series.units or entry.get("units"),
            "frequency": series.frequency,
        }
        publication_ts = self._parse_period(period, series.frequency)
        return RawRecord(
            source_id=self.source_id,
            source_record_id=record_id,
            source_payload_json=payload,
            source_publication_ts=publication_ts,
        )

    def _parse_period(self, value: Any, frequency: Any) -> datetime:
        """Parse an EIA period string into a UTC datetime.

        EIA periods can be daily ('YYYY-MM-DD'), monthly ('YYYY-MM'),
        quarterly ('YYYY-Q1'), or yearly ('YYYY'). We normalize each to
        the start of the period.
        """
        if not value:
            return datetime.now(tz=UTC)
        text = str(value)
        try:
            # Quarterly check first because YYYY-Q2 is len=7 but the
            # YYYY-MM branch would fail to parse it cleanly.
            if "Q" in text:
                year, quarter = text.split("-Q")
                month = (int(quarter) - 1) * 3 + 1
                return datetime(int(year), month, 1, tzinfo=UTC)
            if len(text) == 10:  # YYYY-MM-DD
                return datetime.combine(date.fromisoformat(text), datetime.min.time(), tzinfo=UTC)
            if len(text) == 7:  # YYYY-MM
                year, month_str = text.split("-")
                return datetime(int(year), int(month_str), 1, tzinfo=UTC)
            if len(text) == 4 and text.isdigit():
                return datetime(int(text), 1, 1, tzinfo=UTC)
        except (ValueError, IndexError):
            pass
        del frequency  # unused — period text already encodes granularity
        return datetime.now(tz=UTC)

    def _frequency_param(self, frequency: str) -> str:
        """Map EIA's short frequency codes to v2 API parameter values."""
        return {
            "D": "daily",
            "W": "weekly",
            "M": "monthly",
            "Q": "quarterly",
            "A": "annual",
        }.get(frequency.upper(), "daily")

    def _request_with_retry(
        self, url: str, params: list[tuple[str, str | int | float | bool | None]]
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(_EIA_MAX_RETRIES + 1):
            self._rate_limit()
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "EIA request error attempt=%d error=%s",
                    attempt,
                    type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    parsed = response.json()
                    if not isinstance(parsed, dict):
                        return {}
                    return parsed
                if response.status_code == 429 or response.status_code >= 500:
                    logger.warning(
                        "EIA transient error attempt=%d status=%d",
                        attempt,
                        response.status_code,
                    )
                else:
                    response.raise_for_status()
                last_exc = httpx.HTTPStatusError(
                    f"EIA returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if attempt < _EIA_MAX_RETRIES:
                time.sleep(
                    exponential_backoff_with_jitter(attempt, base_seconds=1.0, max_seconds=60.0)
                )
        raise RateLimitedError(
            f"EIA rate-limited or transient-failed past {_EIA_MAX_RETRIES} retries: {last_exc}"
        )


_ = timedelta  # keep timedelta importable for future date-windowing logic
