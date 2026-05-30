"""T-060 verification — ACLED connector."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from razor_rooster.data_ingest.connectors.acled import (
    AcledConnector,
    AcledTermsAcknowledgementRequired,
    fetch_acled_terms_hash,
)
from razor_rooster.data_ingest.connectors.base import (
    CredentialMissingError,
    License,
    RateLimitedError,
    ResumeToken,
)
from razor_rooster.data_ingest.credentials import (
    ApiKeyBundle,
    UserPasswordBundle,
)
from razor_rooster.data_ingest.normalization.base import EventStreamRecord, RawRecord
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.provenance import (
    record_license_acknowledgement,
    register_source,
)
from razor_rooster.data_ingest.persistence.schemas import SchemaType
from razor_rooster.data_ingest.registry import is_registered


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "acled.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


@pytest.fixture
def credentials() -> UserPasswordBundle:
    return UserPasswordBundle(
        source_id="acled", username="test@example.com", password="test_password"
    )


@pytest.fixture
def acled_acknowledged_store(store: DuckDBStore, tmp_path: Path) -> DuckDBStore:
    """Store with ACLED registered and Terms acknowledged for a known hash."""
    terms_text = b"acled terms content for testing"
    expected_hash = hashlib.sha256(terms_text).hexdigest()
    with store.connection() as conn:
        register_source(
            conn,
            source_id="acled",
            source_type="event_stream",
            cadence="daily",
            freshness_threshold_seconds=259200,
            license="ACLED_TERMS_VERSIONED",
            license_noncommercial_required=True,
        )
        record_license_acknowledgement(conn, source_id="acled", terms_hash=expected_hash)
    return store


# Routing transport: dispatch based on URL path so we can canned-respond
# differently for token requests vs. data requests.
class _RoutingTransport(httpx.MockTransport):
    def __init__(
        self,
        token_responses: list[tuple[int, dict[str, Any]]] | None = None,
        events_responses: list[tuple[int, dict[str, Any]]] | None = None,
        terms_response: tuple[int, bytes] | None = None,
        deleted_responses: list[tuple[int, dict[str, Any]]] | None = None,
    ) -> None:
        self.token_responses = list(token_responses or [])
        self.events_responses = list(events_responses or [])
        self.terms_response = terms_response
        self.deleted_responses = list(deleted_responses or [])
        self.requests_received: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests_received.append(request)
            path = request.url.path
            if path.endswith("/oauth/token"):
                if not self.token_responses:
                    return httpx.Response(500, json={"error": "no token canned"})
                status, body = self.token_responses.pop(0)
                return httpx.Response(status, json=body)
            if path == "/api/acled/read":
                if not self.events_responses:
                    return httpx.Response(500, json={"error": "no events canned"})
                status, body = self.events_responses.pop(0)
                return httpx.Response(status, json=body)
            if path == "/api/deleted/read":
                if not self.deleted_responses:
                    return httpx.Response(200, json={"data": []})
                status, body = self.deleted_responses.pop(0)
                return httpx.Response(status, json=body)
            if path == "/terms-and-conditions":
                if self.terms_response is None:
                    return httpx.Response(500, content=b"no terms canned")
                status, body = self.terms_response
                return httpx.Response(status, content=body)
            return httpx.Response(404)

        super().__init__(handler)


def _client(transport: _RoutingTransport) -> httpx.Client:
    return httpx.Client(transport=transport, timeout=5.0)


def _token_response(*, expires_in: int = 86400) -> dict[str, Any]:
    return {
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "token_type": "Bearer",
        "expires_in": expires_in,
    }


def _events_page(events: list[dict[str, Any]], *, count: int | None = None) -> dict[str, Any]:
    return {
        "success": True,
        "count": count if count is not None else len(events),
        "data": events,
    }


def _event(
    *,
    event_id: str,
    event_date: str = "2026-05-14",
    actor1: str = "Actor A",
    actor2: str = "",
    iso3: str = "SOM",
    event_type: str = "Violence against civilians",
) -> dict[str, Any]:
    return {
        "event_id_cnty": event_id,
        "event_date": event_date,
        "year": int(event_date[:4]),
        "actor1": actor1,
        "actor2": actor2,
        "iso3": iso3,
        "event_type": event_type,
        "fatalities": 0,
        "notes": "test event note",
    }


# --- registration & class attributes ---------------------------------------


def test_acled_self_registers() -> None:
    assert is_registered("acled")


def test_class_attributes() -> None:
    assert AcledConnector.source_id == "acled"
    assert AcledConnector.canonical_schema == SchemaType.EVENT_STREAM
    assert AcledConnector.license == License.ACLED_TERMS_VERSIONED
    assert AcledConnector.license_noncommercial_required is True
    assert AcledConnector.backfill_supported is True


# --- Terms gate ------------------------------------------------------------


def test_unregistered_source_raises_terms_gate(
    store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    transport = _RoutingTransport(terms_response=(200, b"terms text"))
    connector = AcledConnector(store, credentials=credentials, client=_client(transport))
    with pytest.raises(AcledTermsAcknowledgementRequired, match="not registered"):
        list(connector.fetch_incremental(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_registered_but_unacknowledged_raises(
    store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    with store.connection() as conn:
        register_source(
            conn,
            source_id="acled",
            source_type="event_stream",
            cadence="daily",
            freshness_threshold_seconds=259200,
            license="ACLED_TERMS_VERSIONED",
            license_noncommercial_required=True,
        )
    transport = _RoutingTransport(terms_response=(200, b"terms text"))
    connector = AcledConnector(store, credentials=credentials, client=_client(transport))
    with pytest.raises(AcledTermsAcknowledgementRequired, match="not been acknowledged"):
        list(connector.fetch_incremental(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_changed_terms_hash_raises(store: DuckDBStore, credentials: UserPasswordBundle) -> None:
    """Recorded hash != live hash should re-prompt."""
    with store.connection() as conn:
        register_source(
            conn,
            source_id="acled",
            source_type="event_stream",
            cadence="daily",
            freshness_threshold_seconds=259200,
            license="ACLED_TERMS_VERSIONED",
            license_noncommercial_required=True,
        )
        record_license_acknowledgement(conn, source_id="acled", terms_hash="a" * 64)
    # Live hash is for "different terms text", which won't match "aa…aa".
    transport = _RoutingTransport(terms_response=(200, b"different terms text"))
    connector = AcledConnector(store, credentials=credentials, client=_client(transport))
    with pytest.raises(AcledTermsAcknowledgementRequired, match="hash has changed"):
        list(connector.fetch_incremental(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_terms_gate_passes_with_matching_hash(
    acled_acknowledged_store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    """When recorded hash matches live, the gate passes."""
    transport = _RoutingTransport(
        token_responses=[(200, _token_response())],
        events_responses=[(200, _events_page([_event(event_id="SOM-1")]))],
        terms_response=(200, b"acled terms content for testing"),
    )
    connector = AcledConnector(
        acled_acknowledged_store, credentials=credentials, client=_client(transport)
    )
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 1


def test_skip_terms_gate_flag_bypasses_check(
    store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    """skip_terms_gate=True is the test/dev escape hatch."""
    transport = _RoutingTransport(
        token_responses=[(200, _token_response())],
        events_responses=[(200, _events_page([_event(event_id="SOM-2")]))],
    )
    connector = AcledConnector(
        store,
        credentials=credentials,
        client=_client(transport),
        skip_terms_gate=True,
    )
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 1


def test_terms_gate_uses_recorded_hash_when_live_fetch_fails(
    acled_acknowledged_store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    """If live terms fetch fails, fall back to recorded hash with a warning."""
    transport = _RoutingTransport(
        token_responses=[(200, _token_response())],
        events_responses=[(200, _events_page([_event(event_id="SOM-3")]))],
        terms_response=(500, b"server error"),
    )
    connector = AcledConnector(
        acled_acknowledged_store, credentials=credentials, client=_client(transport)
    )
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    assert len(records) == 1


# --- credentials & OAuth ---------------------------------------------------


def test_missing_credentials_raises(
    acled_acknowledged_store: DuckDBStore,
) -> None:
    transport = _RoutingTransport(terms_response=(200, b"acled terms content for testing"))
    connector = AcledConnector(
        acled_acknowledged_store, credentials=None, client=_client(transport)
    )
    with pytest.raises(CredentialMissingError, match="ACLED_USERNAME"):
        list(connector.fetch_incremental(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_wrong_credential_type_raises(
    acled_acknowledged_store: DuckDBStore,
) -> None:
    """An ApiKeyBundle (wrong shape for ACLED) should raise."""
    transport = _RoutingTransport(terms_response=(200, b"acled terms content for testing"))
    connector = AcledConnector(
        acled_acknowledged_store,
        credentials=ApiKeyBundle(source_id="acled", api_key="wrong"),
        client=_client(transport),
    )
    with pytest.raises(CredentialMissingError):
        list(connector.fetch_incremental(since=datetime(2026, 1, 1, tzinfo=UTC)))


def test_password_grant_obtains_access_token(
    acled_acknowledged_store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    transport = _RoutingTransport(
        token_responses=[(200, _token_response())],
        events_responses=[(200, _events_page([_event(event_id="SOM-1")]))],
        terms_response=(200, b"acled terms content for testing"),
    )
    connector = AcledConnector(
        acled_acknowledged_store, credentials=credentials, client=_client(transport)
    )
    list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    # The token request should have happened first.
    token_requests = [r for r in transport.requests_received if r.url.path == "/oauth/token"]
    assert len(token_requests) >= 1


def test_token_refresh_when_near_expiry(
    acled_acknowledged_store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    """Tokens close to expiry are refreshed proactively."""
    # First token expires in 100s (well within the buffer).
    transport = _RoutingTransport(
        token_responses=[
            (200, _token_response(expires_in=100)),
            (200, _token_response(expires_in=86400)),
        ],
        events_responses=[
            (200, _events_page([_event(event_id="SOM-1")])),
            (200, _events_page([_event(event_id="SOM-2")])),
        ],
        terms_response=(200, b"acled terms content for testing"),
    )
    connector = AcledConnector(
        acled_acknowledged_store, credentials=credentials, client=_client(transport)
    )
    # Two separate fetches → first uses the short-lived token, second
    # refreshes before fetching.
    list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    # Force the cached token to look near-expiry.
    if connector._token_cache:
        connector._token_cache.expires_at = datetime.now(tz=UTC) + timedelta(seconds=10)
    list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    # We should have at least 2 token requests overall.
    token_requests = [r for r in transport.requests_received if r.url.path == "/oauth/token"]
    assert len(token_requests) >= 2


# --- normalization ---------------------------------------------------------


def test_normalize_event_record(
    acled_acknowledged_store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    transport = _RoutingTransport(terms_response=(200, b"acled terms content for testing"))
    connector = AcledConnector(
        acled_acknowledged_store, credentials=credentials, client=_client(transport)
    )
    raw = RawRecord(
        source_id="acled",
        source_record_id="SOM-1234",
        source_payload_json=_event(
            event_id="SOM-1234",
            event_date="2026-05-14",
            iso3="SOM",
            event_type="Violence against civilians",
        ),
        source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, EventStreamRecord)
    assert normalized.event_ts == datetime(2026, 5, 14, tzinfo=UTC)
    assert normalized.country_iso3 == "SOM"
    assert normalized.event_class == "Violence against civilians"
    assert normalized.actor_primary == "Actor A"
    assert normalized.actor_secondary is None
    assert normalized.description == "test event note"


def test_normalize_handles_missing_fields(
    acled_acknowledged_store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    transport = _RoutingTransport(terms_response=(200, b"acled terms content for testing"))
    connector = AcledConnector(
        acled_acknowledged_store, credentials=credentials, client=_client(transport)
    )
    raw = RawRecord(
        source_id="acled",
        source_record_id="X",
        source_payload_json={
            "event_id_cnty": "X",
            "event_date": "",
            "iso3": "",
            "actor1": "",
        },
        source_publication_ts=datetime(2026, 5, 14, tzinfo=UTC),
    )
    normalized = connector.normalize(raw)
    assert isinstance(normalized, EventStreamRecord)
    assert normalized.country_iso3 is None
    assert normalized.actor_primary is None


# --- pagination ------------------------------------------------------------


def test_pagination_terminates_on_partial_page(
    acled_acknowledged_store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    """A page with fewer rows than the limit ends pagination."""
    transport = _RoutingTransport(
        token_responses=[(200, _token_response())],
        events_responses=[
            (200, _events_page([_event(event_id=f"X-{i}") for i in range(10)])),
        ],
        terms_response=(200, b"acled terms content for testing"),
    )
    connector = AcledConnector(
        acled_acknowledged_store, credentials=credentials, client=_client(transport)
    )
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    # 10 records < page size (5000) → no second request issued for that year.
    assert len(records) == 10


# --- backfill --------------------------------------------------------------


def test_backfill_emits_year_page_resume_tokens(
    acled_acknowledged_store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    # Backfill iterates from 1997 → 2026 by default. We need an empty-page
    # response for each year except the one that produces a record. Add 30
    # empty responses; replace the last (2026) with the populated one.
    empty_pages = [(200, _events_page([]))] * 29
    populated = (200, _events_page([_event(event_id="X", event_date="2026-01-01")]))
    transport = _RoutingTransport(
        token_responses=[(200, _token_response())],
        events_responses=[*empty_pages, populated],
        terms_response=(200, b"acled terms content for testing"),
    )
    connector = AcledConnector(
        acled_acknowledged_store, credentials=credentials, client=_client(transport)
    )
    pairs = list(connector.fetch_backfill(until=datetime(2026, 12, 31, tzinfo=UTC)))
    assert len(pairs) > 0
    token_value = pairs[0][1].value
    assert ":" in token_value
    year, page = token_value.split(":")
    assert year == "2026"
    assert page == "1"


def test_backfill_resumes_from_token(
    acled_acknowledged_store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    transport = _RoutingTransport(
        token_responses=[(200, _token_response())],
        events_responses=[
            (200, _events_page([_event(event_id="X", event_date="2026-06-01")])),
        ],
        terms_response=(200, b"acled terms content for testing"),
    )
    connector = AcledConnector(
        acled_acknowledged_store, credentials=credentials, client=_client(transport)
    )
    list(
        connector.fetch_backfill(
            until=datetime(2026, 12, 31, tzinfo=UTC),
            resume_token=ResumeToken(value="2026:1"),
        )
    )
    events_requests = [r for r in transport.requests_received if r.url.path == "/api/acled/read"]
    # First events request should have year=2026 and page=2.
    assert len(events_requests) == 1
    request = events_requests[0]
    assert "year=2026" in str(request.url)
    assert "page=2" in str(request.url)


# --- error paths -----------------------------------------------------------


def test_429_retries_then_raises(
    acled_acknowledged_store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    transport = _RoutingTransport(
        token_responses=[(200, _token_response())],
        events_responses=[(429, {"error": "rate limit"})] * 6,
        terms_response=(200, b"acled terms content for testing"),
    )
    connector = AcledConnector(
        acled_acknowledged_store, credentials=credentials, client=_client(transport)
    )
    import razor_rooster.data_ingest.connectors.acled as acled_module

    original = acled_module.exponential_backoff_with_jitter
    acled_module.exponential_backoff_with_jitter = lambda *a, **k: 0.0  # type: ignore[assignment]
    try:
        with pytest.raises(RateLimitedError):
            list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    finally:
        acled_module.exponential_backoff_with_jitter = original


def test_401_response_triggers_token_refresh(
    acled_acknowledged_store: DuckDBStore, credentials: UserPasswordBundle
) -> None:
    """A 401 mid-stream should refresh and retry."""
    transport = _RoutingTransport(
        token_responses=[
            (200, _token_response()),
            (200, _token_response()),
        ],
        events_responses=[
            (401, {"error": "expired"}),
            (200, _events_page([_event(event_id="X")])),
        ],
        terms_response=(200, b"acled terms content for testing"),
    )
    connector = AcledConnector(
        acled_acknowledged_store, credentials=credentials, client=_client(transport)
    )
    records = list(connector.fetch_incremental(since=datetime(2026, 5, 1, tzinfo=UTC)))
    # Should ultimately succeed.
    assert len(records) == 1
    # Two token grants happened (initial + post-401 refresh-via-password).
    token_requests = [r for r in transport.requests_received if r.url.path == "/oauth/token"]
    assert len(token_requests) == 2


# --- helpers ---------------------------------------------------------------


def test_fetch_acled_terms_hash_round_trip() -> None:
    """The terms-hash helper produces a stable SHA-256 hex digest."""
    body = b"some terms text"
    expected = hashlib.sha256(body).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/terms-and-conditions":
            return httpx.Response(200, content=body)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    actual = fetch_acled_terms_hash(client)
    assert actual == expected
