"""T-PMC-042 / T-PMC-043 — resolution backfill and daily delta tests."""

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
    POLYMARKET_RESOLUTIONS_SOURCE_ID,
    register_polymarket_sources,
)
from razor_rooster.polymarket_connector.sync.markets import sync_markets
from razor_rooster.polymarket_connector.sync.resolutions import (
    backfill_resolutions,
    sync_recent_resolutions,
)


@pytest.fixture(autouse=True)
def _isolated_bucket() -> Iterator[None]:
    reset_shared_bucket()
    yield
    reset_shared_bucket()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DuckDBStore]:
    db_path = tmp_path / "polymarket_resolutions.duckdb"
    s = DuckDBStore(db_path)
    with s.connection() as conn:
        run_pending_data_ingest_migrations(conn)
        run_pending_polymarket_migrations(conn)
        register_polymarket_sources(conn)
    try:
        yield s
    finally:
        s.close()


def _resolved_market_payload(
    condition_id: str,
    *,
    winning_index: int = 0,
    resolved_at: str = "2026-05-10T12:00:00Z",
    outcomes: tuple[str, str] = ("Yes", "No"),
    invalidated: bool = False,
) -> dict[str, Any]:
    prices = ["1.0", "0.0"] if winning_index == 0 else ["0.0", "1.0"]
    return {
        "conditionId": condition_id,
        "slug": f"resolved-{condition_id[2:6]}",
        "question": f"Did {condition_id[2:6]} happen?",
        "active": False,
        "closed": True,
        "resolved": True,
        "outcomes": list(outcomes),
        "outcomePrices": prices,
        "clobTokenIds": [f"{condition_id}-yes", f"{condition_id}-no"],
        "resolvedAt": resolved_at,
        "endDate": resolved_at,
        "volume": 12345.0,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": resolved_at,
        "invalidated": invalidated,
    }


def _gamma_resolved_handler(
    pages: list[list[dict[str, Any]]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a /markets handler that serves resolved-market pages keyed by offset.

    list_resolved() calls list_markets(active=None, closed=True), so the
    handler matches on closed=true regardless of the active param.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/markets":
            return httpx.Response(404)
        closed_param = request.url.params.get("closed")
        if closed_param == "true":
            offset = int(request.url.params.get("offset", "0"))
            limit = int(request.url.params.get("limit", "100"))
            page_index = offset // limit
            if page_index < len(pages):
                return httpx.Response(200, json=pages[page_index])
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    return handler


def _build_client(handler: Callable[[httpx.Request], httpx.Response]) -> GammaClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url=GAMMA_BASE_URL)
    bucket = TokenBucket(capacity=1000.0, refill_per_second=1000.0)
    return GammaClient(http_client=http, bucket=bucket, max_retries=0)


# -------------------------------------------------------------------------
# backfill_resolutions
# -------------------------------------------------------------------------
def test_backfill_resolutions_walks_pages_to_completion(store: DuckDBStore) -> None:
    page1 = [_resolved_market_payload(f"0xa{i:02x}") for i in range(100)]
    page2 = [_resolved_market_payload(f"0xb{i:02x}") for i in range(50)]
    handler = _gamma_resolved_handler([page1, page2])

    with _build_client(handler) as client:
        report = backfill_resolutions(store, client=client, page_size=100)

    assert report.status == "completed"
    assert report.pages_fetched == 2
    assert report.resolutions_inserted == 150

    with store.connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM polymarket_resolutions").fetchone()
    assert row is not None
    assert row[0] == 150


def test_backfill_resolutions_persists_resume_state(store: DuckDBStore) -> None:
    page1 = [_resolved_market_payload(f"0xa{i:02x}") for i in range(100)]
    page2 = [_resolved_market_payload(f"0xb{i:02x}") for i in range(100)]
    handler = _gamma_resolved_handler([page1, page2])

    with _build_client(handler) as client:
        report = backfill_resolutions(store, client=client, page_size=100, max_pages=1)

    assert report.status == "in_progress"
    assert report.pages_fetched == 1
    assert report.resolutions_inserted == 100
    assert report.next_offset == 100

    with store.connection() as conn:
        row = conn.execute(
            "SELECT last_resume_token, status FROM backfill_state WHERE source_id = ?",
            [POLYMARKET_RESOLUTIONS_SOURCE_ID],
        ).fetchone()

    assert row is not None
    assert row[0] == "100"
    assert row[1] == "in_progress"


def test_backfill_resolutions_resumes_from_persisted_offset(store: DuckDBStore) -> None:
    page1 = [_resolved_market_payload(f"0xa{i:02x}") for i in range(100)]
    page2 = [_resolved_market_payload(f"0xb{i:02x}") for i in range(50)]
    handler = _gamma_resolved_handler([page1, page2])

    with _build_client(handler) as client:
        # First run stops after one page.
        backfill_resolutions(store, client=client, page_size=100, max_pages=1)
        # Second run picks up where the first left off.
        second = backfill_resolutions(store, client=client, page_size=100)

    assert second.status == "completed"
    assert second.pages_fetched == 1  # only page 2 was fetched
    assert second.resolutions_inserted == 50

    with store.connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM polymarket_resolutions").fetchone()
    assert total is not None
    assert total[0] == 150


def test_backfill_resolutions_restart_clears_prior_state(store: DuckDBStore) -> None:
    page1 = [_resolved_market_payload(f"0xa{i:02x}") for i in range(100)]
    handler = _gamma_resolved_handler([page1])

    with _build_client(handler) as client:
        backfill_resolutions(store, client=client, page_size=100, max_pages=1)

    # restart=True wipes backfill state.
    with _build_client(_gamma_resolved_handler([page1])) as client:
        report = backfill_resolutions(store, client=client, page_size=100, restart=True)

    # On the second run, we expect to reach completion: first page,
    # then a short-empty page that exits.
    assert report.status == "completed"
    assert report.next_offset == 100  # offset advanced by 100 via the page
    # Restart deleted prior state, so this run has its own fresh state.


def test_backfill_resolutions_handles_fetch_error(store: DuckDBStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with _build_client(handler) as client:
        report = backfill_resolutions(store, client=client, page_size=100)

    assert report.status == "failed"
    assert report.errors

    # The backfill_state row should be marked failed.
    with store.connection() as conn:
        row = conn.execute(
            "SELECT status, notes FROM backfill_state WHERE source_id = ?",
            [POLYMARKET_RESOLUTIONS_SOURCE_ID],
        ).fetchone()
    assert row is not None
    assert row[0] == "failed"


def test_backfill_resolutions_idempotent_on_re_run(store: DuckDBStore) -> None:
    page1 = [_resolved_market_payload(f"0xa{i:02x}") for i in range(50)]
    handler = _gamma_resolved_handler([page1])

    with _build_client(handler) as client:
        first = backfill_resolutions(store, client=client, page_size=100)

    # Second run from the start finds offset already at 50 → no-op.
    with _build_client(_gamma_resolved_handler([])) as client:
        second = backfill_resolutions(store, client=client, page_size=100)

    assert first.resolutions_inserted == 50
    assert second.resolutions_inserted == 0
    assert second.status == "completed"


def test_backfill_resolutions_marks_polymarket_markets_resolved(
    store: DuckDBStore,
) -> None:
    """The market row's resolved/closed flags get flipped to TRUE when seen."""
    # Seed an active market first so we can observe the flip.
    active_payload = {
        "conditionId": "0xabc",
        "slug": "market-abc",
        "question": "Will it happen?",
        "active": True,
        "closed": False,
        "resolved": False,
        "outcomes": ["Yes", "No"],
        "clobTokenIds": ["0xabc-yes", "0xabc-no"],
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-05-14T08:00:00Z",
    }

    def markets_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets":
            active_param = request.url.params.get("active")
            if active_param == "true":
                return httpx.Response(200, json=[active_payload])
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    with _build_client(markets_handler) as client:
        sync_markets(store, client=client, now=datetime(2026, 5, 14, tzinfo=UTC))

    # Now the market resolves: backfill_resolutions sees it.
    resolved = _resolved_market_payload("0xabc")
    handler = _gamma_resolved_handler([[resolved]])

    with _build_client(handler) as client:
        report = backfill_resolutions(store, client=client, page_size=100)

    assert report.resolutions_inserted == 1

    with store.connection() as conn:
        row = conn.execute(
            "SELECT resolved, closed FROM polymarket_markets "
            "WHERE condition_id = ? AND superseded_at IS NULL",
            ["0xabc"],
        ).fetchone()

    assert row is not None
    assert row[0] is True
    assert row[1] is True


def test_backfill_resolutions_extracts_winning_outcome(store: DuckDBStore) -> None:
    """outcomePrices [1.0, 0.0] → winning_outcome_label = 'Yes'."""
    payload = _resolved_market_payload("0xabc", winning_index=0)
    handler = _gamma_resolved_handler([[payload]])

    with _build_client(handler) as client:
        backfill_resolutions(store, client=client, page_size=100)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT winning_outcome_label, winning_outcome_token_id, "
            "final_yes_price, final_no_price "
            "FROM polymarket_resolutions WHERE condition_id = ?",
            ["0xabc"],
        ).fetchone()

    assert row is not None
    assert row[0] == "Yes"
    assert row[1] == "0xabc-yes"
    assert row[2] == 1.0
    assert row[3] == 0.0


def test_backfill_resolutions_no_winning_for_invalid_market(store: DuckDBStore) -> None:
    """A market with outcomePrices [0.5, 0.5] (tie / refund) has no winner."""
    payload = _resolved_market_payload("0xinvalid")
    payload["outcomePrices"] = ["0.5", "0.5"]
    payload["invalidated"] = True
    handler = _gamma_resolved_handler([[payload]])

    with _build_client(handler) as client:
        backfill_resolutions(store, client=client, page_size=100)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT winning_outcome_label, winning_outcome_token_id, invalidated "
            "FROM polymarket_resolutions WHERE condition_id = ?",
            ["0xinvalid"],
        ).fetchone()

    assert row is not None
    assert row[0] is None
    assert row[1] is None
    assert row[2] is True


def test_backfill_resolutions_handles_string_outcome_prices(store: DuckDBStore) -> None:
    """Polymarket sometimes returns outcomePrices as a JSON-stringified list."""
    payload = _resolved_market_payload("0xabc")
    payload["outcomePrices"] = '["1.0", "0.0"]'
    payload["clobTokenIds"] = '["0xabc-yes", "0xabc-no"]'
    handler = _gamma_resolved_handler([[payload]])

    with _build_client(handler) as client:
        backfill_resolutions(store, client=client, page_size=100)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT winning_outcome_label, final_yes_price "
            "FROM polymarket_resolutions WHERE condition_id = ?",
            ["0xabc"],
        ).fetchone()

    assert row is not None
    assert row[0] == "Yes"
    assert row[1] == 1.0


def test_backfill_resolutions_records_last_successful_fetch(store: DuckDBStore) -> None:
    payload = _resolved_market_payload("0xabc")
    handler = _gamma_resolved_handler([[payload]])

    when = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)
    with _build_client(handler) as client:
        backfill_resolutions(store, client=client, page_size=100, now=when)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT last_successful_fetch FROM sources WHERE source_id = ?",
            [POLYMARKET_RESOLUTIONS_SOURCE_ID],
        ).fetchone()

    assert row is not None
    assert row[0] == when


# -------------------------------------------------------------------------
# sync_recent_resolutions
# -------------------------------------------------------------------------
def test_sync_recent_resolutions_pulls_new_resolutions(store: DuckDBStore) -> None:
    page = [_resolved_market_payload(f"0xa{i:02x}") for i in range(5)]
    handler = _gamma_resolved_handler([page])

    with _build_client(handler) as client:
        report = sync_recent_resolutions(store, client=client)

    assert report.resolutions_inserted == 5
    assert report.errors == []


def test_sync_recent_resolutions_short_circuits_on_unchanged_page(
    store: DuckDBStore,
) -> None:
    """Once a page contains only known resolutions, the delta walk stops."""
    page1 = [_resolved_market_payload(f"0xa{i:02x}") for i in range(5)]
    handler1 = _gamma_resolved_handler([page1])

    # Seed via initial backfill.
    with _build_client(handler1) as client:
        backfill_resolutions(store, client=client, page_size=100)

    # Re-run the delta — same data, should short-circuit.
    handler2 = _gamma_resolved_handler([page1])
    with _build_client(handler2) as client:
        report = sync_recent_resolutions(store, client=client)

    assert report.resolutions_inserted == 0
    assert report.resolutions_unchanged == 5
    assert report.short_circuit_offset == 0


def test_sync_recent_resolutions_max_pages_caps_walk(store: DuckDBStore) -> None:
    """max_pages bounds the delta walk."""
    pages = [[_resolved_market_payload(f"0xp{p}-{i:02x}") for i in range(100)] for p in range(5)]
    handler = _gamma_resolved_handler(pages)

    with _build_client(handler) as client:
        report = sync_recent_resolutions(store, client=client, page_size=100, max_pages=2)

    assert report.pages_fetched == 2
    assert report.resolutions_inserted == 200


def test_sync_recent_resolutions_handles_empty(store: DuckDBStore) -> None:
    handler = _gamma_resolved_handler([])

    with _build_client(handler) as client:
        report = sync_recent_resolutions(store, client=client)

    assert report.resolutions_inserted == 0
    assert report.errors == []


def test_sync_recent_resolutions_records_errors(store: DuckDBStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with _build_client(handler) as client:
        report = sync_recent_resolutions(store, client=client)

    assert report.errors


def test_sync_recent_resolutions_records_last_successful_fetch(store: DuckDBStore) -> None:
    payload = _resolved_market_payload("0xabc")
    handler = _gamma_resolved_handler([[payload]])
    when = datetime(2026, 5, 14, 9, 0, tzinfo=UTC)

    with _build_client(handler) as client:
        sync_recent_resolutions(store, client=client, now=when)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT last_successful_fetch FROM sources WHERE source_id = ?",
            [POLYMARKET_RESOLUTIONS_SOURCE_ID],
        ).fetchone()

    assert row is not None
    assert row[0] == when
