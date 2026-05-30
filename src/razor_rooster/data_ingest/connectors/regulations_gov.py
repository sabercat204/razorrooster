"""regulations.gov connector — EPA dockets (T-063).

Pulls regulatory dockets from regulations.gov v4 API at
``https://api.regulations.gov/v4/dockets``. Scoped to EPA dockets in v1
per design §3.1; other agencies addable via configuration in a later
version.

Authentication is via single API key passed in the ``X-Api-Key``
header. Free-tier rate limit is 1000 requests/hour. The connector
applies a conservative client-side limiter at one request per 0.5
seconds.

Records map to the ``document_docket`` canonical schema. Backfill resume
tokens are ``YYYY-MM-DD:page``; the API uses page-based pagination with
a maximum page size of 250.
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
    DocumentDocketRecord,
    NormalizedRecord,
    RawRecord,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import register

logger = logging.getLogger(__name__)


_REG_BASE_URL: Final[str] = "https://api.regulations.gov/v4"
_REG_DOCKETS_PATH: Final[str] = "/dockets"
_REG_TIMEOUT_SECONDS: Final[float] = 30.0
_REG_MIN_INTERVAL_SECONDS: Final[float] = 0.5
_REG_MAX_RETRIES: Final[int] = 5
_REG_PAGE_SIZE: Final[int] = 250
_REG_AGENCY: Final[str] = "EPA"


@register
class RegulationsGovConnector(Connector):
    """Pulls EPA dockets from regulations.gov into the ``document_docket`` schema."""

    source_id = "regulations_gov"
    title = "regulations.gov — EPA dockets"
    canonical_schema = SchemaType.DOCUMENT_DOCKET
    license = License.PUBLIC_DOMAIN
    cadence_default = "daily"
    backfill_supported = True
    connector_version = "regulations_gov@0.1.0"
    license_noncommercial_required = False

    def __init__(
        self,
        store: DuckDBStore,
        *,
        credentials: CredentialBundle | None = None,
        client: httpx.Client | None = None,
        agency: str = _REG_AGENCY,
    ) -> None:
        super().__init__(store, credentials=credentials)
        self._client = client or httpx.Client(timeout=_REG_TIMEOUT_SECONDS, follow_redirects=True)
        self._owns_client = client is None
        self._last_request_at: float = 0.0
        self._agency = agency

    def __del__(self) -> None:
        try:
            if self._owns_client:
                self._client.close()
        except Exception:  # pragma: no cover
            pass

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        api_key = self._require_api_key()
        for record, _page in self._iter_pages(
            api_key=api_key, modified_since=since.date(), starting_page=1
        ):
            yield record

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        api_key = self._require_api_key()
        until_date = until.date()
        starting_page = 1
        if resume_token is not None:
            parts = resume_token.value.split(":")
            if len(parts) == 2:
                starting_page = int(parts[1]) + 1
        for record, page in self._iter_pages(
            api_key=api_key,
            modified_since=date(2003, 1, 1),
            modified_until=until_date,
            starting_page=starting_page,
        ):
            yield record, ResumeToken(value=f"{until_date.isoformat()}:{page}")

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.source_payload_json
        attributes_raw = payload.get("attributes")
        attributes: dict[str, Any] = attributes_raw if isinstance(attributes_raw, dict) else {}
        title = str(attributes.get("title") or "")
        published = self._parse_date(attributes.get("modifyDate") or attributes.get("postedDate"))
        return DocumentDocketRecord(
            source_id=self.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=payload,
            title=title,
            document_type=str(attributes.get("docketType") or "") or None,
            docket_id=str(payload.get("id") or "") or None,
            agency=str(attributes.get("agencyId") or self._agency),
            published_date=published,
            effective_date=None,
            comment_close_date=self._parse_date(attributes.get("commentEndDate")),
            full_text_uri=self._docket_url(payload.get("id")),
            full_text_local_path=None,
        )

    # --- internals ---------------------------------------------------------

    def _require_api_key(self) -> str:
        if not isinstance(self.credentials, ApiKeyBundle):
            raise CredentialMissingError(
                "regulations.gov connector requires REGULATIONS_GOV_API_KEY in .env"
            )
        return self.credentials.api_key

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _REG_MIN_INTERVAL_SECONDS:
            time.sleep(_REG_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _iter_pages(
        self,
        *,
        api_key: str,
        modified_since: date,
        modified_until: date | None = None,
        starting_page: int = 1,
    ) -> Iterator[tuple[RawRecord, int]]:
        page = starting_page
        end = modified_until or datetime.now(tz=UTC).date()
        while True:
            params: list[tuple[str, str | int | float | bool | None]] = [
                ("filter[agencyId]", self._agency),
                ("filter[lastModifiedDate][ge]", modified_since.isoformat()),
                ("filter[lastModifiedDate][le]", end.isoformat()),
                ("page[number]", str(page)),
                ("page[size]", str(_REG_PAGE_SIZE)),
                ("sort", "lastModifiedDate"),
            ]
            body = self._request_with_retry(_REG_DOCKETS_PATH, params, api_key=api_key)
            data = body.get("data", []) if isinstance(body, dict) else []
            if not isinstance(data, list):
                return
            if not data:
                return
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                yield self._build_record(entry), page
            meta = body.get("meta", {}) if isinstance(body, dict) else {}
            total_pages = int(meta.get("totalPages", 1)) if isinstance(meta, dict) else 1
            if page >= total_pages:
                return
            page += 1

    def _build_record(self, entry: dict[str, Any]) -> RawRecord:
        docket_id = entry.get("id") or ""
        attributes_raw = entry.get("attributes")
        attributes: dict[str, Any] = attributes_raw if isinstance(attributes_raw, dict) else {}
        publication_ts = self._parse_iso(
            attributes.get("modifyDate") or attributes.get("postedDate")
        )
        return RawRecord(
            source_id=self.source_id,
            source_record_id=str(docket_id),
            source_payload_json=entry,
            source_publication_ts=publication_ts,
        )

    def _parse_date(self, value: Any) -> date | None:
        if not value:
            return None
        text = str(value)
        try:
            return date.fromisoformat(text[:10])
        except (TypeError, ValueError):
            return None

    def _parse_iso(self, value: Any) -> datetime:
        if not value:
            return datetime.now(tz=UTC)
        try:
            d = date.fromisoformat(str(value)[:10])
            return datetime.combine(d, datetime.min.time(), tzinfo=UTC)
        except (TypeError, ValueError):
            return datetime.now(tz=UTC)

    def _docket_url(self, docket_id: Any) -> str | None:
        if not docket_id:
            return None
        return f"https://www.regulations.gov/docket/{docket_id}"

    def _request_with_retry(
        self,
        path: str,
        params: list[tuple[str, str | int | float | bool | None]],
        *,
        api_key: str,
    ) -> dict[str, Any]:
        url = f"{_REG_BASE_URL}{path}"
        headers = {"X-Api-Key": api_key}
        last_exc: Exception | None = None
        for attempt in range(_REG_MAX_RETRIES + 1):
            self._rate_limit()
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "regulations.gov request error attempt=%d error=%s",
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
                        "regulations.gov transient error attempt=%d status=%d",
                        attempt,
                        response.status_code,
                    )
                else:
                    response.raise_for_status()
                last_exc = httpx.HTTPStatusError(
                    f"regulations.gov returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if attempt < _REG_MAX_RETRIES:
                time.sleep(
                    exponential_backoff_with_jitter(attempt, base_seconds=1.0, max_seconds=60.0)
                )
        raise RateLimitedError(
            f"regulations.gov rate-limited or transient-failed past "
            f"{_REG_MAX_RETRIES} retries: {last_exc}"
        )
