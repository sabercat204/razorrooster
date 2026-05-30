"""T-064 verification — ACLED deletions reconciliation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pytest

from razor_rooster.data_ingest.connectors.acled import AcledConnector
from razor_rooster.data_ingest.connectors.acled_deletions import (
    DeletionReconciliationReport,
    ensure_acled_deleted_state_table,
    get_last_reconciliation_ts,
    reconcile_deletions,
    upsert_reconciliation_state,
)
from razor_rooster.data_ingest.credentials import UserPasswordBundle
from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import run_pending_migrations
from razor_rooster.data_ingest.persistence.provenance import (
    record_license_acknowledgement,
    register_source,
)
from razor_rooster.data_ingest.persistence.staging_merge import staging_merge


@pytest.fixture
def store(tmp_path: Path) -> DuckDBStore:
    db_path = tmp_path / "acled_del.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_migrations(conn)
    return s


def _register_acled(store: DuckDBStore) -> str:
    """Register ACLED with an acknowledged Terms hash for predictable tests."""
    terms_hash = hashlib.sha256(b"acled terms content for testing").hexdigest()
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
        record_license_acknowledgement(conn, source_id="acled", terms_hash=terms_hash)
    return terms_hash


def _seed_event_rows(store: DuckDBStore, event_ids: list[str]) -> None:
    """Insert active event_stream rows for the given ACLED event IDs."""
    now = datetime.now(tz=UTC)
    pub_ts = datetime(2026, 5, 1, tzinfo=UTC)
    rows = []
    for eid in event_ids:
        rows.append(
            {
                "source_id": "acled",
                "source_record_id": eid,
                "source_publication_ts": pub_ts,
                "fetch_ts": now,
                "connector_version": "acled@0.1.0",
                "superseded_at": None,
                "source_payload_json": json.dumps({"event_id_cnty": eid, "actor1": "X"}),
                "event_ts": pub_ts,
                "country_iso3": "SOM",
                "actor_primary": "X",
                "actor_secondary": None,
                "event_class": "Test",
                "description": "Test event",
            }
        )
    batch = pa.table(
        {
            key: [r[key] for r in rows]
            for key in (
                "source_id",
                "source_record_id",
                "source_publication_ts",
                "fetch_ts",
                "connector_version",
                "superseded_at",
                "source_payload_json",
                "event_ts",
                "country_iso3",
                "actor_primary",
                "actor_secondary",
                "event_class",
                "description",
            )
        }
    )
    with store.connection() as conn:
        staging_merge(conn, "event_stream", batch)


class _DeletionTransport(httpx.MockTransport):
    """Routes /oauth/token, /api/deleted/read, and /terms-and-conditions."""

    def __init__(
        self,
        *,
        deleted_responses: list[tuple[int, dict[str, Any]]] | None = None,
    ) -> None:
        self.deleted_responses = list(deleted_responses or [])
        self.requests_received: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests_received.append(request)
            path = request.url.path
            if path.endswith("/oauth/token"):
                return httpx.Response(
                    200,
                    json={
                        "access_token": "test_access_token",
                        "refresh_token": "test_refresh_token",
                        "token_type": "Bearer",
                        "expires_in": 86400,
                    },
                )
            if path == "/api/deleted/read":
                if not self.deleted_responses:
                    return httpx.Response(200, json={"data": []})
                status, body = self.deleted_responses.pop(0)
                return httpx.Response(status, json=body)
            if path == "/terms-and-conditions":
                return httpx.Response(200, content=b"acled terms content for testing")
            return httpx.Response(404)

        super().__init__(handler)


def _connector(store: DuckDBStore, transport: _DeletionTransport) -> AcledConnector:
    return AcledConnector(
        store,
        credentials=UserPasswordBundle(source_id="acled", username="t@example.com", password="p"),
        client=httpx.Client(transport=transport, timeout=5.0),
        skip_terms_gate=True,
    )


def test_state_table_created_on_demand(store: DuckDBStore) -> None:
    with store.connection() as conn:
        ensure_acled_deleted_state_table(conn)
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    assert "acled_deleted_state" in {r[0] for r in rows}


def test_get_last_reconciliation_ts_returns_none_when_no_state(
    store: DuckDBStore,
) -> None:
    with store.connection() as conn:
        ensure_acled_deleted_state_table(conn)
        ts = get_last_reconciliation_ts(conn)
    assert ts is None


def test_upsert_reconciliation_state_round_trip(store: DuckDBStore) -> None:
    when = datetime(2026, 5, 14, tzinfo=UTC)
    with store.connection() as conn:
        upsert_reconciliation_state(
            conn,
            source_id="acled",
            last_reconciliation_ts=when,
            notes="first run",
        )
    with store.connection() as conn:
        ts = get_last_reconciliation_ts(conn)
    assert ts == when


def test_reconcile_deletions_supersedes_matching_rows(store: DuckDBStore) -> None:
    _register_acled(store)
    _seed_event_rows(store, ["SOM-1234", "SOM-5678", "SOM-9999"])

    deleted_ts_unix = int(datetime(2026, 5, 14, 12, 0, tzinfo=UTC).timestamp())
    transport = _DeletionTransport(
        deleted_responses=[
            (
                200,
                {
                    "data": [
                        {"event_id_cnty": "SOM-1234", "deleted_timestamp": deleted_ts_unix},
                        {"event_id_cnty": "SOM-5678", "deleted_timestamp": deleted_ts_unix},
                    ]
                },
            )
        ]
    )
    connector = _connector(store, transport)
    report = reconcile_deletions(store, connector)

    assert isinstance(report, DeletionReconciliationReport)
    assert report.deleted_ids_seen == 2
    assert report.rows_superseded == 2

    # The superseded rows should be marked.
    with store.connection() as conn:
        active_rows = conn.execute(
            "SELECT source_record_id FROM event_stream "
            "WHERE source_id = 'acled' AND superseded_at IS NULL"
        ).fetchall()
        superseded_rows = conn.execute(
            "SELECT source_record_id FROM event_stream "
            "WHERE source_id = 'acled' AND superseded_at IS NOT NULL"
        ).fetchall()

    active_ids = {r[0] for r in active_rows}
    superseded_ids = {r[0] for r in superseded_rows}
    assert active_ids == {"SOM-9999"}
    assert superseded_ids == {"SOM-1234", "SOM-5678"}


def test_reconciliation_is_idempotent(store: DuckDBStore) -> None:
    """Running the reconciliation twice with the same upstream is a no-op."""
    _register_acled(store)
    _seed_event_rows(store, ["SOM-1234"])

    deleted_ts_unix = int(datetime(2026, 5, 14, tzinfo=UTC).timestamp())
    transport = _DeletionTransport(
        deleted_responses=[
            (
                200,
                {"data": [{"event_id_cnty": "SOM-1234", "deleted_timestamp": deleted_ts_unix}]},
            ),
            (200, {"data": []}),
        ]
    )
    connector = _connector(store, transport)
    first = reconcile_deletions(store, connector)
    assert first.rows_superseded == 1

    second = reconcile_deletions(store, connector)
    assert second.rows_superseded == 0


def test_watermark_advances_on_reconciliation(store: DuckDBStore) -> None:
    _register_acled(store)
    _seed_event_rows(store, ["SOM-1234"])

    deleted_ts = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    transport = _DeletionTransport(
        deleted_responses=[
            (
                200,
                {
                    "data": [
                        {
                            "event_id_cnty": "SOM-1234",
                            "deleted_timestamp": int(deleted_ts.timestamp()),
                        }
                    ]
                },
            )
        ]
    )
    connector = _connector(store, transport)
    report = reconcile_deletions(store, connector)
    assert report.new_watermark is not None

    with store.connection() as conn:
        ts = get_last_reconciliation_ts(conn)
    assert ts is not None
    # Stored watermark should match the deleted_timestamp from the response.
    assert int(ts.timestamp()) == int(deleted_ts.timestamp())


def test_deletion_reason_annotation_in_payload(store: DuckDBStore) -> None:
    _register_acled(store)
    _seed_event_rows(store, ["SOM-1234"])

    deleted_ts_unix = int(datetime(2026, 5, 14, tzinfo=UTC).timestamp())
    transport = _DeletionTransport(
        deleted_responses=[
            (
                200,
                {"data": [{"event_id_cnty": "SOM-1234", "deleted_timestamp": deleted_ts_unix}]},
            )
        ]
    )
    connector = _connector(store, transport)
    reconcile_deletions(store, connector)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT source_payload_json FROM event_stream "
            "WHERE source_record_id = 'SOM-1234' AND superseded_at IS NOT NULL"
        ).fetchone()
    assert row is not None
    payload_text = row[0]
    payload = json.loads(payload_text) if isinstance(payload_text, str) else payload_text
    assert payload.get("_razor_deletion_reason") == "acled_deleted_endpoint"


def test_reconciliation_skips_unknown_event_ids(store: DuckDBStore) -> None:
    """A deletion for an event we never ingested is not an error."""
    _register_acled(store)
    # No event rows seeded.

    deleted_ts_unix = int(datetime(2026, 5, 14, tzinfo=UTC).timestamp())
    transport = _DeletionTransport(
        deleted_responses=[
            (
                200,
                {"data": [{"event_id_cnty": "UNKNOWN-1", "deleted_timestamp": deleted_ts_unix}]},
            )
        ]
    )
    connector = _connector(store, transport)
    report = reconcile_deletions(store, connector)
    assert report.deleted_ids_seen == 1
    assert report.rows_superseded == 0  # nothing matched


def test_reconciliation_starts_from_watermark(store: DuckDBStore) -> None:
    """If we already have a watermark, only newer deletions are processed."""
    _register_acled(store)
    _seed_event_rows(store, ["SOM-1234"])

    # Set watermark to 2026-05-14 12:00.
    watermark = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    with store.connection() as conn:
        upsert_reconciliation_state(
            conn,
            source_id="acled",
            last_reconciliation_ts=watermark,
        )

    transport = _DeletionTransport(deleted_responses=[(200, {"data": []})])
    connector = _connector(store, transport)
    reconcile_deletions(store, connector)

    # The deletion request should have included the watermark timestamp.
    deleted_requests = [r for r in transport.requests_received if r.url.path == "/api/deleted/read"]
    assert len(deleted_requests) == 1
    assert f"deleted_timestamp={int(watermark.timestamp())}" in str(deleted_requests[0].url)
