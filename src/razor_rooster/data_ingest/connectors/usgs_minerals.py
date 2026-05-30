"""USGS Mineral Commodity Summaries connector (T-056).

Pulls annual mineral commodity data from USGS's publicly-available CSV
downloads at ``pubs.usgs.gov/periodicals/mcs<year>/``. The CSVs are
typically published late January or early February of each year.

The connector is configured via ``config/usgs_minerals.yaml``: each
entry represents one year's edition and lists the URL, the column name
that identifies commodities, and the metric columns to extract. For
each (commodity, metric) pair, the connector emits one TimeSeries
record with ``series_id = "<commodity>:<metric>"`` and
``observation_ts = <year>-01-01 UTC``.

USGS does not require authentication and does not publish a per-IP
rate limit. The connector applies a polite client-side limiter at one
request per 1.0 seconds, which is more than sufficient for the small
number of annual downloads.

Backfill is supported but in practice trivial: each edition is one CSV
download. Resume tokens are the year identifier of the most recently
committed edition; resume picks up from the next edition in the
config list. Mid-edition resumption is not implemented because each
edition is a single bulk download — partial state is not meaningful.
"""

from __future__ import annotations

import csv
import io
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


_USGS_TIMEOUT_SECONDS: Final[float] = 60.0
_USGS_MIN_INTERVAL_SECONDS: Final[float] = 1.0
_USGS_MAX_RETRIES: Final[int] = 5

_DEFAULT_CONFIG_PATH: Final[Path] = (
    Path(__file__).resolve().parents[4] / "config" / "usgs_minerals.yaml"
)


@dataclass(frozen=True, slots=True)
class UsgsEdition:
    """One annual USGS Mineral Commodity Summaries edition."""

    year: int
    url: str
    title: str
    commodity_column: str
    metric_columns: tuple[str, ...]


def load_usgs_config(path: Path | str | None = None) -> tuple[UsgsEdition, ...]:
    """Load the USGS edition list from YAML."""
    config_path = Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"USGS config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict) or "editions" not in raw:
        raise ValueError(f"USGS config must have an 'editions' key: {config_path}")

    parsed: list[UsgsEdition] = []
    for entry in raw["editions"]:
        metrics = entry.get("metric_columns") or []
        if not isinstance(metrics, list) or not metrics:
            raise ValueError(
                f"USGS edition year={entry.get('year')!r}: "
                "'metric_columns' must be a non-empty list"
            )
        parsed.append(
            UsgsEdition(
                year=int(entry["year"]),
                url=str(entry["url"]),
                title=str(entry["title"]),
                commodity_column=str(entry["commodity_column"]),
                metric_columns=tuple(str(m) for m in metrics),
            )
        )
    parsed.sort(key=lambda e: e.year)
    return tuple(parsed)


@register
class UsgsMineralsConnector(Connector):
    """Pulls USGS mineral commodity summaries into the ``time_series`` schema."""

    source_id = "usgs_minerals"
    title = "USGS Mineral Commodity Summaries"
    canonical_schema = SchemaType.TIME_SERIES
    license = License.PUBLIC_DOMAIN
    cadence_default = "annual"
    backfill_supported = True
    connector_version = "usgs_minerals@0.1.0"
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
        self._editions = load_usgs_config(config_path)
        self._client = client or httpx.Client(timeout=_USGS_TIMEOUT_SECONDS, follow_redirects=True)
        self._owns_client = client is None
        self._last_request_at: float = 0.0

    def __del__(self) -> None:
        try:
            if self._owns_client:
                self._client.close()
        except Exception:  # pragma: no cover
            pass

    @property
    def editions(self) -> tuple[UsgsEdition, ...]:
        return self._editions

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        # Only fetch editions that haven't been ingested yet.
        cutoff_year = since.year
        for edition in self._editions:
            if edition.year < cutoff_year:
                continue
            yield from self._fetch_edition(edition)

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        until_year = until.year
        resume_year: int | None = None
        if resume_token is not None:
            try:
                resume_year = int(resume_token.value)
            except ValueError:
                resume_year = None

        for edition in self._editions:
            if edition.year > until_year:
                break
            if resume_year is not None and edition.year <= resume_year:
                continue
            for raw in self._fetch_edition(edition):
                yield raw, ResumeToken(value=str(edition.year))

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.source_payload_json
        year = int(payload["year"])
        observation_ts = datetime.combine(date(year, 1, 1), datetime.min.time(), tzinfo=UTC)
        value_raw = payload.get("value")
        numeric: float | None
        if value_raw is None:
            numeric = None
        else:
            try:
                numeric = float(value_raw)
            except (TypeError, ValueError):
                numeric = None
        series_id = f"{payload['commodity']}:{payload['metric']}"
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
            unit=payload.get("unit"),
            frequency="A",
        )

    # --- internals ---------------------------------------------------------

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _USGS_MIN_INTERVAL_SECONDS:
            time.sleep(_USGS_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _fetch_edition(self, edition: UsgsEdition) -> Iterator[RawRecord]:
        text = self._fetch_csv(edition.url)
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            logger.warning("USGS edition %d has no CSV header", edition.year)
            return
        if edition.commodity_column not in reader.fieldnames:
            logger.warning(
                "USGS edition %d missing commodity column %r; columns=%s",
                edition.year,
                edition.commodity_column,
                reader.fieldnames,
            )
            return
        publication_ts = datetime.combine(date(edition.year, 1, 1), datetime.min.time(), tzinfo=UTC)
        for row in reader:
            commodity = (row.get(edition.commodity_column) or "").strip()
            if not commodity:
                continue
            for metric in edition.metric_columns:
                if metric not in row:
                    continue
                value = (row.get(metric) or "").strip()
                payload = {
                    "year": edition.year,
                    "commodity": commodity,
                    "metric": metric,
                    "value": value if value else None,
                    "unit": None,
                    "edition_title": edition.title,
                    "edition_url": edition.url,
                }
                yield RawRecord(
                    source_id=self.source_id,
                    source_record_id=f"{edition.year}:{commodity}:{metric}",
                    source_payload_json=payload,
                    source_publication_ts=publication_ts,
                )

    def _fetch_csv(self, url: str) -> str:
        last_exc: Exception | None = None
        for attempt in range(_USGS_MAX_RETRIES + 1):
            self._rate_limit()
            try:
                response = self._client.get(url)
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "USGS request error attempt=%d url=%s error=%s",
                    attempt,
                    url,
                    type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    return response.text
                if response.status_code == 429 or response.status_code >= 500:
                    logger.warning(
                        "USGS transient error attempt=%d status=%d",
                        attempt,
                        response.status_code,
                    )
                else:
                    response.raise_for_status()
                last_exc = httpx.HTTPStatusError(
                    f"USGS returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if attempt < _USGS_MAX_RETRIES:
                time.sleep(
                    exponential_backoff_with_jitter(attempt, base_seconds=1.0, max_seconds=60.0)
                )
        raise RateLimitedError(
            f"USGS rate-limited or transient-failed past {_USGS_MAX_RETRIES} retries: {last_exc}"
        )


_ = Any  # keep ``Any`` reachable for forward typing in case future fields are added
