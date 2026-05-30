"""T-PMC-040 — daily markets sync tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from razor_rooster.data_ingest.persistence.duckdb_store import DuckDBStore
from razor_rooster.data_ingest.persistence.migrations import (
    run_pending_migrations as run_pending_data_ingest_migrations,
)
from razor_rooster.polymarket_connector.client.gamma import (
    GAMMA_BASE_URL,
    GammaClient,
)
from razor_rooster.polymarket_connector.client.rate_limit import (
    TokenBucket,
    reset_shared_bucket,
)
from razor_rooster.polymarket_connector.persistence.migrations import (
    run_pending_polymarket_migrations,
)
from razor_rooster.polymarket_connector.persistence.source import (
    POLYMARKET_LIVE_SOURCE_ID,
    register_polymarket_sources,
)
from razor_rooster.polymarket_connector.sync.markets import (
    MarketSyncReport,
    sync_markets,
)


@pytest.fixture(autouse=True)
def _isolated_bucket() -> Iterator[None]:
    reset_shared_bucket()
    yield
    reset_shared_bucket()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "polymarket_sync.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        register_polymarket_sources(conn)
    try:
        yield s
    finally:
        s.close()


def _market_payload(
    condition_id: str,
    *,
    slug: str | None = None,
    active: bool = True,
    closed: bool = False,
    resolved: bool = False,
    outcomes: list[str] | None = None,
    token_ids: list[str] | None = None,
    category: str | None = "Politics",
    volume: float | None = 1234.5,
    end_date: str | None = "2026-12-31T23:59:59Z",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "conditionId": condition_id,
        "slug": slug or f"market-{condition_id[2:8]}",
        "question": f"Will event {condition_id[2:8]} happen?",
        "description": "A synthetic test market.",
        "active": active,
        "closed": closed,
        "resolved": resolved,
        "outcomes": outcomes or ["Yes", "No"],
        "clobTokenIds": token_ids or [f"{condition_id}-yes", f"{condition_id}-no"],
        "category": category,
        "volume": volume,
        "endDate": end_date,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-05-14T08:00:00Z",
        "tags": ["politics", "elections"],
    }
    if extra:
        payload.update(extra)
    return payload


def _build_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> GammaClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=GAMMA_BASE_URL)
    bucket = TokenBucket(capacity=1000.0, refill_per_second=1000.0)
    return GammaClient(http_client=http, bucket=bucket, max_retries=0)


def _two_market_handler(
    *,
    active_payloads: list[dict[str, Any]] | None = None,
    closed_payloads: list[dict[str, Any]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a mock handler that returns active and closed market lists.

    Pages are short (length < page_size) so iter_markets exits after one
    page in each direction.
    """
    active = active_payloads or []
    closed = closed_payloads or []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/markets":
            return httpx.Response(404, json={"error": "no such path"})
        active_param = request.url.params.get("active")
        closed_param = request.url.params.get("closed")
        if active_param == "true" and closed_param == "false":
            return httpx.Response(200, json=active)
        if active_param == "false" and closed_param == "true":
            return httpx.Response(200, json=closed)
        return httpx.Response(200, json=[])

    return handler


def test_sync_markets_inserts_new_records(store: DuckDBStore) -> None:
    payloads = [_market_payload("0xabc"), _market_payload("0xdef")]
    handler = _two_market_handler(active_payloads=payloads)

    with _build_client(handler) as client:
        report = sync_markets(store, client=client)

    assert isinstance(report, MarketSyncReport)
    assert report.markets_total_seen == 2
    assert report.markets_inserted == 2
    assert report.markets_updated == 0
    assert report.markets_unchanged == 0
    assert report.markets_removed == 0

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT condition_id, market_type, active, closed, resolved "
            "FROM polymarket_markets ORDER BY condition_id"
        ).fetchall()

    assert len(rows) == 2
    assert {r[0] for r in rows} == {"0xabc", "0xdef"}
    for row in rows:
        assert row[1] == "binary"
        assert row[2] is True
        assert row[3] is False
        assert row[4] is False


def test_sync_markets_idempotent_on_unchanged_payload(store: DuckDBStore) -> None:
    payloads = [_market_payload("0xabc")]
    handler = _two_market_handler(active_payloads=payloads)

    with _build_client(handler) as client:
        first = sync_markets(store, client=client)
        # Second sync with identical responses → all unchanged.
        second = sync_markets(store, client=client)

    assert first.markets_inserted == 1
    assert second.markets_inserted == 0
    assert second.markets_updated == 0
    assert second.markets_unchanged == 1


def test_sync_markets_revision_supersedes_prior_row(store: DuckDBStore) -> None:
    payload_v1 = _market_payload("0xabc")
    payload_v2 = _market_payload(
        "0xabc",
        extra={"updatedAt": "2026-05-15T08:00:00Z", "volume": 9999.0},
    )

    payloads_state: list[list[dict[str, Any]]] = [[payload_v1], [payload_v2]]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/markets":
            return httpx.Response(404)
        active_param = request.url.params.get("active")
        if active_param == "true":
            return httpx.Response(200, json=payloads_state[0])
        return httpx.Response(200, json=[])

    with _build_client(handler) as client:
        sync_markets(store, client=client)
        # Swap to the v2 response for the next sync.
        payloads_state[0] = [payload_v2]
        second = sync_markets(store, client=client)

    assert second.markets_updated == 1

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT volume_lifetime, superseded_at "
            "FROM polymarket_markets WHERE condition_id = ? ORDER BY fetch_ts",
            ["0xabc"],
        ).fetchall()

    assert len(rows) == 2
    # Prior row superseded; new row active.
    assert rows[0][1] is not None  # superseded_at set on old row
    assert rows[1][1] is None  # new row is active
    assert rows[1][0] == 9999.0


def test_sync_markets_marks_removed_for_missing_market(store: DuckDBStore) -> None:
    """Once a market disappears from the upstream response, removed_at is set."""

    def first_handler(request: httpx.Request) -> httpx.Response:
        active_param = request.url.params.get("active")
        if active_param == "true":
            return httpx.Response(200, json=[_market_payload("0xabc")])
        return httpx.Response(200, json=[])

    with _build_client(first_handler) as client:
        sync_markets(store, client=client)

    # Second sync: empty upstream — the market disappeared.
    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    with _build_client(empty_handler) as client:
        report = sync_markets(store, client=client)

    assert report.markets_removed == 1

    with store.connection() as conn:
        row = conn.execute(
            "SELECT removed_at FROM polymarket_markets WHERE condition_id = ?",
            ["0xabc"],
        ).fetchone()

    assert row is not None
    assert row[0] is not None


def test_sync_markets_records_last_successful_fetch(store: DuckDBStore) -> None:
    handler = _two_market_handler(active_payloads=[_market_payload("0xabc")])
    explicit_now = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)

    with _build_client(handler) as client:
        sync_markets(store, client=client, now=explicit_now)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT last_successful_fetch FROM sources WHERE source_id = ?",
            [POLYMARKET_LIVE_SOURCE_ID],
        ).fetchone()

    assert row is not None
    assert row[0] == explicit_now


def test_sync_markets_handles_active_and_closed_markets(store: DuckDBStore) -> None:
    """Both active and closed-not-resolved markets get persisted."""
    active = [_market_payload("0xa01")]
    closed = [_market_payload("0xc01", active=False, closed=True, resolved=False)]
    handler = _two_market_handler(active_payloads=active, closed_payloads=closed)

    with _build_client(handler) as client:
        report = sync_markets(store, client=client)

    assert report.markets_total_seen == 2
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT condition_id, active, closed FROM polymarket_markets ORDER BY condition_id"
        ).fetchall()
    assert {r[0] for r in rows} == {"0xa01", "0xc01"}
    by_id = {r[0]: r for r in rows}
    assert by_id["0xa01"][1] is True
    assert by_id["0xa01"][2] is False
    assert by_id["0xc01"][1] is False
    assert by_id["0xc01"][2] is True


def test_sync_markets_skips_non_binary_in_count(store: DuckDBStore) -> None:
    multi = _market_payload(
        "0xabc",
        outcomes=["Cand A", "Cand B", "Cand C"],
        token_ids=["t1", "t2", "t3"],
    )
    binary = _market_payload("0xdef")
    handler = _two_market_handler(active_payloads=[multi, binary])

    with _build_client(handler) as client:
        report = sync_markets(store, client=client)

    assert report.markets_total_seen == 2
    assert report.skipped_non_binary == 1

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT condition_id, market_type FROM polymarket_markets ORDER BY condition_id"
        ).fetchall()
    by_id = {r[0]: r[1] for r in rows}
    assert by_id["0xabc"] == "multi"
    assert by_id["0xdef"] == "binary"


def test_sync_markets_records_neg_risk() -> None:
    """A negRisk market gets the 'negrisk' market_type tag."""
    from razor_rooster.polymarket_connector.client.gamma import GammaMarket
    from razor_rooster.polymarket_connector.sync.markets import _market_type

    market = GammaMarket(
        condition_id="0xneg",
        slug="neg-risk-market",
        question="Compound multi-outcome?",
        active=True,
        closed=False,
        raw={
            "negRisk": True,
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["a", "b"],
        },
    )
    assert _market_type(market) == "negrisk"


def test_sync_markets_handles_fetch_error(store: DuckDBStore) -> None:
    """A network failure during fetch is captured as a report error, not raised."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server down")

    with _build_client(handler) as client:
        report = sync_markets(store, client=client)

    assert report.errors  # at least one error captured
    assert report.markets_inserted == 0
    assert report.markets_total_seen == 0


def test_sync_markets_dedupes_duplicate_condition_ids(store: DuckDBStore) -> None:
    """If the same condition_id appears in both active and closed lists, take one."""
    payload_active = _market_payload("0xdup", active=True, closed=False)
    payload_closed = _market_payload("0xdup", active=False, closed=True)
    handler = _two_market_handler(
        active_payloads=[payload_active], closed_payloads=[payload_closed]
    )

    with _build_client(handler) as client:
        report = sync_markets(store, client=client)

    assert report.markets_total_seen == 2  # both pages saw it
    # Only one row inserted (the active version, which is the first in the dedup loop).
    assert report.markets_inserted == 1

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT active FROM polymarket_markets WHERE condition_id = ?",
            ["0xdup"],
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] is True


def test_sync_markets_preserves_raw_payload(store: DuckDBStore) -> None:
    payload = _market_payload("0xabc", extra={"custom": "value", "nested": {"a": 1}})
    handler = _two_market_handler(active_payloads=[payload])

    with _build_client(handler) as client:
        sync_markets(store, client=client)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT source_payload_json FROM polymarket_markets WHERE condition_id = ?",
            ["0xabc"],
        ).fetchone()
    assert row is not None
    import json

    raw = json.loads(row[0])
    assert raw["custom"] == "value"
    assert raw["nested"] == {"a": 1}


def test_sync_markets_handles_jsonstring_clob_token_ids(store: DuckDBStore) -> None:
    """Polymarket sometimes returns clobTokenIds as a JSON-stringified array."""
    payload = _market_payload("0xabc")
    payload["clobTokenIds"] = '["0xabc-yes", "0xabc-no"]'
    handler = _two_market_handler(active_payloads=[payload])

    with _build_client(handler) as client:
        report = sync_markets(store, client=client)

    assert report.markets_inserted == 1
    with store.connection() as conn:
        row = conn.execute(
            "SELECT outcome_tokens, market_type FROM polymarket_markets WHERE condition_id = ?",
            ["0xabc"],
        ).fetchone()
    assert row is not None
    import json

    tokens = json.loads(row[0])
    assert len(tokens) == 2
    assert tokens[0]["token_id"] == "0xabc-yes"
    assert tokens[1]["token_id"] == "0xabc-no"
    assert row[1] == "binary"


def test_sync_markets_includes_timing(store: DuckDBStore) -> None:
    handler = _two_market_handler(active_payloads=[_market_payload("0xabc")])
    explicit_now = datetime(2026, 5, 14, 8, 0, tzinfo=UTC)

    with _build_client(handler) as client:
        report = sync_markets(store, client=client, now=explicit_now)

    assert report.started_at == explicit_now
    assert report.completed_at is not None
    assert report.duration_seconds is not None
    assert report.duration_seconds >= 0


def test_sync_markets_can_run_against_empty_upstream(store: DuckDBStore) -> None:
    handler = _two_market_handler(active_payloads=[], closed_payloads=[])

    with _build_client(handler) as client:
        report = sync_markets(store, client=client)

    assert report.markets_total_seen == 0
    assert report.markets_inserted == 0
    assert report.markets_removed == 0
    assert not report.errors
