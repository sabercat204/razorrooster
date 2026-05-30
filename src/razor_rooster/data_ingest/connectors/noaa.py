"""NOAA Climate Data Online (CDO) connector (T-055).

Pulls climate observations from NOAA's CDO v2 API at
``https://www.ncei.noaa.gov/cdo-web/api/v2/data``.

Authentication is a single API token passed in the ``token`` header. The
free-tier rate budget is 1000 requests/day and 5 requests/second; the
connector applies a conservative client-side limiter at one request per
0.25 seconds (= 4 req/s, 96k req/day, well under the daily cap unless
the operator runs many distinct configurations).

NOAA's API uses ``offset`` + ``limit`` pagination with a maximum
``limit`` of 1000 per page. The connector iterates until ``offset +
count >= total``.

Configuration is in ``config/noaa_datasets.yaml``: each entry is a
(dataset_id, station_id, datatype_ids[], title, start_date) tuple.
Records map to the ``time_series`` canonical schema, with
``series_id = "<dataset>:<station>:<datatype>"``.

Backfill is supported. Resume tokens are
``<dataset>:<station>:<datatype>:<offset>``; the next run continues at
the recorded offset of the same query, then rolls forward to the next
query in the config when the page is exhausted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
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


_NOAA_BASE_URL: Final[str] = "https://www.ncei.noaa.gov/cdo-web/api/v2/data"
_NOAA_TIMEOUT_SECONDS: Final[float] = 30.0
_NOAA_DEFAULT_MIN_INTERVAL: Final[float] = 0.25
_NOAA_MAX_RETRIES: Final[int] = 5
_NOAA_PAGE_SIZE: Final[int] = 1000

_DEFAULT_CONFIG_PATH: Final[Path] = (
    Path(__file__).resolve().parents[4] / "config" / "noaa_datasets.yaml"
)


@dataclass(frozen=True, slots=True)
class NoaaQuery:
    """One NOAA CDO query the connector should run each cycle."""

    dataset: str
    station: str
    datatypes: tuple[str, ...]
    title: str
    start_date: date


def load_noaa_config(
    path: Path | str | None = None,
) -> tuple[tuple[NoaaQuery, ...], float]:
    """Load the NOAA config. Returns ``(queries, min_interval_seconds)``."""
    config_path = Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"NOAA config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict) or "queries" not in raw:
        raise ValueError(f"NOAA config must have a 'queries' key: {config_path}")

    rate_section = raw.get("rate_limit", {})
    min_interval = float(rate_section.get("min_interval_seconds", _NOAA_DEFAULT_MIN_INTERVAL))

    parsed: list[NoaaQuery] = []
    for entry in raw["queries"]:
        datatypes_raw = entry.get("datatypes", [])
        if not isinstance(datatypes_raw, list) or not datatypes_raw:
            raise ValueError(
                f"NOAA query {entry.get('dataset')!r}/{entry.get('station')!r}: "
                "'datatypes' must be a non-empty list"
            )
        parsed.append(
            NoaaQuery(
                dataset=str(entry["dataset"]),
                station=str(entry["station"]),
                datatypes=tuple(str(dt) for dt in datatypes_raw),
                title=str(entry["title"]),
                start_date=date.fromisoformat(str(entry["start_date"])),
            )
        )
    return tuple(parsed), min_interval


@register
class NoaaConnector(Connector):
    """Pulls NOAA CDO observations into the ``time_series`` schema."""

    source_id = "noaa"
    title = "NOAA Climate Data Online"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = True
    connector_version = "noaa@0.1.0"
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
        self._queries, self._min_interval = load_noaa_config(config_path)
        self._client = client or httpx.Client(timeout=_NOAA_TIMEOUT_SECONDS, follow_redirects=True)
        self._owns_client = client is None
        self._last_request_at: float = 0.0

    def __del__(self) -> None:
        try:
            if self._owns_client:
                self._client.close()
        except Exception:  # pragma: no cover
            pass

    @property
    def queries(self) -> tuple[NoaaQuery, ...]:
        return self._queries

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        token = self._require_token()
        since_date = since.date()
        for query in self._queries:
            for raw, _offset in self._iter_query_pages(query, token=token, start_date=since_date):
                yield raw

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        token = self._require_token()
        until_date = until.date()
        resume_dataset: str | None = None
        resume_station: str | None = None
        resume_datatype: str | None = None
        resume_offset: int | None = None
        if resume_token is not None:
            parts = resume_token.value.split(":")
            if len(parts) == 4:
                resume_dataset, resume_station, resume_datatype, resume_offset_str = parts
                resume_offset = int(resume_offset_str)

        skipping = resume_dataset is not None
        for query in self._queries:
            if skipping and (query.dataset != resume_dataset or query.station != resume_station):
                continue
            skipping = False
            datatypes_to_run = query.datatypes
            if resume_datatype is not None and resume_dataset == query.dataset:
                # Skip datatypes earlier in the list than the resume datatype.
                idx_list = list(query.datatypes)
                if resume_datatype in idx_list:
                    idx = idx_list.index(resume_datatype)
                    datatypes_to_run = tuple(idx_list[idx:])

            for datatype in datatypes_to_run:
                offset = (
                    (resume_offset + 1)
                    if (
                        resume_dataset == query.dataset
                        and resume_datatype == datatype
                        and resume_offset is not None
                    )
                    else 1
                )
                for raw, current_offset in self._iter_query_pages(
                    query,
                    token=token,
                    start_date=query.start_date,
                    end_date=until_date,
                    starting_offset=offset,
                    datatype_filter=(datatype,),
                ):
                    token_value = f"{query.dataset}:{query.station}:{datatype}:{current_offset}"
                    yield raw, ResumeToken(value=token_value)
                resume_datatype = None
                resume_offset = None
            # After exhausting one query, clear in-query resume state.
            resume_dataset = None
            resume_station = None

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.source_payload_json
        observation_ts = self._parse_iso_ts(payload.get("date"))
        value_raw = payload.get("value")
        numeric: float | None
        if value_raw is None:
            numeric = None
        else:
            try:
                numeric = float(value_raw)
            except (TypeError, ValueError):
                numeric = None
        series_id = f"{payload.get('dataset')}:{payload.get('station')}:{payload.get('datatype')}"
        return TimeSeriesRecord(
            source_id=self.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=payload,
            series_id=series_id,
            observation_ts=observation_ts,
            value=numeric,
            unit=payload.get("attributes"),
            frequency=payload.get("dataset_frequency"),
        )

    # --- internals ---------------------------------------------------------

    def _require_token(self) -> str:
        if not isinstance(self.credentials, ApiKeyBundle):
            raise CredentialMissingError("NOAA connector requires NOAA_CDO_TOKEN in .env")
        return self.credentials.api_key

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _iter_query_pages(
        self,
        query: NoaaQuery,
        *,
        token: str,
        start_date: date,
        end_date: date | None = None,
        starting_offset: int = 1,
        datatype_filter: tuple[str, ...] | None = None,
    ) -> Iterator[tuple[RawRecord, int]]:
        offset = starting_offset
        end = end_date or datetime.now(tz=UTC).date()
        datatypes = datatype_filter or query.datatypes
        while True:
            params: list[tuple[str, str | int | float | bool | None]] = [
                ("datasetid", query.dataset),
                ("stationid", query.station),
                ("startdate", start_date.isoformat()),
                ("enddate", end.isoformat()),
                ("limit", str(_NOAA_PAGE_SIZE)),
                ("offset", str(offset)),
                ("includemetadata", "true"),
            ]
            for datatype in datatypes:
                params.append(("datatypeid", datatype))

            body = self._request_with_retry(_NOAA_BASE_URL, params, token=token)
            results = body.get("results", []) if isinstance(body, dict) else []
            if not results:
                return
            metadata = (
                body.get("metadata", {}).get("resultset", {}) if isinstance(body, dict) else {}
            )
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                yield self._build_record(entry, query), offset
            count = int(metadata.get("count", len(results)))
            limit = int(metadata.get("limit", _NOAA_PAGE_SIZE))
            current_offset = int(metadata.get("offset", offset))
            if current_offset + len(results) > count:
                return
            offset = current_offset + limit
            if offset > count:
                return

    def _build_record(self, entry: dict[str, Any], query: NoaaQuery) -> RawRecord:
        observation_date = entry.get("date", "")
        record_id = (
            f"{query.dataset}:{query.station}:{entry.get('datatype', '')}:{observation_date}"
        )
        payload = {
            "dataset": query.dataset,
            "dataset_title": query.title,
            "station": query.station,
            "datatype": entry.get("datatype"),
            "date": observation_date,
            "value": entry.get("value"),
            "attributes": entry.get("attributes"),
        }
        publication_ts = self._parse_iso_ts(observation_date)
        return RawRecord(
            source_id=self.source_id,
            source_record_id=record_id,
            source_payload_json=payload,
            source_publication_ts=publication_ts,
        )

    def _parse_iso_ts(self, value: Any) -> datetime:
        if not value:
            return datetime.now(tz=UTC)
        try:
            # NOAA returns "2024-01-15T00:00:00".
            text = str(value)
            if text.endswith("Z"):
                text = text[:-1]
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except (ValueError, TypeError):
            return datetime.now(tz=UTC)

    def _request_with_retry(
        self,
        url: str,
        params: list[tuple[str, str | int | float | bool | None]],
        *,
        token: str,
    ) -> dict[str, Any]:
        headers = {"token": token}
        last_exc: Exception | None = None
        for attempt in range(_NOAA_MAX_RETRIES + 1):
            self._rate_limit()
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "NOAA request error attempt=%d error=%s",
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
                        "NOAA transient error attempt=%d status=%d",
                        attempt,
                        response.status_code,
                    )
                else:
                    response.raise_for_status()
                last_exc = httpx.HTTPStatusError(
                    f"NOAA returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if attempt < _NOAA_MAX_RETRIES:
                time.sleep(
                    exponential_backoff_with_jitter(attempt, base_seconds=1.0, max_seconds=60.0)
                )
        raise RateLimitedError(
            f"NOAA rate-limited or transient-failed past {_NOAA_MAX_RETRIES} retries: {last_exc}"
        )
