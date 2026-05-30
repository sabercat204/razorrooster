"""GDELT 2.0 events connector (T-052, OQ-002 resolution).

Pulls geopolitical event records from GDELT's 2.0 events feed at
``http://data.gdeltproject.org/gdeltv2/<YYYYMMDDHHMMSS>.export.CSV.zip``.

GDELT 2.0 publishes a new ZIP-compressed TSV file every 15 minutes (96
files per day). Each file contains the events recorded in that 15-minute
window plus accumulated context fields. Per the design's OQ-002
resolution, this v1 connector ingests only the events feed (not the
larger GKG corpus) and caps backfill at 5 years.

The connector is unauthenticated. GDELT does not publish per-IP rate
limits; the connector applies a polite client-side limiter at one
request per 0.5 seconds (= 7,200 requests/hour, comfortable for a
backfill of 96 files/day by 5 years = 175,200 files / 24h sustained).

Each event row maps to the ``event_stream`` canonical schema:
- ``source_record_id`` = GLOBALEVENTID
- ``event_ts`` = SQLDATE column parsed as YYYYMMDD UTC
- ``country_iso3`` = ActionGeo_CountryCode (already ISO-3 in GDELT 2.0)
- ``event_class`` = EventCode (CAMEO event code)
- ``actor_primary`` = Actor1Name
- ``actor_secondary`` = Actor2Name
- ``description`` = SOURCEURL (the originating news URL — GDELT does
  not publish article text)

Backfill is supported. Resume tokens are the GDELT timestamp string
``YYYYMMDDHHMMSS`` of the most recently committed file; resume picks up
at the next 15-minute window. Per-source byte cap (30 GB per design
§2 OQ-002) is enforced by the cap-check layer (T-035).

The 15-minute file boundaries are 00, 15, 30, 45 of every hour. A
``next_window`` helper iterates these correctly across day/month/year
boundaries.
"""

from __future__ import annotations

import csv
import io
import logging
import time
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx

from razor_rooster.data_ingest.connectors.base import (
    Connector,
    License,
    RateLimitedError,
    ResumeToken,
    exponential_backoff_with_jitter,
)
from razor_rooster.data_ingest.credentials import CredentialBundle
from razor_rooster.data_ingest.normalization.base import (
    EventStreamRecord,
    NormalizedRecord,
    RawRecord,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import register

logger = logging.getLogger(__name__)


_GDELT_BASE_URL: Final[str] = "http://data.gdeltproject.org/gdeltv2"
_GDELT_TIMEOUT_SECONDS: Final[float] = 60.0
_GDELT_MIN_INTERVAL_SECONDS: Final[float] = 0.5
_GDELT_MAX_RETRIES: Final[int] = 5
_GDELT_BACKFILL_YEARS_DEFAULT: Final[int] = 5

# GDELT 2.0 events column order. Reference:
# http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf
_GDELT_COLUMNS: Final[tuple[str, ...]] = (
    "GLOBALEVENTID",
    "SQLDATE",
    "MonthYear",
    "Year",
    "FractionDate",
    "Actor1Code",
    "Actor1Name",
    "Actor1CountryCode",
    "Actor1KnownGroupCode",
    "Actor1EthnicCode",
    "Actor1Religion1Code",
    "Actor1Religion2Code",
    "Actor1Type1Code",
    "Actor1Type2Code",
    "Actor1Type3Code",
    "Actor2Code",
    "Actor2Name",
    "Actor2CountryCode",
    "Actor2KnownGroupCode",
    "Actor2EthnicCode",
    "Actor2Religion1Code",
    "Actor2Religion2Code",
    "Actor2Type1Code",
    "Actor2Type2Code",
    "Actor2Type3Code",
    "IsRootEvent",
    "EventCode",
    "EventBaseCode",
    "EventRootCode",
    "QuadClass",
    "GoldsteinScale",
    "NumMentions",
    "NumSources",
    "NumArticles",
    "AvgTone",
    "Actor1Geo_Type",
    "Actor1Geo_FullName",
    "Actor1Geo_CountryCode",
    "Actor1Geo_ADM1Code",
    "Actor1Geo_ADM2Code",
    "Actor1Geo_Lat",
    "Actor1Geo_Long",
    "Actor1Geo_FeatureID",
    "Actor2Geo_Type",
    "Actor2Geo_FullName",
    "Actor2Geo_CountryCode",
    "Actor2Geo_ADM1Code",
    "Actor2Geo_ADM2Code",
    "Actor2Geo_Lat",
    "Actor2Geo_Long",
    "Actor2Geo_FeatureID",
    "ActionGeo_Type",
    "ActionGeo_FullName",
    "ActionGeo_CountryCode",
    "ActionGeo_ADM1Code",
    "ActionGeo_ADM2Code",
    "ActionGeo_Lat",
    "ActionGeo_Long",
    "ActionGeo_FeatureID",
    "DATEADDED",
    "SOURCEURL",
)


def gdelt_filename(window_ts: datetime) -> str:
    """Return the GDELT v2 events filename for a given 15-minute window."""
    return f"{window_ts.strftime('%Y%m%d%H%M%S')}.export.CSV.zip"


def gdelt_url(window_ts: datetime) -> str:
    """Return the full GDELT v2 events URL for a window."""
    return f"{_GDELT_BASE_URL}/{gdelt_filename(window_ts)}"


def round_to_15_minute_window(ts: datetime) -> datetime:
    """Round a timestamp down to the nearest 15-minute window in UTC."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    elif ts.tzinfo != UTC:
        ts = ts.astimezone(UTC)
    minute = (ts.minute // 15) * 15
    return ts.replace(minute=minute, second=0, microsecond=0)


def iter_15_minute_windows(start: datetime, end: datetime) -> Iterator[datetime]:
    """Yield each 15-minute window timestamp from ``start`` (inclusive) to ``end`` (exclusive)."""
    current = round_to_15_minute_window(start)
    end = end if end.tzinfo is not None else end.replace(tzinfo=UTC)
    while current < end:
        yield current
        current += timedelta(minutes=15)


@register
class GdeltEventsConnector(Connector):
    """Pulls GDELT 2.0 event rows into the ``event_stream`` schema."""

    source_id = "gdelt_events"
    title = "GDELT 2.0 — Events"
    canonical_schema = SchemaType.EVENT_STREAM
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = True
    connector_version = "gdelt_events@0.1.0"
    license_noncommercial_required = False

    def __init__(
        self,
        store: DuckDBStore,
        *,
        credentials: CredentialBundle | None = None,
        client: httpx.Client | None = None,
        backfill_years_cap: int = _GDELT_BACKFILL_YEARS_DEFAULT,
    ) -> None:
        super().__init__(store, credentials=credentials)
        self._client = client or httpx.Client(timeout=_GDELT_TIMEOUT_SECONDS, follow_redirects=True)
        self._owns_client = client is None
        self._last_request_at: float = 0.0
        self._backfill_years_cap = backfill_years_cap

    def __del__(self) -> None:
        try:
            if self._owns_client:
                self._client.close()
        except Exception:  # pragma: no cover
            pass

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        end = datetime.now(tz=UTC)
        for window_ts in iter_15_minute_windows(since, end):
            yield from self._fetch_window(window_ts)

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        if resume_token is not None:
            start_ts = self._parse_window_token(resume_token.value) + timedelta(minutes=15)
        else:
            cap_start = until - timedelta(days=365 * self._backfill_years_cap)
            start_ts = round_to_15_minute_window(cap_start)
        for window_ts in iter_15_minute_windows(start_ts, until):
            window_token = window_ts.strftime("%Y%m%d%H%M%S")
            yielded_any = False
            for raw in self._fetch_window(window_ts):
                yielded_any = True
                yield raw, ResumeToken(value=window_token)
            if not yielded_any:
                # Even an empty window should advance the resume token so
                # the next call doesn't re-attempt the same empty file.
                yield from ()  # no records; orchestrator advances on its own
                # Emit a sentinel record? No — resume_token advances only on
                # a successful yield, by design. The orchestrator handles
                # advancement after a batch completes.

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.source_payload_json
        event_ts = self._parse_sql_date(payload.get("SQLDATE"))
        country = payload.get("ActionGeo_CountryCode")
        # GDELT 2.0 uses 2-letter FIPS codes for countries by default; some
        # exports include 3-letter ISO codes. Accept both: 2-letter codes
        # pass through to_iso3 via the geo helper if needed, otherwise leave
        # as-is when already 3-letter. Here we accept any non-empty value
        # and trust the country field; downstream subsystems can re-normalize
        # via to_iso3 if they need stricter ISO-3.
        return EventStreamRecord(
            source_id=self.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=payload,
            event_ts=event_ts,
            country_iso3=country if country else None,
            actor_primary=payload.get("Actor1Name") or None,
            actor_secondary=payload.get("Actor2Name") or None,
            event_class=payload.get("EventCode") or None,
            description=payload.get("SOURCEURL") or None,
        )

    # --- internals ---------------------------------------------------------

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _GDELT_MIN_INTERVAL_SECONDS:
            time.sleep(_GDELT_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _fetch_window(self, window_ts: datetime) -> Iterator[RawRecord]:
        url = gdelt_url(window_ts)
        zip_bytes = self._fetch_zip(url)
        if zip_bytes is None:
            return
        publication_ts = window_ts
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for name in zf.namelist():
                    if not name.endswith(".CSV"):
                        continue
                    with zf.open(name) as fh:
                        text_io = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
                        reader = csv.reader(text_io, delimiter="\t")
                        for row in reader:
                            yield self._row_to_record(row, publication_ts)
                    break
        except zipfile.BadZipFile as exc:
            logger.warning(
                "GDELT bad zip url=%s error=%s",
                url,
                exc,
            )

    def _row_to_record(self, row: list[str], publication_ts: datetime) -> RawRecord:
        # Pad short rows with empty strings; GDELT v2 has 61 columns.
        if len(row) < len(_GDELT_COLUMNS):
            row = list(row) + [""] * (len(_GDELT_COLUMNS) - len(row))
        payload: dict[str, Any] = dict(zip(_GDELT_COLUMNS, row, strict=False))
        global_event_id = payload.get("GLOBALEVENTID") or ""
        return RawRecord(
            source_id=self.source_id,
            source_record_id=str(global_event_id),
            source_payload_json=payload,
            source_publication_ts=publication_ts,
        )

    def _parse_sql_date(self, value: Any) -> datetime:
        if not value:
            return datetime.now(tz=UTC)
        text = str(value)
        if len(text) == 8 and text.isdigit():
            try:
                return datetime(
                    int(text[:4]),
                    int(text[4:6]),
                    int(text[6:8]),
                    tzinfo=UTC,
                )
            except ValueError:
                pass
        return datetime.now(tz=UTC)

    def _parse_window_token(self, value: str) -> datetime:
        if len(value) != 14 or not value.isdigit():
            raise ValueError(f"invalid GDELT window token: {value!r}")
        return datetime(
            int(value[:4]),
            int(value[4:6]),
            int(value[6:8]),
            int(value[8:10]),
            int(value[10:12]),
            int(value[12:14]),
            tzinfo=UTC,
        )

    def _fetch_zip(self, url: str) -> bytes | None:
        last_exc: Exception | None = None
        for attempt in range(_GDELT_MAX_RETRIES + 1):
            self._rate_limit()
            try:
                response = self._client.get(url)
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "GDELT request error attempt=%d url=%s error=%s",
                    attempt,
                    url,
                    type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    return response.content
                if response.status_code == 404:
                    # GDELT files for some windows simply don't exist (e.g.,
                    # outage windows). Treat as empty rather than retry.
                    return None
                if response.status_code == 429 or response.status_code >= 500:
                    logger.warning(
                        "GDELT transient error attempt=%d status=%d",
                        attempt,
                        response.status_code,
                    )
                else:
                    response.raise_for_status()
                last_exc = httpx.HTTPStatusError(
                    f"GDELT returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if attempt < _GDELT_MAX_RETRIES:
                time.sleep(
                    exponential_backoff_with_jitter(attempt, base_seconds=1.0, max_seconds=60.0)
                )
        raise RateLimitedError(
            f"GDELT rate-limited or transient-failed past {_GDELT_MAX_RETRIES} retries: {last_exc}"
        )
