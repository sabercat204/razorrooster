"""ACLED connector — events ingestion via OAuth 2.0 password grant (T-060).

Implements the connector half of the v0.12.0 ACLED amendment. The
companion deletions reconciliation lives in T-064 (acled_deletions.py)
and shares the OAuth client constructed here.

Authentication
--------------
ACLED uses OAuth 2.0 password grant against ``acleddata.com/oauth/token``:

- POST form-encoded with ``username`` + ``password`` + ``grant_type=password``
  + ``client_id=acled`` + ``scope=authenticated``.
- Response carries ``access_token`` (24h TTL) and ``refresh_token`` (14d TTL).
- Subsequent data calls send ``Authorization: Bearer <access_token>``.

Tokens are cached in process memory only (REQ-ACLED-AUTH-002). They are
never written to DuckDB, log files, or env files. On expiry, the
connector first attempts a refresh; on refresh failure it falls back to
a full password grant. Bearer tokens never appear in URL query strings
(REQ-ACLED-AUTH-003).

License gate
------------
The first call to a fetch method invokes the Terms acknowledgement gate
(REQ-ACLED-LICENSE-001). The gate fetches the SHA-256 of ACLED's
canonical Terms text and verifies it matches the hash recorded in the
``sources`` row. If the row has no acknowledgement or a different hash,
the connector raises :class:`AcledTermsAcknowledgementRequired` with
operator-facing instructions. The CLI ack-tos subcommand (TBD in a
future task) is the legitimate path to acknowledgement; for now the gate
can be satisfied programmatically via
:func:`record_license_acknowledgement` from
``razor_rooster.data_ingest.persistence.provenance``.

ACLED data is conservatively treated as non-commercial-use only
(REQ-ACLED-LICENSE-002). Until the operator records an explicit
``commercial_use_recorded_grant``, downstream subsystems must check the
flag before exporting derived data.

Pagination & backfill
---------------------
The events endpoint uses ``page=`` pagination with a 5,000-row default
limit and terminates when a page returns fewer rows than the limit
(REQ-ACLED-EVENTS-002). Backfill is year-bounded via
``year_where=BETWEEN`` chunks (REQ-ACLED-EVENTS-003), one calendar year
per chunk by default. The first page of each chunk requests
``with_total=true`` for progress reporting (REQ-ACLED-EVENTS-004).

Resume tokens are ``<year>:<page>`` of the most recently committed
page; the next run continues at the next page within the same year, or
rolls forward to the next year when the current year's pages are
exhausted.

Rate limiting
-------------
ACLED's documentation does not publish a per-account RPS. The connector
applies a conservative client-side limiter at 1 request / 0.2 seconds
(= 5 req/s). On 429 responses the limiter pauses and the framework's
exponential-backoff retries kick in (capped at 5 attempts; persistent
failures raise :class:`RateLimitedError`).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

import httpx

from razor_rooster.data_ingest.connectors.base import (
    Connector,
    ConnectorError,
    CredentialMissingError,
    License,
    RateLimitedError,
    ResumeToken,
    exponential_backoff_with_jitter,
)
from razor_rooster.data_ingest.credentials import (
    CredentialBundle,
    UserPasswordBundle,
)
from razor_rooster.data_ingest.normalization.base import (
    EventStreamRecord,
    NormalizedRecord,
    RawRecord,
)
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.provenance import (
    get_license_posture,
)
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import register

logger = logging.getLogger(__name__)


_ACLED_BASE_URL: Final[str] = "https://acleddata.com"
_ACLED_TOKEN_PATH: Final[str] = "/oauth/token"
_ACLED_EVENTS_PATH: Final[str] = "/api/acled/read"
_ACLED_TIMEOUT_SECONDS: Final[float] = 30.0
_ACLED_MIN_INTERVAL_SECONDS: Final[float] = 0.2
_ACLED_MAX_RETRIES: Final[int] = 5
_ACLED_PAGE_SIZE: Final[int] = 5000

# Refresh the access token when its remaining lifetime drops below this many
# seconds. Below this threshold, the connector preemptively refreshes rather
# than risking a mid-batch 401.
_ACLED_TOKEN_REFRESH_BUFFER_SECONDS: Final[int] = 300

# Default earliest year to backfill from. Operator may override but ACLED's
# data goes back to roughly 1997 in regions where collection is mature.
_ACLED_EARLIEST_BACKFILL_YEAR: Final[int] = 1997

# Fields the connector requests from the ACLED API. Restricting fields
# reduces payload size and gives the connector a stable contract; if ACLED
# adds new fields they aren't silently absorbed without code review.
_ACLED_FIELDS: Final[tuple[str, ...]] = (
    "event_id_cnty",
    "event_date",
    "year",
    "time_precision",
    "event_type",
    "sub_event_type",
    "actor1",
    "assoc_actor_1",
    "inter1",
    "actor2",
    "assoc_actor_2",
    "inter2",
    "interaction",
    "country",
    "iso3",
    "region",
    "admin1",
    "admin2",
    "admin3",
    "location",
    "latitude",
    "longitude",
    "geo_precision",
    "source",
    "source_scale",
    "notes",
    "fatalities",
    "timestamp",
)


class AcledTermsAcknowledgementRequired(ConnectorError):
    """Raised when ACLED's startup gate cannot proceed.

    The connector refuses to fetch data until the operator has explicitly
    recorded their acknowledgement of ACLED's current Terms via
    :func:`record_license_acknowledgement` (T-015).
    """


@dataclass(slots=True)
class _CachedToken:
    """In-memory cache of an OAuth 2.0 token pair.

    Tokens are never persisted. The cache lives only as long as the
    connector instance does.
    """

    access_token: str
    refresh_token: str
    expires_at: datetime
    refresh_expires_at: datetime
    issued_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def is_access_expired_or_near(
        self, *, buffer_seconds: int = _ACLED_TOKEN_REFRESH_BUFFER_SECONDS
    ) -> bool:
        return datetime.now(tz=UTC) >= self.expires_at - timedelta(seconds=buffer_seconds)

    def is_refresh_expired(self) -> bool:
        return datetime.now(tz=UTC) >= self.refresh_expires_at


def fetch_acled_terms_hash(client: httpx.Client | None = None) -> str:
    """Return the SHA-256 of ACLED's canonical Terms text.

    The function fetches ``acleddata.com/terms-and-conditions`` over HTTPS
    and hashes the response body verbatim. If the network is unavailable
    the function raises; callers handle the failure (the gate falls back
    to a recorded last-known hash if present).
    """
    owns_client = client is None
    c = client or httpx.Client(timeout=_ACLED_TIMEOUT_SECONDS, follow_redirects=True)
    try:
        response = c.get(f"{_ACLED_BASE_URL}/terms-and-conditions")
        response.raise_for_status()
        body = response.content
    finally:
        if owns_client:
            c.close()
    return hashlib.sha256(body).hexdigest()


@register
class AcledConnector(Connector):
    """ACLED events connector (REQ-ACLED-AUTH/EVENTS/LICENSE family)."""

    source_id = "acled"
    title = "ACLED — Armed Conflict Location & Event Data"
    canonical_schema = SchemaType.EVENT_STREAM
    license = License.ACLED_TERMS_VERSIONED
    cadence_default = "daily"
    backfill_supported = True
    connector_version = "acled@0.1.0"
    license_noncommercial_required = True

    def __init__(
        self,
        store: DuckDBStore,
        *,
        credentials: CredentialBundle | None = None,
        client: httpx.Client | None = None,
        skip_terms_gate: bool = False,
    ) -> None:
        super().__init__(store, credentials=credentials)
        self._client = client or httpx.Client(timeout=_ACLED_TIMEOUT_SECONDS, follow_redirects=True)
        self._owns_client = client is None
        self._last_request_at: float = 0.0
        self._token_cache: _CachedToken | None = None
        self._terms_gate_passed = skip_terms_gate

    def __del__(self) -> None:
        try:
            if self._owns_client:
                self._client.close()
        except Exception:  # pragma: no cover
            pass

    def fetch_incremental(self, since: datetime) -> Iterator[RawRecord]:
        self._ensure_terms_gate()
        creds = self._require_password_credentials()
        access_token = self._ensure_access_token(creds)
        # Incremental: walk from since.year through the current year. Within
        # each year, paginate until exhausted.
        current_year = datetime.now(tz=UTC).year
        for yr in range(since.year, current_year + 1):
            for record, _page in self._iter_year_pages(
                year=yr,
                access_token=access_token,
                creds=creds,
                start_page=1,
                event_date_gte=since.date() if yr == since.year else None,
            ):
                yield record

    def fetch_backfill(
        self,
        until: datetime,
        resume_token: ResumeToken | None = None,
    ) -> Iterator[tuple[RawRecord, ResumeToken | None]]:
        self._ensure_terms_gate()
        creds = self._require_password_credentials()
        access_token = self._ensure_access_token(creds)

        until_year = until.year
        resume_year: int | None = None
        resume_page: int | None = None
        if resume_token is not None:
            parts = resume_token.value.split(":")
            if len(parts) == 2:
                resume_year = int(parts[0])
                resume_page = int(parts[1])

        start_year = resume_year or _ACLED_EARLIEST_BACKFILL_YEAR
        for yr in range(start_year, until_year + 1):
            page = (resume_page + 1) if (yr == resume_year and resume_page is not None) else 1
            for record, current_page in self._iter_year_pages(
                year=yr,
                access_token=access_token,
                creds=creds,
                start_page=page,
            ):
                yield record, ResumeToken(value=f"{yr}:{current_page}")
            resume_page = None  # next year starts fresh

    def normalize(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.source_payload_json
        event_date_raw = payload.get("event_date")
        event_ts = self._parse_event_date(event_date_raw)
        country_iso3 = payload.get("iso3") or None
        # ACLED's ``actor1`` and ``actor2`` are the principal parties. The
        # ``assoc_actor_1`` / ``assoc_actor_2`` fields are secondary; we
        # don't surface them as actor_secondary because that field is for
        # the *opposing* party.
        return EventStreamRecord(
            source_id=self.source_id,
            source_record_id=raw.source_record_id,
            source_publication_ts=raw.source_publication_ts,
            fetch_ts=datetime.now(tz=UTC),
            connector_version=self.connector_version,
            source_payload_json=payload,
            event_ts=event_ts,
            country_iso3=str(country_iso3) if country_iso3 else None,
            actor_primary=str(payload.get("actor1") or "") or None,
            actor_secondary=str(payload.get("actor2") or "") or None,
            event_class=str(payload.get("event_type") or "") or None,
            description=str(payload.get("notes") or "") or None,
        )

    # --- Terms gate --------------------------------------------------------

    def _ensure_terms_gate(self) -> None:
        """Raise if the operator hasn't acknowledged ACLED's current Terms.

        REQ-ACLED-LICENSE-001: the connector refuses to fetch data until
        the ``sources`` row records an acknowledgement matching the live
        Terms hash. If the live fetch fails (e.g., network down), we fall
        back to the recorded hash and warn rather than blocking.
        """
        if self._terms_gate_passed:
            return
        with self.store.connection() as conn:
            posture = get_license_posture(conn, self.source_id)
        if posture is None:
            raise AcledTermsAcknowledgementRequired(
                f"source {self.source_id!r} is not registered yet; "
                "register it via register_source() before fetching"
            )
        if posture.license != License.ACLED_TERMS_VERSIONED.value:
            raise AcledTermsAcknowledgementRequired(
                f"source {self.source_id!r} has license {posture.license!r}; "
                f"expected {License.ACLED_TERMS_VERSIONED.value!r}"
            )
        if posture.license_terms_hash is None or posture.license_acknowledged_at is None:
            raise AcledTermsAcknowledgementRequired(
                "ACLED Terms have not been acknowledged for this source. "
                "Run the ack-tos workflow to record the operator's acknowledgement "
                "of acleddata.com/terms-and-conditions and the conservative "
                "non-commercial-use posture (REQ-ACLED-LICENSE-002)."
            )
        try:
            live_hash = fetch_acled_terms_hash(self._client)
        except httpx.HTTPError as exc:
            logger.warning(
                "ACLED Terms hash live-fetch failed; using recorded hash: %s",
                exc,
            )
            self._terms_gate_passed = True
            return
        if live_hash != posture.license_terms_hash:
            raise AcledTermsAcknowledgementRequired(
                "ACLED Terms hash has changed since the last acknowledgement. "
                f"Recorded: {posture.license_terms_hash[:12]}…; "
                f"live: {live_hash[:12]}…. "
                "Re-acknowledge before continuing."
            )
        self._terms_gate_passed = True

    # --- credentials & OAuth ----------------------------------------------

    def _require_password_credentials(self) -> UserPasswordBundle:
        if not isinstance(self.credentials, UserPasswordBundle):
            raise CredentialMissingError(
                "ACLED connector requires ACLED_USERNAME and ACLED_PASSWORD in .env"
            )
        return self.credentials

    def _ensure_access_token(self, creds: UserPasswordBundle) -> str:
        """Return a usable access token, refreshing or re-issuing as needed."""
        cache = self._token_cache
        if cache is None:
            self._token_cache = self._password_grant(creds)
            return self._token_cache.access_token
        if not cache.is_access_expired_or_near():
            return cache.access_token
        if not cache.is_refresh_expired():
            try:
                self._token_cache = self._refresh_token(cache.refresh_token)
                return self._token_cache.access_token
            except _AcledTokenError as exc:
                logger.warning(
                    "ACLED token refresh failed (%s); falling back to password grant",
                    exc,
                )
        # Refresh expired or refresh failed → full password grant.
        self._token_cache = self._password_grant(creds)
        return self._token_cache.access_token

    def _password_grant(self, creds: UserPasswordBundle) -> _CachedToken:
        url = f"{_ACLED_BASE_URL}{_ACLED_TOKEN_PATH}"
        data = {
            "username": creds.username,
            "password": creds.password,
            "grant_type": "password",
            "client_id": "acled",
            "scope": "authenticated",
        }
        return self._token_request(url, data, label="password grant")

    def _refresh_token(self, refresh_token: str) -> _CachedToken:
        url = f"{_ACLED_BASE_URL}{_ACLED_TOKEN_PATH}"
        data = {
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "client_id": "acled",
        }
        return self._token_request(url, data, label="refresh")

    def _token_request(self, url: str, data: dict[str, str], *, label: str) -> _CachedToken:
        self._rate_limit()
        try:
            response = self._client.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError as exc:
            raise _AcledTokenError(f"{label} request error: {exc}") from exc
        if response.status_code != 200:
            raise _AcledTokenError(
                f"{label} returned {response.status_code}: {response.text[:200]}"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise _AcledTokenError(f"{label} returned non-dict body")
        access_token = body.get("access_token")
        refresh_token = body.get("refresh_token")
        expires_in = body.get("expires_in", 0)
        if not access_token or not refresh_token:
            raise _AcledTokenError(f"{label} response missing tokens")
        now = datetime.now(tz=UTC)
        return _CachedToken(
            access_token=str(access_token),
            refresh_token=str(refresh_token),
            expires_at=now + timedelta(seconds=int(expires_in)),
            # Refresh token TTL is 14 days per ACLED docs.
            refresh_expires_at=now + timedelta(days=14),
            issued_at=now,
        )

    # --- pagination & data fetch ------------------------------------------

    def _iter_year_pages(
        self,
        *,
        year: int,
        access_token: str,
        creds: UserPasswordBundle,
        start_page: int = 1,
        event_date_gte: date | None = None,
    ) -> Iterator[tuple[RawRecord, int]]:
        page = start_page
        while True:
            params: list[tuple[str, str | int | float | bool | None]] = [
                ("_format", "json"),
                ("year", str(year)),
                ("limit", str(_ACLED_PAGE_SIZE)),
                ("page", str(page)),
                ("fields", "|".join(_ACLED_FIELDS)),
            ]
            if event_date_gte is not None:
                params.append(("event_date", event_date_gte.isoformat()))
                params.append(("event_date_where", ">="))
            if page == start_page:
                params.append(("with_total", "true"))

            body = self._authed_get(_ACLED_EVENTS_PATH, params, access_token, creds)
            data = body.get("data", []) if isinstance(body, dict) else []
            if not isinstance(data, list):
                logger.warning(
                    "ACLED returned non-list data field for year=%d page=%d",
                    year,
                    page,
                )
                return
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                yield self._build_record(entry), page
            if len(data) < _ACLED_PAGE_SIZE:
                return
            page += 1

    def _build_record(self, entry: dict[str, Any]) -> RawRecord:
        event_id = entry.get("event_id_cnty") or ""
        event_date_raw = entry.get("event_date") or ""
        publication_ts = self._parse_event_date(event_date_raw)
        return RawRecord(
            source_id=self.source_id,
            source_record_id=str(event_id),
            source_payload_json=entry,
            source_publication_ts=publication_ts,
        )

    def _parse_event_date(self, value: Any) -> datetime:
        if not value:
            return datetime.now(tz=UTC)
        text = str(value)
        try:
            return datetime.combine(date.fromisoformat(text), datetime.min.time(), tzinfo=UTC)
        except (TypeError, ValueError):
            return datetime.now(tz=UTC)

    # --- HTTP machinery ----------------------------------------------------

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _ACLED_MIN_INTERVAL_SECONDS:
            time.sleep(_ACLED_MIN_INTERVAL_SECONDS - elapsed)
        self._last_request_at = time.monotonic()

    def _authed_get(
        self,
        path: str,
        params: list[tuple[str, str | int | float | bool | None]],
        access_token: str,
        creds: UserPasswordBundle,
    ) -> dict[str, Any]:
        url = f"{_ACLED_BASE_URL}{path}"
        last_exc: Exception | None = None
        for attempt in range(_ACLED_MAX_RETRIES + 1):
            self._rate_limit()
            try:
                response = self._client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "ACLED request error attempt=%d error=%s",
                    attempt,
                    type(exc).__name__,
                )
            else:
                if response.status_code == 200:
                    parsed = response.json()
                    if not isinstance(parsed, dict):
                        return {}
                    return parsed
                if response.status_code == 401:
                    # Token expired between rate-limit calls; refresh and retry.
                    logger.info("ACLED 401 — refreshing token and retrying")
                    self._token_cache = self._password_grant(creds)
                    access_token = self._token_cache.access_token
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    logger.warning(
                        "ACLED transient error attempt=%d status=%d",
                        attempt,
                        response.status_code,
                    )
                else:
                    response.raise_for_status()
                last_exc = httpx.HTTPStatusError(
                    f"ACLED returned {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if attempt < _ACLED_MAX_RETRIES:
                time.sleep(
                    exponential_backoff_with_jitter(attempt, base_seconds=1.0, max_seconds=60.0)
                )
        raise RateLimitedError(
            f"ACLED rate-limited or transient-failed past {_ACLED_MAX_RETRIES} retries: {last_exc}"
        )


class _AcledTokenError(ConnectorError):
    """Internal: raised when the OAuth token endpoint returns an error."""
