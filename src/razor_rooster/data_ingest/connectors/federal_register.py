"""Federal Register connector (T-053).

Pulls regulatory filings from the U.S. Federal Register's public API at
``https://www.federalregister.gov/api/v1/documents.json``.

The Federal Register is unauthenticated — anyone can hit the API with no
key. The published rate limit is 1000 requests per hour per IP. The
connector applies a conservative client-side rate limiter at one request
per 0.5 seconds (~7,200 req/hour ceiling, well above any realistic
ingest cadence).

Pagination is via the ``page`` parameter. Each page returns up to 1,000
documents with metadata fields the connector maps onto the
``document_docket`` canonical schema. ``full_text_uri`` points at the
source-hosted HTML/PDF; we do not download the full text in v1
(per design DEFER-004).

Backfill resume tokens are ``YYYY-MM-DD:page`` for the most recently
committed page; resume picks up at page+1 of the same publication date,
or rolls forward to the next date when the current page was the last.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
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
    DocumentDocketRecord,
    NormalizedRecord,
    RawRecord,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import register

logger = logging.getLogger(__name__)


_FR_BASE_URL: Final[str] = "https://www.federalregister.gov/api/v1/documents.json"
_FR_TIMEOUT_SECONDS: Final[float] = 30.0
_FR_MIN_INTERVAL_SECONDS: Final[float] = 0.5
_FR_MAX_RETRIES: Final[int] = 5
_FR_PAGE_SIZE: Final[int] = 1000
_FR_FIELDS: Final[tuple[str, ...]] = (
    "document_number",
    "title",
    "type",
    "abstract",
    "agencies",
    "publication_date",
    "effective_on",
    "comments_close_on",
    "html_url",
    "docket_id",
    "docket_ids",
)


@register
class FederalRegisterConnector(Connector):
    """Pulls Federal Register documents into the ``document_docket`` schema."""

    source_id = "federal_register"
    title = "U.S. Federal Register"
    canonical_schema = SchemaType.DOCUMENT_DOCKET
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = True
    connector_version = "federal_register@0.1.0"
    license_noncommercial_required = False

    def __init__(
        self,
        store: DuckDBStore,
        *,
        credentials: CredentialBundle | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(store, credentials=credentials)
        self._client = client or httpx.Client(timeout=_FR_TIMEOUT_SECONDS, follow_redirects=True)
        self._owns_client = client is None
        self._last_request_at: float = 0.0

    def __del__(self) -> None:
        try:
            if self._owns_client:
                self._client.close()
        except Exception:  # pragma: no cover
            pass

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        # Federal Register API supports `conditions[publication_date][gte]=YYYY-MM-DD`.
        since_date = since.date()
        for record, _date_str, _page in self._iter_pages(since_date=since_date):
            yield record

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        # We treat backfill as "from the dawn of the API forward to until";
        # the API's earliest publication date is 1994-01-01.
        until_date = until.date()
        resume_date: date | None = None
        resume_page: int | None = None
        if resume_token is not None:
            parts = resume_token.value.split(":")
            if len(parts) == 2:
                resume_date = date.fromisoformat(parts[0])
                resume_page = int(parts[1])

        starting_page = (resume_page + 1) if resume_page is not None else 1

        for record, date_str, page in self._iter_pages(
            since_date=resume_date or date(1994, 1, 1),
            until_date=until_date,
            starting_page=starting_page,
        ):
            yield record, ResumeToken(value=f"{date_str}:{page}")

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.source_payload_json
        agencies_list = payload.get("agencies") or []
        agency_name: str | None = None
        if agencies_list and isinstance(agencies_list, list):
            first = agencies_list[0]
            if isinstance(first, dict):
                agency_name = first.get("name") or first.get("raw_name")
        # docket_id is sometimes top-level, sometimes inside docket_ids list.
        docket_id = payload.get("docket_id")
        if not docket_id:
            ids = payload.get("docket_ids") or []
            if isinstance(ids, list) and ids:
                docket_id = str(ids[0])
        published_date = self._parse_date(payload.get("publication_date"))
        effective_date = self._parse_date(payload.get("effective_on"))
        comment_close_date = self._parse_date(payload.get("comments_close_on"))

        return DocumentDocketRecord(
            source_id=self.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=payload,
            title=str(payload.get("title") or ""),
            document_type=payload.get("type"),
            docket_id=str(docket_id) if docket_id else None,
            agency=agency_name,
            published_date=published_date,
            effective_date=effective_date,
            comment_close_date=comment_close_date,
            full_text_uri=payload.get("html_url"),
            full_text_local_path=None,
        )

    # --- internals ---------------------------------------------------------

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _FR_MIN_INTERVAL_SECONDS:
            time.sleep(_FR_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _iter_pages(
        self,
        *,
        since_date: date,
        until_date: date | None = None,
        starting_page: int = 1,
    ) -> Iterator[tuple[RawRecord, str, int]]:
        page = starting_page
        while True:
            params_list: list[tuple[str, str | int | float | bool | None]] = [
                ("per_page", str(_FR_PAGE_SIZE)),
                ("page", str(page)),
                ("order", "oldest"),
                ("conditions[publication_date][gte]", since_date.isoformat()),
            ]
            if until_date is not None:
                params_list.append(("conditions[publication_date][lte]", until_date.isoformat()))
            for field in _FR_FIELDS:
                params_list.append(("fields[]", field))

            body = self._request_with_retry(_FR_BASE_URL, params_list)
            results = body.get("results", [])
            if not isinstance(results, list):
                logger.warning(
                    "Federal Register returned unexpected results shape on page=%d",
                    page,
                )
                return
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                record = self._build_record(entry)
                yield record, record.source_payload_json.get("publication_date", ""), page
            total_pages = int(body.get("total_pages", 1))
            if page >= total_pages:
                return
            page += 1

    def _build_record(self, entry: dict[str, Any]) -> RawRecord:
        document_number = entry.get("document_number") or ""
        publication_date = entry.get("publication_date") or ""
        publication_dt: datetime
        try:
            publication_dt = datetime.combine(
                date.fromisoformat(publication_date),
                datetime.min.time(),
                tzinfo=UTC,
            )
        except (ValueError, TypeError):
            publication_dt = datetime.now(tz=UTC)
        return RawRecord(
            source_id=self.source_id,
            source_record_id=str(document_number),
            source_payload_json=entry,
            source_publication_ts=publication_dt,
        )

    def _parse_date(self, value: Any) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None

    def _request_with_retry(
        self, url: str, params: list[tuple[str, str | int | float | bool | None]]
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(_FR_MAX_RETRIES + 1):
            self._rate_limit()
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "Federal Register request error attempt=%d error=%s",
                    attempt,
                    type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    parsed = response.json()
                    if not isinstance(parsed, dict):
                        raise ValueError(
                            f"Federal Register returned non-dict body: {type(parsed).__name__}"
                        )
                    return parsed
                if response.status_code == 429 or response.status_code >= 500:
                    logger.warning(
                        "Federal Register transient error attempt=%d status=%d",
                        attempt,
                        response.status_code,
                    )
                else:
                    response.raise_for_status()
                last_exc = httpx.HTTPStatusError(
                    f"Federal Register returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if attempt < _FR_MAX_RETRIES:
                time.sleep(
                    exponential_backoff_with_jitter(attempt, base_seconds=1.0, max_seconds=60.0)
                )
        raise RateLimitedError(
            f"Federal Register rate-limited or transient-failed past "
            f"{_FR_MAX_RETRIES} retries: {last_exc}"
        )
