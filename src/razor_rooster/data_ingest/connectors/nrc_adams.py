"""NRC ADAMS Public Search connector (T-062, OQ-004 resolution).

Pulls publicly-released documents from the U.S. Nuclear Regulatory
Commission's ADAMS Public Search API (Azure-managed) at
``https://adams-api-developer.nrc.gov``.

Authentication is via single API key passed in the
``Ocp-Apim-Subscription-Key`` header — Azure API Management's standard
subscription-key header. The connector loads it from
``NRC_ADAMS_API_KEY``.

The API returns documents in PARS Library (1999+) by default; we don't
target the Public Legacy Library (pre-1999) in v1.

Records map to the ``document_docket`` canonical schema with
``full_text_uri`` pointing at the source-hosted PDF; v1 does not cache
full text locally (per design DEFER-004).

Backfill resume tokens are ``YYYY-MM-DD:offset``.
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


_NRC_BASE_URL: Final[str] = "https://adams-api-developer.nrc.gov"
_NRC_DEFAULT_PATH: Final[str] = "/api/PublicSearch/Documents"
_NRC_TIMEOUT_SECONDS: Final[float] = 30.0
_NRC_MIN_INTERVAL_SECONDS: Final[float] = 0.5
_NRC_MAX_RETRIES: Final[int] = 5
_NRC_PAGE_SIZE: Final[int] = 100


@register
class NrcAdamsConnector(Connector):
    """Pulls NRC ADAMS Public Search documents into the ``document_docket`` schema."""

    source_id = "nrc_adams"
    title = "NRC ADAMS Public Search"
    canonical_schema = SchemaType.DOCUMENT_DOCKET
    license = License.PUBLIC_DOMAIN
    cadence_default = "weekly"
    backfill_supported = True
    connector_version = "nrc_adams@0.1.0"
    license_noncommercial_required = False

    def __init__(
        self,
        store: DuckDBStore,
        *,
        credentials: CredentialBundle | None = None,
        client: httpx.Client | None = None,
        api_path: str | None = None,
    ) -> None:
        super().__init__(store, credentials=credentials)
        self._client = client or httpx.Client(timeout=_NRC_TIMEOUT_SECONDS, follow_redirects=True)
        self._owns_client = client is None
        self._last_request_at: float = 0.0
        self._api_path = api_path or _NRC_DEFAULT_PATH

    def __del__(self) -> None:
        try:
            if self._owns_client:
                self._client.close()
        except Exception:  # pragma: no cover
            pass

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        api_key = self._require_api_key()
        for record, _offset in self._iter_pages(
            api_key=api_key, document_date_gte=since.date(), starting_offset=0
        ):
            yield record

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        api_key = self._require_api_key()
        until_date = until.date()
        resume_offset = 0
        if resume_token is not None:
            parts = resume_token.value.split(":")
            if len(parts) == 2:
                resume_offset = int(parts[1]) + _NRC_PAGE_SIZE
        for record, offset in self._iter_pages(
            api_key=api_key,
            document_date_gte=date(1999, 1, 1),
            document_date_lte=until_date,
            starting_offset=resume_offset,
        ):
            yield record, ResumeToken(value=f"{until_date.isoformat()}:{offset}")

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.source_payload_json
        title = str(payload.get("documentTitle") or payload.get("title") or "")
        published_str = payload.get("documentDate") or payload.get("publishDate")
        published = self._parse_date(published_str)
        return DocumentDocketRecord(
            source_id=self.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=payload,
            title=title,
            document_type=str(payload.get("documentType") or "") or None,
            docket_id=str(payload.get("docketNumber") or payload.get("docketId") or "") or None,
            agency="U.S. Nuclear Regulatory Commission",
            published_date=published,
            effective_date=None,
            comment_close_date=None,
            full_text_uri=str(payload.get("documentUri") or payload.get("documentUrl") or "")
            or None,
            full_text_local_path=None,
        )

    # --- internals ---------------------------------------------------------

    def _require_api_key(self) -> str:
        if not isinstance(self.credentials, ApiKeyBundle):
            raise CredentialMissingError("NRC ADAMS connector requires NRC_ADAMS_API_KEY in .env")
        return self.credentials.api_key

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _NRC_MIN_INTERVAL_SECONDS:
            time.sleep(_NRC_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _iter_pages(
        self,
        *,
        api_key: str,
        document_date_gte: date,
        document_date_lte: date | None = None,
        starting_offset: int = 0,
    ) -> Iterator[tuple[RawRecord, int]]:
        offset = starting_offset
        end = document_date_lte or datetime.now(tz=UTC).date()
        while True:
            params: list[tuple[str, str | int | float | bool | None]] = [
                ("documentDate.gte", document_date_gte.isoformat()),
                ("documentDate.lte", end.isoformat()),
                ("offset", str(offset)),
                ("limit", str(_NRC_PAGE_SIZE)),
                ("sortBy", "documentDate"),
                ("sortOrder", "asc"),
            ]
            body = self._request_with_retry(self._api_path, params, api_key=api_key)
            results = body.get("results", []) if isinstance(body, dict) else []
            if not isinstance(results, list):
                return
            if not results:
                return
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                yield self._build_record(entry), offset
            offset += len(results)
            total = int(body.get("totalCount", 0)) if isinstance(body, dict) else 0
            if total > 0 and offset >= total:
                return
            if len(results) < _NRC_PAGE_SIZE:
                return

    def _build_record(self, entry: dict[str, Any]) -> RawRecord:
        accession = (
            entry.get("accessionNumber")
            or entry.get("accession_number")
            or entry.get("documentId")
            or ""
        )
        publication_ts = self._parse_iso(entry.get("documentDate") or entry.get("publishDate"))
        return RawRecord(
            source_id=self.source_id,
            source_record_id=str(accession),
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
        text = str(value)
        try:
            d = date.fromisoformat(text[:10])
            return datetime.combine(d, datetime.min.time(), tzinfo=UTC)
        except (TypeError, ValueError):
            return datetime.now(tz=UTC)

    def _request_with_retry(
        self,
        path: str,
        params: list[tuple[str, str | int | float | bool | None]],
        *,
        api_key: str,
    ) -> dict[str, Any]:
        url = f"{_NRC_BASE_URL}{path}"
        headers = {"Ocp-Apim-Subscription-Key": api_key}
        last_exc: Exception | None = None
        for attempt in range(_NRC_MAX_RETRIES + 1):
            self._rate_limit()
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "NRC ADAMS request error attempt=%d error=%s",
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
                        "NRC ADAMS transient error attempt=%d status=%d",
                        attempt,
                        response.status_code,
                    )
                else:
                    response.raise_for_status()
                last_exc = httpx.HTTPStatusError(
                    f"NRC ADAMS returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if attempt < _NRC_MAX_RETRIES:
                time.sleep(
                    exponential_backoff_with_jitter(attempt, base_seconds=1.0, max_seconds=60.0)
                )
        raise RateLimitedError(
            f"NRC ADAMS rate-limited or transient-failed past "
            f"{_NRC_MAX_RETRIES} retries: {last_exc}"
        )
