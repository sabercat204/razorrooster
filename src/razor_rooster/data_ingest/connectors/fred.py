"""FRED connector (T-050).

Pulls observations from the Federal Reserve Economic Data (FRED) API at
``https://api.stlouisfed.org/fred/series/observations``.

The connector iterates over a configurable list of series IDs (default in
``config/fred_series.yaml``). For each series, it requests observations
either since ``last_successful_fetch`` (incremental) or from FRED's
earliest available record up to ``until`` (backfill). Each observation is
yielded as a :class:`RawRecord` with a synthetic ``source_record_id`` of
``<series_id>:<observation_date>``.

Authentication is a single API key passed as the ``api_key`` query
parameter. The key is loaded from ``FRED_API_KEY`` via the credential
loader; calling ``fetch_incremental`` without credentials raises
:class:`CredentialMissingError` so the scheduler classifies the run as
``skipped``.

FRED's API has a published rate limit of 120 requests per 60 seconds. The
connector applies a conservative client-side rate limiter at 1 request /
0.6 seconds (= 100 req/min, 17% headroom).

Rate-limit retries follow the framework's exponential-backoff-with-jitter
schedule (T-030) capped at 5 retries; persistent rate-limit failures
raise :class:`RateLimitedError`.

Backfill is supported. Resume tokens are ``<series_id>:<observation_date>``
of the most recently committed observation. On resume, the connector
restarts from the next series in the list (if mid-series-list) or from
the next observation in the current series.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
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
    load_credentials_for,
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


_FRED_BASE_URL: Final[str] = "https://api.stlouisfed.org/fred/series/observations"
_FRED_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0
_FRED_MIN_INTERVAL_SECONDS: Final[float] = 0.6  # ~100 requests / minute
_FRED_MAX_RETRIES: Final[int] = 5

_DEFAULT_CONFIG_PATH: Final[Path] = (
    Path(__file__).resolve().parents[4] / "config" / "fred_series.yaml"
)


@dataclass(frozen=True, slots=True)
class FredSeries:
    """One FRED series the connector should ingest."""

    id: str
    title: str
    frequency: str
    units: str | None = None


def load_fred_series_config(path: Path | str | None = None) -> tuple[FredSeries, ...]:
    """Load the FRED series list from YAML config."""
    config_path = Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"FRED series config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict) or "series" not in raw:
        raise ValueError(f"FRED series config must have a top-level 'series' key: {config_path}")
    return tuple(
        FredSeries(
            id=str(entry["id"]),
            title=str(entry["title"]),
            frequency=str(entry["frequency"]),
            units=entry.get("units"),
        )
        for entry in raw["series"]
    )


@register
class FredConnector(Connector):
    """Pulls FRED observations into the ``time_series`` canonical schema."""

    source_id = "fred"
    title = "FRED — Federal Reserve Economic Data"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = True
    connector_version = "fred@0.1.0"
    license_noncommercial_required = False

    def __init__(
        self,
        store: DuckDBStore,
        *,
        credentials: CredentialBundle | None = None,
        series_config_path: Path | str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(store, credentials=credentials)
        self._series = load_fred_series_config(series_config_path)
        self._client = client or httpx.Client(
            timeout=_FRED_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        self._last_request_at: float = 0.0
        self._owns_client = client is None

    def __del__(self) -> None:
        try:
            if self._owns_client:
                self._client.close()
        except Exception:  # pragma: no cover - cleanup, must not raise
            pass

    @property
    def series(self) -> tuple[FredSeries, ...]:
        return self._series

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        api_key = self._require_api_key()
        observation_start = since.date()
        for series in self._series:
            yield from self._fetch_series(
                series, api_key=api_key, observation_start=observation_start
            )

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        api_key = self._require_api_key()
        observation_end = until.date()

        # Parse resume token: format is "<series_id>:<YYYY-MM-DD>" of the most
        # recently committed observation. We resume from the next series after
        # that, *or* from the next observation within that series.
        resume_series_id: str | None = None
        resume_after_date: date | None = None
        if resume_token is not None:
            parts = resume_token.value.split(":", 1)
            if len(parts) == 2:
                resume_series_id, resume_after_str = parts
                resume_after_date = date.fromisoformat(resume_after_str)

        skipping_until_resume = resume_series_id is not None
        for series in self._series:
            if skipping_until_resume and series.id != resume_series_id:
                continue
            skipping_until_resume = False

            # When resuming inside a series, start from the day *after* the
            # last committed observation so we don't re-fetch a known row.
            start_date: date | None
            if resume_series_id == series.id and resume_after_date is not None:
                start_date = resume_after_date + timedelta(days=1)
            else:
                start_date = None
            for record in self._fetch_series(
                series,
                api_key=api_key,
                observation_start=start_date,
                observation_end=observation_end,
            ):
                token_value = record.source_record_id
                yield record, ResumeToken(value=token_value)
            # After completing one series, clear the in-series resume so the
            # next series starts from the beginning.
            resume_series_id = None
            resume_after_date = None

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.source_payload_json
        observation_date = payload["observation_date"]
        observation_ts = datetime.combine(
            date.fromisoformat(observation_date), dtime(0, 0), tzinfo=UTC
        )
        value = payload.get("value")
        # FRED returns "." for missing observations; normalize to None.
        numeric_value: float | None
        if value is None or value == "." or value == "":
            numeric_value = None
        else:
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                numeric_value = None
        return TimeSeriesRecord(
            source_id=self.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=payload,
            series_id=str(payload["series_id"]),
            observation_ts=observation_ts,
            value=numeric_value,
            unit=payload.get("units"),
            frequency=payload.get("frequency"),
        )

    # --- internals ---------------------------------------------------------

    def _require_api_key(self) -> str:
        if not isinstance(self.credentials, ApiKeyBundle):
            raise CredentialMissingError("FRED connector requires FRED_API_KEY in .env")
        return self.credentials.api_key

    def _rate_limit(self) -> None:
        """Sleep to keep request rate under FRED's 120/min cap."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _FRED_MIN_INTERVAL_SECONDS:
            time.sleep(_FRED_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _fetch_series(
        self,
        series: FredSeries,
        *,
        api_key: str,
        observation_start: date | None = None,
        observation_end: date | None = None,
    ) -> Iterator[RawRecord]:
        params: dict[str, str] = {
            "series_id": series.id,
            "api_key": api_key,
            "file_type": "json",
        }
        if observation_start is not None:
            params["observation_start"] = observation_start.isoformat()
        if observation_end is not None:
            params["observation_end"] = observation_end.isoformat()

        body = self._request_with_retry(_FRED_BASE_URL, params)
        observations = body.get("observations", [])
        for obs in observations:
            obs_date = obs.get("date")
            if not obs_date:
                continue
            payload = {
                "series_id": series.id,
                "observation_date": obs_date,
                "value": obs.get("value"),
                "realtime_start": obs.get("realtime_start"),
                "realtime_end": obs.get("realtime_end"),
                "units": series.units,
                "frequency": series.frequency,
                "series_title": series.title,
            }
            yield RawRecord(
                source_id=self.source_id,
                source_record_id=f"{series.id}:{obs_date}",
                source_payload_json=payload,
                source_publication_ts=datetime.combine(
                    date.fromisoformat(obs_date), dtime(0, 0), tzinfo=UTC
                ),
            )

    def _request_with_retry(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(_FRED_MAX_RETRIES + 1):
            self._rate_limit()
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "FRED request error attempt=%d series_id=%s error=%s",
                    attempt,
                    params.get("series_id"),
                    type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    parsed = response.json()
                    if not isinstance(parsed, dict):
                        raise ValueError(f"FRED returned non-dict body: {type(parsed).__name__}")
                    return parsed
                if response.status_code == 429 or response.status_code >= 500:
                    logger.warning(
                        "FRED transient error attempt=%d status=%d series_id=%s",
                        attempt,
                        response.status_code,
                        params.get("series_id"),
                    )
                else:
                    response.raise_for_status()
                last_exc = httpx.HTTPStatusError(
                    f"FRED returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if attempt < _FRED_MAX_RETRIES:
                time.sleep(
                    exponential_backoff_with_jitter(attempt, base_seconds=1.0, max_seconds=60.0)
                )
        raise RateLimitedError(
            f"FRED rate-limited or transient-failed past {_FRED_MAX_RETRIES} retries: {last_exc}"
        )


def construct(
    store: DuckDBStore,
    *,
    series_config_path: Path | str | None = None,
) -> FredConnector:
    """Convenience constructor: load credentials, build the connector."""
    credentials = load_credentials_for("fred")
    return FredConnector(store, credentials=credentials, series_config_path=series_config_path)
